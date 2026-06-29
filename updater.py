"""Standalone update checker, branch selector, and auto-updater.

This module is intentionally **decoupled** from the jd2021_installer package.
It imports ONLY from the Python standard library and ``requests`` so that even
if the rest of the tool's code is broken or mid-update, this module can still
run to pull fixes.

Usage::

    from updater import Updater

    u = Updater(project_root=Path("."))
    result = u.check_for_updates()
    if not result.is_up_to_date:
        u.perform_update()
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_OWNER = "VenB304"
GITHUB_REPO = "jd2021-map-installer"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
DEFAULT_BRANCH = "master"
STATE_FILENAME = "updater_state.json"

# Paths that must NEVER be deleted during a zip-mode update.
# Relative to project root.  Matched case-insensitively against the first
# path component of each item inside the project directory.
PRESERVE_PATHS: set[str] = {
    "installer_settings.json",
    "updater_state.json",
    "mapdownloads",
    "cache",
    "temp",
    "logs",
    ".browser-profile",
    ".git",
    ".venv",
    "tools",          # portable git, python, AssetStudio, vgmstream — never wipe
    "__pycache__",
    ".pytest_cache",
    ".cursor",
    "jdnext_songdb_synth.json",
    "map_readjust_index.json",
    "map_readjust_index.jsonold",
}

_HTTP_TIMEOUT = 15  # seconds

logger = logging.getLogger("jd2021.updater")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UpdateCheckResult:
    """Outcome of an update check."""

    is_up_to_date: bool
    local_commit: str
    remote_commit: str
    remote_commit_message: str
    remote_commit_date: str
    branch: str
    is_git_repo: bool
    commits_behind: int = 0
    error: Optional[str] = None


@dataclass
class UpdateResult:
    """Outcome of an update operation."""

    success: bool
    method: str  # "git" or "zip"
    old_commit: str
    new_commit: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class Updater:
    """Check for updates and apply them via git or zip download.

    Parameters
    ----------
    project_root:
        Absolute path to the repository / install directory.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._state_file = self.project_root / STATE_FILENAME

    # ----- state persistence ------------------------------------------------

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self, data: dict) -> None:
        try:
            self._state_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not write updater state: %s", exc)

    def initialize_state(self) -> None:
        """Create or refresh the state file from the current environment."""
        state = self._load_state()
        state["current_commit_sha"] = self.get_current_commit_full()
        state["tracked_branch"] = self.get_current_branch()
        state["repo_owner"] = GITHUB_OWNER
        state["repo_name"] = GITHUB_REPO
        self._save_state(state)

    # ----- environment detection --------------------------------------------

    def is_git_repo(self) -> bool:
        """Return True if the project root contains a ``.git`` directory."""
        return (self.project_root / ".git").is_dir()

    def detect_user_paths_inside_root(self) -> list[Path]:
        """Return any user-configured paths that live *inside* the project root.

        Reads ``installer_settings.json`` using the stdlib only (no package
        imports) and resolves every string value that looks like an absolute
        path.  Any that are children of ``self.project_root`` are returned so
        callers can either warn the user or auto-preserve them during an update.

        Returns an empty list when the settings file cannot be read or contains
        no insider paths.
        """
        settings_file = self.project_root / "installer_settings.json"
        if not settings_file.exists():
            return []

        try:
            raw: dict = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("detect_user_paths_inside_root: could not parse settings: %s", exc)
            return []

        # Fields in AppConfig that carry filesystem paths.
        PATH_KEYS = {
            "game_directory",
            "download_root",
            "cache_directory",
            "temp_directory",
            "browser_profile_dir",
            "assetstudio_cli_path",
            "vgmstream_path",
        }

        found: list[Path] = []
        root_resolved = self.project_root.resolve()

        for key, value in raw.items():
            if key not in PATH_KEYS:
                continue
            if not value or not isinstance(value, str):
                continue
            try:
                candidate = Path(value).expanduser()
                # Relative paths are resolved against the project root.
                if not candidate.is_absolute():
                    candidate = (self.project_root / candidate).resolve()
                else:
                    candidate = candidate.resolve()

                # Is it inside the project root?
                candidate.relative_to(root_resolved)  # raises ValueError if not
                found.append(candidate)
            except ValueError:
                pass  # not inside project root — safe to ignore
            except Exception as exc:
                logger.debug("detect_user_paths_inside_root: skipping %r: %s", value, exc)

        return found

    def _run_git(self, *args: str) -> Optional[str]:
        """Run a git command and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as exc:
            logger.debug("git %s failed: %s", " ".join(args), exc)
        return None

    def get_current_branch(self) -> str:
        """Return the currently tracked branch name."""
        if self.is_git_repo():
            branch = self._run_git("branch", "--show-current")
            if branch:
                return branch
            # Detached HEAD — try to read from state
        state = self._load_state()
        return state.get("tracked_branch", DEFAULT_BRANCH)

    def get_current_commit(self) -> str:
        """Return the current local commit SHA (short form)."""
        if self.is_git_repo():
            sha = self._run_git("rev-parse", "--short", "HEAD")
            if sha:
                return sha
        state = self._load_state()
        full = state.get("current_commit_sha", "unknown")
        # Truncate to short form (7 chars) to match git rev-parse --short
        return full[:7] if full != "unknown" else full

    def get_current_commit_full(self) -> str:
        """Return the full 40-char commit SHA."""
        if self.is_git_repo():
            sha = self._run_git("rev-parse", "HEAD")
            if sha:
                return sha
        state = self._load_state()
        return state.get("current_commit_sha", "unknown")

    # ----- GitHub API helpers -----------------------------------------------

    @staticmethod
    def _api_get(endpoint: str) -> dict | list:
        """Call the GitHub REST API and return parsed JSON."""
        url = f"{GITHUB_API_BASE}/{endpoint}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ----- queries -----------------------------------------------------------

    def fetch_remote_branches(self) -> list[str]:
        """Return the list of branch names from the remote repository."""
        try:
            data = self._api_get("branches?per_page=100")
            return sorted(b["name"] for b in data if isinstance(b, dict))
        except Exception as exc:
            logger.warning("Failed to fetch remote branches: %s", exc)
            return []

    def check_for_updates(self, branch: str | None = None) -> UpdateCheckResult:
        """Check whether the local copy is behind the remote branch tip.

        Parameters
        ----------
        branch:
            Branch to check against.  ``None`` means the currently tracked branch.
        """
        branch = branch or self.get_current_branch()
        local_commit = self.get_current_commit()
        local_commit_full = self.get_current_commit_full()
        is_git = self.is_git_repo()

        try:
            branch_data = self._api_get(f"branches/{quote(branch, safe='')}")
            tip = branch_data.get("commit", {})
            remote_sha = tip.get("sha", "")[:7]
            remote_sha_full = tip.get("sha", "")
            commit_info = tip.get("commit", {})
            remote_msg = commit_info.get("message", "").split("\n")[0]
            raw_date = (
                commit_info.get("committer", {}).get("date", "")
                or commit_info.get("author", {}).get("date", "")
            )
        except requests.HTTPError as exc:
            return UpdateCheckResult(
                is_up_to_date=True,
                local_commit=local_commit,
                remote_commit="",
                remote_commit_message="",
                remote_commit_date="",
                branch=branch,
                is_git_repo=is_git,
                error=f"GitHub API error: {exc}",
            )
        except Exception as exc:
            return UpdateCheckResult(
                is_up_to_date=True,
                local_commit=local_commit,
                remote_commit="",
                remote_commit_message="",
                remote_commit_date="",
                branch=branch,
                is_git_repo=is_git,
                error=f"Network error: {exc}",
            )

        # Compare
        if local_commit_full == "unknown" or not remote_sha_full:
            up_to_date = False
            behind = -1
        elif remote_sha_full.startswith(local_commit_full) or local_commit_full.startswith(remote_sha_full):
            up_to_date = True
            behind = 0
        else:
            up_to_date = False
            behind = self._count_commits_behind(local_commit_full, remote_sha_full)

        return UpdateCheckResult(
            is_up_to_date=up_to_date,
            local_commit=local_commit,
            remote_commit=remote_sha,
            remote_commit_message=remote_msg,
            remote_commit_date=raw_date,
            branch=branch,
            is_git_repo=is_git,
            commits_behind=behind,
        )

    def _count_commits_behind(self, local_sha: str, remote_sha: str) -> int:
        """Use the compare API to determine how many commits behind we are.

        Returns -1 if the comparison cannot be made (unknown SHAs, network
        error, or the GitHub compare endpoint returns an error).
        """
        # Guard: never call the API with placeholder / empty values.
        if not local_sha or local_sha == "unknown" or not remote_sha:
            logger.debug(
                "_count_commits_behind: skipping API call — invalid SHAs "
                "(local=%r, remote=%r)",
                local_sha,
                remote_sha,
            )
            return -1
        try:
            data = self._api_get(
                f"compare/{quote(local_sha, safe='')}...{quote(remote_sha, safe='')}"
            )
            return data.get("ahead_by", 0)
        except Exception:
            return -1  # unknown

    # ----- mutations ---------------------------------------------------------

    def switch_branch(self, branch: str) -> UpdateCheckResult:
        """Switch the tracked branch and check for updates on it.

        For git users this runs ``git fetch`` + ``git checkout``.
        For zip users this only updates the state file.
        The caller should follow up with ``perform_update()`` if the
        returned result indicates an available update.
        """
        if self.is_git_repo():
            self._run_git("fetch", "origin")
            checkout = self._run_git("checkout", branch)
            if checkout is None:
                # Try creating a local tracking branch
                self._run_git(
                    "checkout", "-b", branch, f"origin/{branch}"
                )

        # Persist new branch in state regardless of mode
        state = self._load_state()
        state["tracked_branch"] = branch
        self._save_state(state)

        return self.check_for_updates(branch)

    def perform_update(self, branch: str | None = None) -> UpdateResult:
        """Download and apply the latest code from the remote branch.

        Returns an ``UpdateResult`` describing what happened.
        """
        branch = branch or self.get_current_branch()
        old_commit = self.get_current_commit()

        if self.is_git_repo():
            return self._update_via_git(branch, old_commit)
        return self._update_via_zip(branch, old_commit)

    def _update_via_git(self, branch: str, old_commit: str) -> UpdateResult:
        """Update using local git CLI."""
        fetch_out = self._run_git("fetch", "origin")
        if fetch_out is None:
            return UpdateResult(
                success=False,
                method="git",
                old_commit=old_commit,
                new_commit=old_commit,
                error="git fetch failed",
            )

        current_branch = self._run_git("branch", "--show-current") or ""
        if current_branch != branch:
            co = self._run_git("checkout", branch)
            if co is None:
                co = self._run_git("checkout", "-b", branch, f"origin/{branch}")
            if co is None:
                return UpdateResult(
                    success=False,
                    method="git",
                    old_commit=old_commit,
                    new_commit=old_commit,
                    error=f"git checkout {branch} failed",
                )

        reset_out = self._run_git("reset", "--hard", f"origin/{branch}")
        if reset_out is None:
            return UpdateResult(
                success=False,
                method="git",
                old_commit=old_commit,
                new_commit=old_commit,
                error=f"git reset --hard origin/{branch} failed",
            )

        new_commit = self.get_current_commit()

        # Sync state file
        state = self._load_state()
        state["current_commit_sha"] = self.get_current_commit_full()
        state["tracked_branch"] = branch
        state["last_update_iso"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        return UpdateResult(
            success=True,
            method="git",
            old_commit=old_commit,
            new_commit=new_commit,
        )

    def _update_via_zip(self, branch: str, old_commit: str) -> UpdateResult:
        """Update by downloading and extracting the branch zip archive."""
        zip_url = (
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
            f"/archive/refs/heads/{quote(branch, safe='')}.zip"
        )

        try:
            resp = requests.get(zip_url, timeout=60, stream=True)
            resp.raise_for_status()
        except Exception as exc:
            return UpdateResult(
                success=False,
                method="zip",
                old_commit=old_commit,
                new_commit=old_commit,
                error=f"Download failed: {exc}",
            )

        # Write to a temp file, then extract
        try:
            tmp_dir = tempfile.mkdtemp(prefix="jd2021_update_")
            zip_path = Path(tmp_dir) / "update.zip"

            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp_dir)

            # GitHub zips contain a single root folder like
            # ``jd2021-map-installer-v2/``
            extracted_dirs = [
                d for d in Path(tmp_dir).iterdir()
                if d.is_dir() and d.name != "__MACOSX"
            ]
            if len(extracted_dirs) != 1:
                return UpdateResult(
                    success=False,
                    method="zip",
                    old_commit=old_commit,
                    new_commit=old_commit,
                    error="Unexpected zip structure",
                )

            source_root = extracted_dirs[0]

            # Dynamically expand the preserve set with any user-configured
            # paths (e.g. game_directory) that happen to live inside the
            # project root.  We only need the top-level directory name
            # because _merge_zip_contents matches against the first component.
            extra_preserve: set[str] = set()
            root_resolved = self.project_root.resolve()
            for insider in self.detect_user_paths_inside_root():
                try:
                    rel = insider.relative_to(root_resolved)
                    top = rel.parts[0].lower()
                    if top:
                        extra_preserve.add(top)
                        logger.info(
                            "Zip update: auto-preserving user path '%s' (top-level: '%s')",
                            insider,
                            top,
                        )
                except Exception:
                    pass

            self._merge_zip_contents(source_root, extra_preserve=extra_preserve)

        except Exception as exc:
            return UpdateResult(
                success=False,
                method="zip",
                old_commit=old_commit,
                new_commit=old_commit,
                error=f"Extract/merge failed: {exc}",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Determine new commit from the API (we just downloaded this branch)
        new_commit = old_commit
        try:
            branch_data = self._api_get(f"branches/{quote(branch, safe='')}")
            new_sha = branch_data["commit"]["sha"]
            new_commit = new_sha[:7]
            full_sha = new_sha
        except Exception:
            full_sha = new_commit

        state = self._load_state()
        state["current_commit_sha"] = full_sha
        state["tracked_branch"] = branch
        state["last_update_iso"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        return UpdateResult(
            success=True,
            method="zip",
            old_commit=old_commit,
            new_commit=new_commit,
        )

    def _merge_zip_contents(
        self,
        source_root: Path,
        *,
        extra_preserve: set[str] | None = None,
    ) -> None:
        """Replace project files with extracted zip contents, preserving user data.

        Parameters
        ----------
        source_root:
            The extracted archive directory whose contents replace the project.
        extra_preserve:
            Additional lowercase top-level names to protect on top of the
            static ``PRESERVE_PATHS`` set.  Used to shield user-configured
            directories (e.g. a game install inside the project root) that
            were discovered at runtime from ``installer_settings.json``.

        Uses an atomic move-then-restore strategy:

        1. Move all non-preserved items out of the project root into a
           temporary backup directory.
        2. Copy new files from the extracted archive into the project root.
        3. If step 2 raises, restore from the backup so the install is never
           left in a half-applied state.
        4. On success, discard the backup.
        """
        preserve_lower = {p.lower() for p in PRESERVE_PATHS}
        if extra_preserve:
            preserve_lower |= {p.lower() for p in extra_preserve}

        backup_dir = Path(tempfile.mkdtemp(prefix="jd2021_update_backup_"))
        # Tracks (project_root child path, backup copy path) for rollback.
        backed_up: list[tuple[Path, Path]] = []

        try:
            # --- Phase 1: Move old items to backup --------------------------
            for child in list(self.project_root.iterdir()):
                if child.name.lower() in preserve_lower:
                    continue
                backup_dest = backup_dir / child.name
                try:
                    shutil.move(str(child), str(backup_dest))
                    backed_up.append((child, backup_dest))
                except OSError as exc:
                    # Non-fatal: log and continue — the copy phase will
                    # overwrite the file anyway.
                    logger.warning("Could not move %s to backup: %s", child, exc)

            # --- Phase 2: Copy new files from archive -----------------------
            for child in source_root.iterdir():
                if child.name.lower() in preserve_lower:
                    continue
                dest = self.project_root / child.name
                if child.is_dir():
                    shutil.copytree(child, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, dest)

            # Restore custom local song databases (JDU, JDNext) from backup if they were moved
            backup_songdb = backup_dir / "assets" / "songdb"
            if backup_songdb.exists():
                dest_songdb = self.project_root / "assets" / "songdb"
                dest_songdb.mkdir(parents=True, exist_ok=True)
                for item in backup_songdb.iterdir():
                    shutil.copy2(item, dest_songdb / item.name)

        except Exception:
            # Rollback: restore every item that was successfully moved out.
            logger.warning(
                "Zip merge failed — rolling back %d item(s) to previous state",
                len(backed_up),
            )
            for original_path, backup_path in backed_up:
                try:
                    if backup_path.is_dir():
                        if original_path.exists():
                            shutil.rmtree(original_path, ignore_errors=True)
                        shutil.copytree(backup_path, original_path)
                    else:
                        shutil.copy2(backup_path, original_path)
                except Exception as restore_exc:
                    logger.error(
                        "Rollback failed for %s: %s", original_path, restore_exc
                    )
            raise

        finally:
            # Always discard the backup directory whether we succeeded or rolled back.
            shutil.rmtree(backup_dir, ignore_errors=True)
