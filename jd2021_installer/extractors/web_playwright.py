"""Playwright-based web extractor for JDU map assets.

Replaces the original Node.js JDH_Downloader script (fetch.mjs).
Uses playwright-python's async API to automate Discord slash commands
(``/assets`` and ``/nohud``) and download associated media files.

The extractor runs Playwright in a background thread (via ``asyncio.run()``)
so it can be safely dispatched from a QThread worker without blocking
the Qt event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import requests
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, cast
from urllib.parse import unquote, urlparse
from html import unescape

from jd2021_installer.core.config import (
    QUALITY_ORDER,
    QUALITY_PATTERNS,
    SCENE_PLATFORM_PREFERENCE,
    AppConfig,
)
from jd2021_installer.core.exceptions import DownloadError, WebExtractionError
from jd2021_installer.extractors.archive_ipk import extract_ipk
from jd2021_installer.extractors.base import BaseExtractor
from jd2021_installer.extractors.jdnext_bundle_strategy import (
    _run_assetstudio_export,
    run_jdnext_bundle_strategy,
)

logger = logging.getLogger("jd2021.extractors.web_playwright")

# SSL workaround for Ubisoft CDN
ssl._create_default_https_context = ssl._create_unverified_context

# Discord DOM selectors (must be updated if Discord changes its UI)
_SEL_TEXTBOX = '[role="textbox"][data-slate-editor="true"]'
_SEL_AUTOCOMPLETE_OPTION = '[role="option"]'
_SEL_MESSAGE_ACCESSORIES = 'div[id^="message-accessories-"]'
_SEL_MESSAGE_LIST_ITEMS = 'li[id^="chat-messages-"]'

# CDN link validation patterns
_UBI_CDN_HOST_PATTERN = re.compile(r"https?://[^\s\"']*(?:cdn\.ubi\.com|cdn\.ubisoft\.cn)", re.IGNORECASE)
_MAP_PATH_PATTERN = re.compile(r"/(?:public|private)/(?:map|jdnext/maps)/", re.IGNORECASE)
_WEBM_VALIDATION_CACHE: Dict[str, tuple[int, int, bool]] = {}
_webm_cache_lock = threading.Lock()
_UUID_LIKE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_browser_closed_error(exc: BaseException) -> bool:
    """Return True when Playwright indicates the page/context/browser was closed."""
    text = str(exc).lower()
    return (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
        or "target closed" in text
    )


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------

def extract_urls_from_html(html_content: str) -> List[str]:
    """Extract all URLs from HTML content, deduplicating.
    Finds links even outside of href attributes, useful for Discord embeds
    where the actual CDN link is plaintext inside an anchor tag.
    """
    urls = re.findall(r'(https?://[^\s<"\']+)', html_content)
    clean: Set[str] = set()
    for url in urls:
        if "discordapp.net" in url:
            continue
        # Strip trailing punctuation that might be captured from text around the URL
        url = url.rstrip(").,!;?")
        url = url.replace("&amp;", "&")
        clean.add(url)
    return list(clean)


def extract_urls_from_file(html_file: str | Path) -> List[str]:
    """Read an HTML file and extract URLs."""
    path = Path(html_file)
    if not path.is_file():
        raise FileNotFoundError(f"HTML file not found: {path}")
    content = path.read_text(encoding="utf-8")
    return extract_urls_from_html(content)


def get_filename_from_url(url: str) -> str:
    """Extract the filename from a URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    parts = path.split("/")
    if len(parts) >= 2 and "." in parts[-2]:
        return parts[-2]
    return parts[-1]


def extract_codename_from_urls(urls: List[str]) -> Optional[str]:
    """Extract map codename from JDU asset URLs.

    Supported patterns:
    - https://.../public/map/{MapName}/...

    Note: JDNext URLs often use opaque map IDs (UUID-like) under
    ``.../jdnext/maps/<id>/...``. Those IDs are not stable codenames and should
    not be used as map folder names.
    """
    for url in urls:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not parts:
            continue

        low_parts = [p.lower() for p in parts]

        # JDU: /public/map/<codename>/...
        for idx in range(len(parts) - 1):
            if low_parts[idx] == "map":
                candidate = parts[idx + 1].strip()
                if candidate:
                    return candidate

        # JDNext: /public|private/jdnext/maps/<id-or-codename>/...
        # Ignore UUID-like IDs so caller can fall back to requested codename
        # (Fetch/JDNext mode) or embed title (HTML mode).
        for idx in range(len(parts) - 2):
            if low_parts[idx] == "jdnext" and low_parts[idx + 1] == "maps":
                candidate = parts[idx + 2].strip()
                if candidate and not _UUID_LIKE_RE.match(candidate):
                    return candidate
    return None


def _is_valid_source_codename(candidate: str) -> bool:
    value = str(candidate or "").strip()
    if not value:
        return False
    if _UUID_LIKE_RE.match(value):
        return False
    # Codenames are safe path stems and should include at least one letter.
    if not re.match(r"^[A-Za-z0-9_-]+$", value):
        return False
    if not re.search(r"[A-Za-z]", value):
        return False
    if value.lower() in {"unknown", "unknownmap", "map", "jdnext"}:
        return False
    return True


def _infer_codename_from_source_files(source_root: Path) -> Optional[str]:
    """Infer codename from extracted source assets (no songdb/cache dependency)."""
    if not source_root.exists():
        return None

    # 1) JDNext mapPackage metadata (strongest signal)
    map_json_candidates = sorted(source_root.rglob("map.json"))
    for map_json in map_json_candidates:
        try:
            payload = json.loads(map_json.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        sd = payload.get("SongDesc")
        if isinstance(sd, dict):
            map_name = str(sd.get("MapName", "") or "").strip()
            if _is_valid_source_codename(map_name):
                return map_name

        for key in ("MapName", "mapName"):
            map_name = str(payload.get(key, "") or "").strip()
            if _is_valid_source_codename(map_name):
                return map_name

    # 2) SongDesc CKD/JSON payload
    for songdesc in sorted(source_root.rglob("*songdesc*.tpl.ckd")):
        try:
            raw_text = songdesc.read_text(encoding="utf-8", errors="ignore").replace("\x00", "")
            payload = json.loads(raw_text)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        comps = payload.get("COMPONENTS")
        if isinstance(comps, list) and comps:
            first = comps[0]
            if isinstance(first, dict):
                map_name = str(first.get("MapName", "") or "").strip()
                if _is_valid_source_codename(map_name):
                    return map_name

    # 3) MAIN_SCENE filename stem
    for scene in sorted(source_root.rglob("*_MAIN_SCENE.isc")):
        stem = scene.stem
        if stem.upper().endswith("_MAIN_SCENE"):
            candidate = stem[:-11]
            if _is_valid_source_codename(candidate):
                return candidate

    # 4) Music track filename stems
    suffixes = ("_musictrack", "_musictrack.tpl", "_musictrack.tpl.ckd")
    music_candidates = list(source_root.rglob("*.trk")) + list(source_root.rglob("*musictrack*.tpl.ckd"))
    for path in sorted(music_candidates):
        candidate = path.stem
        for suffix in suffixes:
            if candidate.lower().endswith(suffix):
                candidate = candidate[: -len(suffix)]
                break
        if _is_valid_source_codename(candidate):
            return candidate

    # 5) Timeline move prefix heuristic (weak signal fallback)
    prefix_count: Dict[str, int] = {}
    for move in source_root.rglob("*"):
        if not move.is_file() or move.suffix.lower() not in {".gesture", ".msm"}:
            continue
        stem = move.stem
        if "_" not in stem:
            continue
        prefix = stem.split("_", 1)[0]
        if not _is_valid_source_codename(prefix):
            continue
        key = prefix
        prefix_count[key] = prefix_count.get(key, 0) + 1

    if prefix_count:
        return max(prefix_count.items(), key=lambda item: item[1])[0]

    return None


def _extract_embed_title_from_html(html_content: str) -> Optional[str]:
    match = re.search(r"<div class=\"embedTitle[^\"]*\">\s*<span>([^<]+)</span>", html_content, re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1).strip()
    # Keep filesystem-safe codename characters only.
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "", raw)
    return safe or None


def _parse_retry_after_seconds(header_value: Optional[str], fallback: int) -> int:
    """Parse Retry-After header as seconds, returning a safe fallback on failure."""
    if not header_value:
        return max(1, int(fallback))
    try:
        return max(1, int(header_value.strip()))
    except (TypeError, ValueError):
        return max(1, int(fallback))


def _is_nohud_video_url(url: str) -> bool:
    """Return True for NOHUD gameplay video URLs from private map CDN paths."""
    parsed = urlparse(url)
    path_low = parsed.path.lower()
    if "/private/" not in path_low:
        return False
    if "/map/" not in path_low and "/maps/" not in path_low:
        return False
    name_low = get_filename_from_url(url).lower()
    if not name_low.endswith(".webm"):
        return False
    if "mappreview" in name_low or "videopreview" in name_low:
        return False
    return True


def _is_valid_webm_file(path: Path, config: AppConfig) -> bool:
    """Validate webm integrity.

    Prefers ffmpeg null-decode for robust corruption detection. Falls back to
    EBML magic check if ffmpeg is unavailable.

    Thread-safe: cache reads and writes are protected by ``_webm_cache_lock``.
    """
    try:
        if not path.exists():
            return False
        st = path.stat()
        if st.st_size <= 1024:
            return False
    except OSError:
        return False

    try:
        cache_key = str(path.resolve())
    except OSError:
        cache_key = str(path)

    with _webm_cache_lock:
        cached = _WEBM_VALIDATION_CACHE.get(cache_key)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached[2]

    def _cache_result(result: bool) -> bool:
        with _webm_cache_lock:
            _WEBM_VALIDATION_CACHE[cache_key] = (st.st_size, st.st_mtime_ns, result)
        return result

    try:
        with open(path, "rb") as fh:
            header = fh.read(4)
        if header != b"\x1a\x45\xdf\xa3":
            return _cache_result(False)
    except OSError:
        return _cache_result(False)

    cmd = [
        config.ffmpeg_path,
        "-v", "error",
        "-i", str(path),
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(15, min(config.download_timeout_s, 180)),
            check=False,
        )
        if proc.returncode != 0:
            return _cache_result(False)
        if proc.stderr and proc.stderr.strip():
            return _cache_result(False)
        return _cache_result(True)
    except (FileNotFoundError, OSError):
        logger.debug("ffmpeg not available for integrity check; using header-only validation.")
        return _cache_result(True)
    except subprocess.TimeoutExpired:
        logger.debug("Timed out while validating webm integrity for %s", path.name)
        return _cache_result(False)


def _download_with_powershell(url: str, target: Path, timeout_s: int) -> bool:
    """Fallback downloader for Windows environments where Python DNS fails.

    Uses Invoke-WebRequest in a separate PowerShell process.
    """
    if platform.system().lower() != "windows":
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        f"Invoke-WebRequest -Uri '{url}' -OutFile '{str(target)}' -TimeoutSec {int(timeout_s)}"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0 and target.exists() and target.stat().st_size > 1024
    except (OSError, ValueError):
        return False


def _resolve_host_via_public_dns(host: str) -> Optional[str]:
    """Resolve host using public DNS via PowerShell to avoid local ISP DNS issues."""
    if platform.system().lower() != "windows":
        return None
    cmd = (
        "try { "
        f"$r = Resolve-DnsName '{host}' -Type A -Server 1.1.1.1 -ErrorAction Stop | "
        "Where-Object { $_.Type -eq 'A' } | Select-Object -First 1 -ExpandProperty IPAddress; "
        "if ($r) { Write-Output $r } "
        "} catch { }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        ip = (completed.stdout or "").strip()
        return ip or None
    except OSError:
        return None


def _download_with_curl_resolve(url: str, target: Path, timeout_s: int) -> bool:
    """Use curl --resolve to bypass system DNS for a specific host."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False
    ip = _resolve_host_via_public_dns(host)
    if not ip:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                "curl.exe",
                "--location",
                "--silent",
                "--show-error",
                "--connect-timeout",
                str(max(5, int(timeout_s))),
                "--max-time",
                str(max(10, int(timeout_s) * 2)),
                "--resolve",
                f"{host}:443:{ip}",
                "--output",
                str(target),
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0 and target.exists() and target.stat().st_size > 1024
    except OSError:
        return False


def _download_single_file(url: str, target: Path, config: AppConfig) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            stream=True,
            timeout=config.download_timeout_s,
            headers={"User-Agent": config.user_agent, "Referer": "https://discord.com/"},
        ) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return target.exists() and target.stat().st_size > 1024
    except Exception:
        return False


def _try_jdnext_missing_fallbacks(
    *,
    all_urls: List[str],
    classified: Dict[str, object],
    downloaded: Dict[str, str],
    download_dir: Path,
    config: AppConfig,
) -> None:
    """Best-effort recovery for JDNext when private links fail in this environment.

    - mapPackage: try cached local mapPackage bundle from temp/jdnext_downloads.
    Preview audio/video are intentionally excluded.
    """
    expected_video = cast(Optional[str], classified.get("video"))
    expected_audio = cast(Optional[str], classified.get("audio"))
    expected_scene = cast(Optional[str], classified.get("mainscene"))

    _ = expected_video
    _ = expected_audio

    if expected_scene:
        expected_scene_name = get_filename_from_url(expected_scene)
        if expected_scene_name not in downloaded:
            local_cache = Path(__file__).resolve().parents[2] / "temp" / "jdnext_downloads"
            candidates = []
            if local_cache.exists():
                candidates.extend(local_cache.glob("*mapPackage*.bundle"))
                candidates.extend(local_cache.glob("*_mapPackage.bundle"))
                candidates.extend(local_cache.glob("*.bundle"))
            for cand in candidates:
                try:
                    if cand.is_file() and cand.stat().st_size > 1024:
                        target = download_dir / expected_scene_name
                        shutil.copy2(cand, target)
                        downloaded[expected_scene_name] = str(target)
                        logger.debug("JDNext fallback: reusing local bundle cache for %s", expected_scene_name)
                        break
                except OSError:
                    continue


def _extract_jdnext_aux_texture_bundles(
    *,
    download_dir: Path,
    extract_dir: Path,
    mainscene_name: str,
    codename: str,
    config: Optional[AppConfig] = None,
) -> int:
    """Decode JDNext non-mapPackage bundles and copy loose texture payloads.

    These bundles usually contain Cover/Coach/Title/background images that are
    not present in mapPackage payloads.
    """
    copied = 0
    strategy_aux_root = extract_dir / "jdnext_strategy" / "aux_bundles"
    menuart_root = extract_dir / "menuart"
    menuart_root.mkdir(parents=True, exist_ok=True)

    mainscene_name_low = mainscene_name.lower()
    for bundle_path in sorted(download_dir.glob("*.bundle")):
        if bundle_path.name.lower() == mainscene_name_low:
            continue

        export_dir = strategy_aux_root / bundle_path.stem
        try:
            _run_assetstudio_export(bundle_path, export_dir, "2021.3.9f1", config=config)
        except Exception as exc:
            logger.debug("JDNext aux bundle export failed (%s): %s", bundle_path.name, exc)
            continue

        for asset_dir_name in ("Texture2D", "Sprite"):
            asset_dir = export_dir / asset_dir_name
            if not asset_dir.exists():
                continue

            for src in asset_dir.glob("*.png"):
                dst_name = src.name
                if not dst_name.lower().startswith(codename.lower() + "_") and dst_name.lower().startswith(("cover", "coach", "banner", "map_", "title")):
                    dst_name = f"{codename}_{dst_name}"
                dst = menuart_root / dst_name
                if dst.exists():
                    continue
                shutil.copy2(src, dst)
                copied += 1

    if copied:
        logger.debug("JDNext aux bundle texture import: copied %d texture(s)", copied)
    return copied


# ---------------------------------------------------------------------------
# File downloader
# ---------------------------------------------------------------------------

def _build_quality_search_order(
    selected_quality: str,
    is_jdnext_url_set: bool,
    config: Optional[AppConfig] = None,
) -> List[str]:
    """Return an ordered list of quality keys to probe for the selected tier.

    Promoted from an inner function inside ``_classify_urls`` so that the
    quality-ladder logic can be unit-tested and reasoned about independently.

    Args:
        selected_quality:  The user's preferred quality key (e.g. ``'ULTRA_HD'``).
        is_jdnext_url_set: True when the URL set contains JDNext-style links, which
                           enables the ``fallback_compatible_down`` VP9-avoidance path.
        config:            App configuration (used for ``vp9_handling_mode``).
    """
    tiers = ["ULTRA", "HIGH", "MID", "LOW"]
    selected = (selected_quality or "ULTRA_HD").upper()
    cfg = config or AppConfig()
    vp9_mode = getattr(cfg, "vp9_handling_mode", "reencode_to_vp8")

    if selected.endswith("_HD"):
        selected_tier = selected[:-3]
        selected_is_hd = True
    else:
        selected_tier = selected
        selected_is_hd = False

    if selected_tier not in tiers:
        selected_tier = "ULTRA"

    # Compatibility mode: avoid VP9 tiers entirely, pick the next HD tier down.
    if vp9_mode == "fallback_compatible_down" and is_jdnext_url_set:
        start_idx = tiers.index(selected_tier)
        if not selected_is_hd:
            start_idx = min(start_idx + 1, len(tiers) - 1)
        return [f"{tier}_HD" for tier in tiers[start_idx:]]

    prefer_hd_first = selected_is_hd

    start_idx = tiers.index(selected_tier)
    ordered_tiers = tiers[start_idx:]

    order: List[str] = []
    for tier in ordered_tiers:
        if prefer_hd_first:
            order.extend([f"{tier}_HD", tier])
        else:
            order.extend([tier, f"{tier}_HD"])

    # Ensure uniqueness while preserving order
    deduped: List[str] = []
    for item in order:
        if item in QUALITY_ORDER and item not in deduped:
            deduped.append(item)
    return deduped


def _classify_urls(
    urls: List[str], quality: str, config: Optional[AppConfig] = None
) -> Dict[str, object]:
    """Classify URLs into video, audio, mainscene, and other assets."""
    jdnext_video_re = re.compile(r"/video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm/", re.IGNORECASE)

    video_urls_by_quality: Dict[str, str] = {}
    jdnext_variant_by_quality: Dict[str, str] = {}
    audio_url: Optional[str] = None
    scene_zips: Dict[str, str] = {}
    other_urls: List[str] = []
    is_jdnext_url_set = False

    for u in urls:
        u_low = u.lower()
        if "/jdnext/maps/" in u_low:
            is_jdnext_url_set = True
        if any(token in u_low for token in ("audiopreview", "videopreview", "mappreview")):
            continue

        jdnext_q: Optional[str] = None
        jdnext_variant: Optional[str] = None
        m = jdnext_video_re.search(u)
        if m:
            tier = m.group(1).upper()
            variant = m.group(2).lower()
            jdnext_variant = variant
            # Some mirrored/private links omit '/jdnext/maps/' but still expose
            # JDNext-style video_*.(hd|vp8|vp9).webm variants. Treat VP9 gameplay
            # variants as JDNext-compatible so fallback_compatible_down is applied.
            if variant == "vp9":
                is_jdnext_url_set = True
            # JDNext tier mapping:
            # - *_HD tiers use .hd variants
            # - non-HD tiers use .vp9 variants (later converted to VP8 on install)
            # - .vp8 is treated as fallback for *_HD only
            if variant == "hd":
                jdnext_q = f"{tier}_HD"
            elif variant == "vp9":
                jdnext_q = tier
            elif variant == "vp8":
                jdnext_q = f"{tier}_HD"
        for q, pattern in QUALITY_PATTERNS.items():
            if pattern in u:
                video_urls_by_quality[q] = u
                break
        if jdnext_q:
            existing_variant = jdnext_variant_by_quality.get(jdnext_q)
            if existing_variant is None:
                video_urls_by_quality[jdnext_q] = u
                if jdnext_variant:
                    jdnext_variant_by_quality[jdnext_q] = jdnext_variant
            elif jdnext_variant == "hd" and existing_variant == "vp8":
                # Prefer .hd when both .hd and .vp8 are present for the HD slot.
                video_urls_by_quality[jdnext_q] = u
                jdnext_variant_by_quality[jdnext_q] = jdnext_variant

        # JDN video: Chromecast/SneakPeak video
        if "jdn-mp4" in u_low or "jdn-sneakpeak" in u_low or "chromecast" in u_low or "sneakpeak" in u_low:
            if "jdn-mp4" in u_low or "chromecast" in u_low:
                video_urls_by_quality["MID"] = u
            else:
                video_urls_by_quality["LOW"] = u

        if (
            (
                ".ogg" in u
                or ".opus" in u
                or ".mp3" in u
            )
            and "audiopreview" not in u.lower()
        ):
            audio_url = u
        elif "MAIN_SCENE" in u and ".zip" in u:
            for plat in ["X360", "DURANGO", "SCARLETT", "NX", "ORBIS", "PROSPERO", "PC", "GGP", "WIIU"]:
                if f"MAIN_SCENE_{plat}" in u:
                    scene_zips[plat] = u
                    break
        elif "mappackage" in u.lower() and ".bundle" in u.lower():
            scene_zips["MAP_PACKAGE"] = u
        elif "bundle.zip" in u_low:
            scene_zips["JDN_BUNDLE"] = u
        elif any(ext in u.lower() for ext in (".ckd", ".jpg", ".jpeg", ".png", ".ad", ".bundle", ".opus", ".zip", ".json")):
            if ".ckd" in u.lower() or ".ad" in u.lower() or ".json" in u.lower() or ".zip" in u.lower() or ("discordapp.net" not in u):
                other_urls.append(u)

    # Select best video using the promoted quality-search-order helper.
    video_url = None
    search_order = _build_quality_search_order(quality, is_jdnext_url_set, config)
    for q in search_order:
        if q in video_urls_by_quality:
            video_url = video_urls_by_quality[q]
            break

    # Select best mainscene/map package
    main_scene_url = None
    for plat in SCENE_PLATFORM_PREFERENCE:
        if plat in scene_zips:
            main_scene_url = scene_zips[plat]
            break
    if not main_scene_url and "MAP_PACKAGE" in scene_zips:
        main_scene_url = scene_zips["MAP_PACKAGE"]
    if not main_scene_url and "JDN_BUNDLE" in scene_zips:
        main_scene_url = scene_zips["JDN_BUNDLE"]
    if not main_scene_url and scene_zips:
        main_scene_url = next(iter(scene_zips.values()))

    return {
        "video": video_url,
        "audio": audio_url,
        "mainscene": main_scene_url,
        "others": other_urls,
    }


# ---------------------------------------------------------------------------
# Thread-local session + per-URL worker  (supports ThreadPoolExecutor)
# ---------------------------------------------------------------------------

_thread_local_sessions = threading.local()


def _get_thread_session(config: AppConfig) -> requests.Session:
    """Return a thread-local :class:`requests.Session`, creating one on first access.

    Using per-thread sessions avoids the data races that arise when a single
    shared session is used from multiple threads simultaneously.
    """
    if not hasattr(_thread_local_sessions, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.user_agent,
            "Referer": "https://discord.com/",
        })
        _thread_local_sessions.session = s
    return _thread_local_sessions.session


def _download_url_worker(
    url: str,
    download_path: Path,
    config: AppConfig,
    game_map_dir: Optional[Path],
) -> tuple[str, Optional[str]]:
    """Download a single URL to ``download_path``.

    Returns ``(filename, local_path_str)`` on success or
    ``(filename, None)`` on failure.  Handles retry logic, integrity
    checks, and DNS fallbacks internally so it can be submitted to a
    :class:`~concurrent.futures.ThreadPoolExecutor` without shared state.
    """
    fname = get_filename_from_url(url)
    target = download_path / fname
    is_nohud_video = _is_nohud_video_url(url)

    # --- Local cache check ---
    if target.exists() and target.stat().st_size > 1024:
        if is_nohud_video and not _is_valid_webm_file(target, config):
            logger.debug("Cached NOHUD video %s is corrupt; redownloading...", fname)
            target.unlink(missing_ok=True)
        else:
            logger.debug("%s already in cache, skipping download.", fname)
            return fname, str(target)

    if target.exists():
        target.unlink()

    # --- Game installation reuse ---
    if game_map_dir and game_map_dir.exists():
        for fpath in game_map_dir.rglob(fname):
            if fpath.is_file() and fpath.stat().st_size > 1024:
                logger.debug("Found %s in game installation; copying to cache...", fname)
                shutil.copy2(fpath, target)
                if is_nohud_video and not _is_valid_webm_file(target, config):
                    logger.debug(
                        "Installed NOHUD video %s failed integrity check; redownloading...",
                        fname,
                    )
                    target.unlink(missing_ok=True)
                else:
                    return fname, str(target)
                break

    # --- Primary: curl --resolve for known CDN mirror ---
    prefer_curl_resolve = "cdn-jdhelper.ramaprojects.ru" in url.lower()
    if prefer_curl_resolve:
        logger.debug("Using curl --resolve as primary downloader for %s", fname)
        if _download_with_curl_resolve(url, target, config.download_timeout_s):
            if is_nohud_video and not _is_valid_webm_file(target, config):
                logger.debug("curl --resolve primary produced corrupt NOHUD video %s", fname)
                target.unlink(missing_ok=True)
            else:
                time.sleep(config.inter_request_delay_s)
                return fname, str(target)

    # --- Main retry loop via thread-local requests.Session ---
    session = _get_thread_session(config)
    success = False
    for attempt in range(1, config.max_retries + 1):
        try:
            with session.get(url, stream=True, timeout=config.download_timeout_s) as r:
                if r.status_code == 429:
                    retry_after = _parse_retry_after_seconds(
                        r.headers.get("Retry-After"),
                        config.retry_base_delay_s * attempt,
                    )
                    logger.warning(
                        "Rate limited (429) for %s. Waiting %ds before retry %d/%d...",
                        fname, retry_after, attempt, config.max_retries,
                    )
                    if attempt < config.max_retries:
                        time.sleep(retry_after)
                        continue
                    else:
                        break

                if r.status_code in (403, 404):
                    logger.warning(
                        "HTTP %d for %s (links may have expired).",
                        r.status_code,
                        fname,
                    )
                    break

                r.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

                if target.stat().st_size > 1024:
                    if is_nohud_video and not _is_valid_webm_file(target, config):
                        logger.warning(
                            "Downloaded NOHUD video %s failed integrity check (attempt %d/%d)",
                            fname, attempt, config.max_retries,
                        )
                        target.unlink(missing_ok=True)
                        if attempt < config.max_retries:
                            time.sleep(config.retry_base_delay_s * (2 ** (attempt - 1)))
                            continue
                        break
                    success = True
                    break
                else:
                    logger.debug("Download produced empty file for %s", fname)
        except Exception as e:
            logger.debug(
                "Download error for %s: %s (Attempt %d/%d)",
                fname, e, attempt, config.max_retries,
            )
            if attempt < config.max_retries:
                time.sleep(config.retry_base_delay_s * (2 ** (attempt - 1)))
            else:
                break

    if success:
        time.sleep(config.inter_request_delay_s)
        return fname, str(target)

    # --- Fallbacks for prefer_curl_resolve hosts ---
    if prefer_curl_resolve:
        logger.debug("Trying curl --resolve fallback download for %s", fname)
        if _download_with_curl_resolve(url, target, config.download_timeout_s):
            if is_nohud_video and not _is_valid_webm_file(target, config):
                logger.debug("curl --resolve fallback produced corrupt NOHUD video %s", fname)
                target.unlink(missing_ok=True)
            else:
                logger.info("curl --resolve fallback succeeded for %s", fname)
                time.sleep(config.inter_request_delay_s)
                return fname, str(target)

        logger.debug("Trying PowerShell fallback download for %s", fname)
        if _download_with_powershell(url, target, config.download_timeout_s):
            if is_nohud_video and not _is_valid_webm_file(target, config):
                logger.debug("PowerShell fallback produced corrupt NOHUD video %s", fname)
                target.unlink(missing_ok=True)
            else:
                logger.info("PowerShell fallback succeeded for %s", fname)
                time.sleep(config.inter_request_delay_s)
                return fname, str(target)

    logger.error("Failed to download %s after %d attempts", fname, config.max_retries)
    return fname, None


def download_files(
    urls: List[str],
    download_dir: str | Path,
    quality: str = "ULTRA_HD",
    config: Optional[AppConfig] = None,
    progress_callback=None,
) -> Dict[str, str]:
    """Download map asset files from URLs concurrently.

    Uses a :class:`~concurrent.futures.ThreadPoolExecutor` with up to 4
    workers so that audio, video, scene-ZIP, and CKD assets are fetched in
    parallel rather than sequentially.  Each worker uses its own
    thread-local :class:`requests.Session`.

    Args:
        urls:              List of asset URLs.
        download_dir:      Directory to save files to.
        quality:           Preferred video quality tier.
        config:            App configuration (for timeouts, etc.).
        progress_callback: Optional callable(filename, current, total).

    Returns:
        Dict mapping filename → local path for downloaded files.
    """
    if config is None:
        config = AppConfig()

    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)

    classified = _classify_urls(urls, quality, config)
    important_urls: List[str] = []
    for key in ("video", "audio", "mainscene"):
        value = cast(Optional[str], classified.get(key))
        if value:
            important_urls.append(value)
    important_urls.extend(cast(List[str], classified.get("others", [])))

    unique_urls = list(set(important_urls))

    # Prioritize: mainscene > audio > video > others so the pool starts
    # the most critical downloads first when max_workers < total.
    def priority(u: str) -> int:
        if "MAIN_SCENE" in u or "mappackage" in u.lower():
            return 0
        if ".ogg" in u or ".wav" in u or ".opus" in u:
            return 1
        if any(pat in u for pat in QUALITY_PATTERNS.values()):
            return 2
        if re.search(r"/video_(ultra|high|mid|low)\.(hd|vp9|vp8)\.webm/", u, re.IGNORECASE):
            return 2
        return 3

    unique_urls.sort(key=priority)

    # Resolve game map directory once (shared read-only across all workers).
    codename = download_path.name
    game_map_dir: Optional[Path] = None
    if config.game_directory:
        base_game_dir = config.game_directory
        while base_game_dir.name.lower() in ("world", "data"):
            base_game_dir = base_game_dir.parent
        game_map_dir = base_game_dir / "data" / "World" / "MAPS" / codename

    downloaded: Dict[str, str] = {}
    total = len(unique_urls)
    _progress_lock = threading.Lock()
    _completed = [0]  # mutable counter accessible from the result loop

    max_workers = min(4, max(1, total))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_download_url_worker, url, download_path, config, game_map_dir): url
            for url in unique_urls
        }
        for future in as_completed(futures):
            try:
                fname, path = future.result()
            except Exception as exc:
                url = futures[future]
                fname = get_filename_from_url(url)
                logger.error("Unexpected download error for %s: %s", fname, exc)
                path = None

            with _progress_lock:
                _completed[0] += 1
                if path:
                    downloaded[fname] = path
                if progress_callback:
                    progress_callback(fname, _completed[0], total)

    return downloaded


# ---------------------------------------------------------------------------
# Discord automation helpers  (ported from V1 fetch.mjs)
# ---------------------------------------------------------------------------

async def _wait_for_login(page, timeout_s: int = 300) -> None:
    """Wait for Discord login — detects the chat textbox."""
    textbox = page.locator(_SEL_TEXTBOX)
    try:
        await textbox.wait_for(timeout=15_000)
        logger.debug("Already logged in to Discord.")
    except Exception as exc:
        if _is_browser_closed_error(exc):
            raise WebExtractionError("Browser was closed by user. Fetch cancelled.") from exc
        logger.info(
            "Please log in to Discord in the browser window. "
            "Waiting up to %d seconds...", timeout_s
        )
        try:
            await textbox.wait_for(timeout=timeout_s * 1000)
        except Exception as inner_exc:
            if _is_browser_closed_error(inner_exc):
                raise WebExtractionError("Browser was closed by user. Fetch cancelled.") from inner_exc
            raise
        logger.debug("Login detected.")
        await page.wait_for_timeout(3000)


async def _get_last_accessory_id(page) -> Optional[str]:
    """Return the DOM id of the last message-accessories element."""
    accessories = page.locator(_SEL_MESSAGE_ACCESSORIES)
    count = await accessories.count()
    if count == 0:
        return None
    return await accessories.nth(count - 1).get_attribute("id")


async def _get_last_message_id(page) -> Optional[str]:
    """Return the DOM id of the last message list item."""
    messages = page.locator(_SEL_MESSAGE_LIST_ITEMS)
    count = await messages.count()
    if count == 0:
        return None
    return await messages.nth(count - 1).get_attribute("id")


async def _wait_for_new_message(
    page, previous_last_message_id: Optional[str], timeout_s: int = 60
) -> str:
    """Poll for a newly appended message list item."""
    logger.debug("Waiting for bot message response...")
    deadline = asyncio.get_running_loop().time() + timeout_s

    while asyncio.get_running_loop().time() < deadline:
        result = await page.evaluate(
            """(prevId) => {
                const all = document.querySelectorAll('li[id^="chat-messages-"]');
                if (all.length === 0) return null;
                const list = Array.from(all);
                let start = 0;
                if (prevId) {
                    const idx = list.findIndex((el) => el.id === prevId);
                    if (idx >= 0) start = idx + 1;
                }

                for (let i = start; i < list.length; i++) {
                    const candidate = list[i];
                    const textContent = candidate.textContent || '';
                    const hasContent = !!candidate.querySelector('[id^="message-content-"]');
                    const hasAccessories = !!candidate.querySelector('[id^="message-accessories-"]');
                    const isLoading = textContent.includes('Loading');

                    if (isLoading) return null; // Block and wait for it to load

                    if (hasContent || hasAccessories) {
                        return {
                            id: candidate.id,
                            hasContent,
                            hasAccessories,
                            isLoading: false,
                        };
                    }
                }
                return null;
            }""",
            previous_last_message_id,
        )

        if result:
            stable = True
            for _ in range(3):
                await page.wait_for_timeout(350)
                latest = await page.evaluate(
                    """(targetId) => {
                        const el = document.getElementById(targetId);
                        if (!el) return null;
                        return {
                            id: el.id,
                            isLoading: (el.textContent || '').includes('Loading'),
                        };
                    }""",
                    result["id"],
                )
                if not latest or latest["id"] != result["id"] or latest["isLoading"]:
                    stable = False
                    break
            if stable:
                logger.debug("Bot message response detected (%s).", result["id"])
                return result["id"]

        await page.wait_for_timeout(400)

    raise WebExtractionError(
        "Timed out waiting for bot message response. "
        "The bot might be offline or the button interaction failed."
    )


async def _extract_message_payload(page, message_id: str) -> Dict[str, str]:
    """Extract message content/accessories payload for a message list item."""
    payload = await page.evaluate(
        """(msgId) => {
            const root = document.getElementById(msgId);
            if (!root) return null;
            const content = root.querySelector('[id^="message-content-"]');
            const accessories = root.querySelector('[id^="message-accessories-"]');
            return {
                message_id: msgId,
                content_html: content ? content.innerHTML : '',
                content_text: content ? (content.textContent || '') : '',
                accessories_html: accessories ? accessories.outerHTML : '',
                combined_html: root.outerHTML,
            };
        }""",
        message_id,
    )
    if not payload:
        raise WebExtractionError(f"Could not extract payload for message id: {message_id}")
    return cast(Dict[str, str], payload)


async def _send_slash_command(
    page, *, command: str, choices: List[str], codename: str
) -> None:
    """Automate the Discord slash-command picker UI.

    1. Focus textbox, type ``/<command>``
    2. Click matching autocomplete option
    3. Click each ``choice`` (e.g. "jdu") in dropdown params
    4. Type the codename, press Enter
    """
    textbox = page.locator(_SEL_TEXTBOX)
    await textbox.click()
    await page.wait_for_timeout(200)

    # Type command
    await page.keyboard.type(f"/{command}", delay=30)

    cmd_option = (
        page.locator(_SEL_AUTOCOMPLETE_OPTION)
        .filter(has_text=re.compile(command, re.IGNORECASE))
        .first
    )
    try:
        await cmd_option.wait_for(timeout=8000)
        await cmd_option.click()
        logger.debug("Selected /%s command.", command)
    except Exception:
        raise WebExtractionError(
            f"Could not find /{command} in the autocomplete. "
            "Make sure the bot is in this server and the command exists."
        )

    await page.wait_for_timeout(300)

    # Handle dropdown choices (e.g. game = "jdu")
    for choice in choices:
        choice_option = (
            page.locator(_SEL_AUTOCOMPLETE_OPTION)
            .filter(has_text=re.compile(rf"^\s*{re.escape(choice)}\s*$", re.IGNORECASE))
            .first
        )
        try:
            await choice_option.wait_for(timeout=8000)
            await choice_option.click()
            logger.debug("Selected choice: %s", choice)
        except Exception:
            # Looser match fallback
            loose = (
                page.locator(_SEL_AUTOCOMPLETE_OPTION)
                .filter(has_text=choice)
                .first
            )
            try:
                await loose.wait_for(timeout=3000)
                await loose.click()
                logger.debug("Selected choice (loose): %s", choice)
            except Exception:
                raise WebExtractionError(
                    f'Could not find "{choice}" in the parameter options.'
                )
        await page.wait_for_timeout(200)

    # Type codename and send
    await page.keyboard.type(codename, delay=20)
    logger.debug("Typed codename: %s", codename)
    await page.wait_for_timeout(200)
    await page.keyboard.press("Enter")
    logger.debug("Command sent.")


async def _wait_for_new_embed(
    page,
    previous_last_id: Optional[str],
    timeout_s: int = 60,
) -> str:
    """Poll for a new message-accessories element (the bot's response).

    Waits until the element is stable (no "Loading" text, has children)
    for 3 consecutive checks.
    """
    logger.debug("Waiting for bot response...")
    deadline = asyncio.get_running_loop().time() + timeout_s

    while asyncio.get_running_loop().time() < deadline:
        result = await page.evaluate(
            """(prevId) => {
                const all = document.querySelectorAll('div[id^="message-accessories-"]');
                if (all.length === 0) return null;
                const list = Array.from(all);
                let start = 0;
                if (prevId) {
                    const idx = list.findIndex((el) => el.id === prevId);
                    if (idx >= 0) start = idx + 1;
                }

                for (let i = start; i < list.length; i++) {
                    const candidate = list[i];
                    const textContent = candidate.textContent || '';
                    const hasChildren = candidate.children.length > 0;
                    const isLoading = textContent.includes('Loading');
                    
                    if (isLoading) return null; // Block and wait for it to load
                    if (!hasChildren) continue; // Skip empty wrappers

                    return {
                        id: candidate.id,
                        hasChildren,
                        isLoading,
                    };
                }

                return null;
            }""",
            previous_last_id,
        )

        if result:
            # Stability check: 3 × 500 ms
            stable = True
            for _ in range(3):
                await page.wait_for_timeout(500)
                latest = await page.evaluate(
                    """(targetId) => {
                        const el = document.getElementById(targetId);
                        if (!el) return null;
                        const tc = el.textContent || '';
                        return {
                            id: el.id,
                            hasChildren: el.children.length > 0,
                            isLoading: tc.includes('Loading'),
                        };
                    }""",
                    result["id"],
                )
                if (
                    not latest
                    or latest["id"] != result["id"]
                    or not latest["hasChildren"]
                    or latest["isLoading"]
                ):
                    stable = False
                    break
            if stable:
                logger.debug("Bot response detected (%s).", result["id"])
                return result["id"]

        await page.wait_for_timeout(500)

    raise WebExtractionError(
        "Timed out waiting for the bot response. "
        "The bot might be offline or the command may have failed."
    )


async def _extract_embed_html(page, accessory_id: str) -> str:
    """Extract the outerHTML of the identified embed element."""
    await page.wait_for_timeout(1500)  # Let rendering finish

    html = await page.evaluate(
        """(id) => {
            const el = document.getElementById(id);
            if (!el) return null;
            const message = el.closest('li[id^="chat-messages-"]');
            return message ? message.outerHTML : el.outerHTML;
        }""",
        accessory_id,
    )
    if not html:
        raise WebExtractionError(
            f'Could not find element with id "{accessory_id}" in the DOM.'
        )
    return html


def _has_valid_cdn_links(html: str) -> bool:
    """Return True if embed contains valid Ubisoft map CDN links or JDN links.

    Supports both legacy/public and newer/private hosts used by NOHUD, as well as Just Dance Now CDNs.
    """
    for url in extract_urls_from_html(html):
        if _MAP_PATH_PATTERN.search(url):
            return True
        url_low = url.lower()
        if "justdancenow.com" in url_low or "jdn-location" in url_low or "jdn-mp4" in url_low or "jdn-sneakpeak" in url_low:
            return True
    return False


def _has_gameplay_video_links(html: str) -> bool:
    """Return True if the embed contains at least one gameplay .webm link."""
    for url in extract_urls_from_html(html):
        # Ubisoft CDN links may wrap the real filename in a hashed path segment,
        # so resolve the effective filename instead of relying on raw URL suffix.
        lower_name = get_filename_from_url(url).lower()
        if not lower_name.endswith(".webm"):
            continue
        if "mappreview" in lower_name or "videopreview" in lower_name:
            continue
        return True
    return False


def _embed_contains_codename_links(html: str, codename: str) -> bool:
    """Return True when at least one map URL targets ``codename``."""
    needle = (codename or "").strip().lower()
    if not needle:
        return True

    for url in extract_urls_from_html(html):
        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if any(part.lower() == needle for part in parts):
            return True
            
        if parts:
            filename = parts[-1].lower()
            if filename.startswith(f"{needle}.") or filename == needle:
                return True
                
    return False


def _is_valid_embed_response(
    html: str,
    require_gameplay_video: bool = False,
    expected_codename: Optional[str] = None,
    allow_textual_codename_fallback: bool = False,
) -> tuple[bool, str]:
    """Validate an embed response before accepting it as usable."""
    if not _has_valid_cdn_links(html):
        return False, "Failed _has_valid_cdn_links (no matching /map/ or /jdnext/maps/ CDN paths found in embed)"
    if expected_codename and not _embed_contains_codename_links(html, expected_codename):
        if not (
            allow_textual_codename_fallback
            and _embed_mentions_expected_codename(html, expected_codename)
        ):
            return False, f"Codename '{expected_codename}' not found in any URL paths or text"
    if require_gameplay_video and not _has_gameplay_video_links(html):
        return False, "Failed _has_gameplay_video_links (no .webm gameplay video found in embed)"
    return True, "Valid"


def _strip_html_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _extract_embed_fields_from_html(html: str) -> Dict[str, str]:
    """Extract Discord embed field name/value pairs from an accessory HTML block."""
    fields: Dict[str, str] = {}
    pattern = re.compile(
        r'<div class="embedFieldName[^\"]*">\s*<span>(.*?)</span>\s*</div>\s*'
        r'<div class="embedFieldValue[^\"]*">(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    for raw_name, raw_value in pattern.findall(html):
        name = unescape(_strip_html_tags(raw_name)).strip().rstrip(":")
        value = unescape(_strip_html_tags(raw_value)).strip()
        if not name:
            continue
        if name in fields:
            fields[name] = f"{fields[name]}\n{value}".strip()
        else:
            fields[name] = value
    return fields


def _extract_embed_title_from_accessory_html(html: str) -> str:
    """Extract embed title text from a Discord accessory HTML block."""
    match = re.search(
        r'<div class="embedTitle[^\"]*">\s*<span>(.*?)</span>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return unescape(_strip_html_tags(match.group(1))).strip()


def _extract_embed_error_message(html: str) -> str:
    """Return an error message when the embed clearly reports a failure."""
    fields = _extract_embed_fields_from_html(html)
    for name, value in fields.items():
        if re.search(r"\berror\b", name, re.IGNORECASE):
            return value.strip()

    text = unescape(_strip_html_tags(html))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    error_hint = re.search(
        r"(could(?:n['’]t| not)\s+find\s+this\s+track|track\s+not\s+found|\berror\b)",
        text,
        re.IGNORECASE,
    )
    if not error_hint:
        return ""

    return text[:300]


def _embed_mentions_expected_codename(html: str, expected_codename: Optional[str]) -> bool:
    """Return True if embed content appears to reference the requested codename."""
    needle = (expected_codename or "").strip().lower()
    if not needle:
        return True

    title = _extract_embed_title_from_accessory_html(html).strip().lower()
    if title == needle:
        return True

    text = unescape(_strip_html_tags(html)).lower()
    if re.search(rf"\b{re.escape(needle)}\b", text):
        return True
        
    # JDNext titles often contain spaces or special chars differing from the codename.
    num_alpha_needle = re.sub(r'[^a-z0-9]', '', needle)
    num_alpha_text = re.sub(r'[^a-z0-9]', '', text)
    if num_alpha_needle and len(num_alpha_needle) >= 3 and num_alpha_needle in num_alpha_text:
        return True
        
    return False


def _extract_requester_mentions_from_embed(html: str) -> Set[str]:
    """Extract @mention display names from embed markup/text."""
    mentions: Set[str] = set()

    # Simple @mention tokens (single-word usernames like @ven)
    for match in re.findall(r"@([A-Za-z0-9_.\-]{2,32})", html or ""):
        name = (match or "").strip().lower()
        if name:
            mentions.add(name)

    # Discord renders mentions inside <span> elements with mention-related
    # class names.  Extract the full text which may contain spaces
    # (e.g. "tonton loup").
    for match in re.findall(
        r'<span[^>]*class="[^"]*mention[^"]*"[^>]*>@?([^<]+)</span>',
        html or "",
        re.IGNORECASE,
    ):
        name = (match or "").strip().lower()
        if name:
            mentions.add(name)

    # "username ... used /command" pattern in interaction headers
    interaction_match = re.search(r'class="username[^>]*>([^<]+)</span>\s*used', html, re.IGNORECASE)
    if interaction_match:
        name = interaction_match.group(1).strip().lower()
        if name:
            mentions.add(name)

    return mentions


def _embed_matches_requester(html: str, requester_handles: Optional[List[str]]) -> bool:
    """Return True when embed appears to target the logged-in requester.

    If no requester handles are known, this check is neutral (True).
    If embed has no @mentions, this check is also neutral to avoid false negatives.

    Uses substring containment rather than exact matching because display names
    may contain spaces (e.g. "tonton loup") and the simple @regex may only
    capture the first word.
    """
    if not requester_handles:
        return True

    expected = {
        (h or "").strip().lstrip("@").lower()
        for h in requester_handles
        if (h or "").strip()
    }
    if not expected:
        return True

    mentions = _extract_requester_mentions_from_embed(html)
    if not mentions:
        return True

    # Exact match first
    if mentions.intersection(expected):
        return True

    # Substring containment: "tonton loup" in expected should match
    # "tonton" in mentions (partial @regex capture), and vice versa.
    for m in mentions:
        for e in expected:
            if m in e or e in m:
                return True

    return False


async def _get_logged_in_requester_handles(page) -> List[str]:
    """Best-effort extraction of logged-in Discord account handles from the UI."""
    candidates = await page.evaluate(
        """() => {
            const out = new Set();

            // Bottom-left account panel: look for the username section
            const accountBtn = document.querySelector('[aria-label*="Set Status"], [aria-label*="User Settings"]');
            if (accountBtn) {
                const t = (accountBtn.textContent || '').trim();
                if (t) out.add(t);
            }

            // Various selectors for the username display in Discord's
            // bottom-left panel (layout changes across Discord versions).
            const selectors = [
                '[class*="nameTag"]',
                '[class*="panelTitleContainer"] [class*="username"]',
                'section[aria-label*="User area"] [class*="username"]',
                '[class*="userTag"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = (el.textContent || '').trim();
                    if (t) out.add(t);
                }
            }

            return Array.from(out);
        }"""
    )

    handles: Set[str] = set()
    for raw in cast(List[str], candidates or []):
        token = (raw or "").strip()
        if not token:
            continue
        # Strip the #discriminator suffix if present (e.g. "User#1234" → "User")
        # but do NOT split by whitespace — display names can contain spaces
        # (e.g. "tonton loup" is one user, not two).
        name = re.split(r"#\d{4}$", token)[0].strip().lstrip("@")
        name_lower = name.lower()
        if name_lower and len(name_lower) >= 2:
            handles.add(name_lower)

    return sorted(handles)


def _parse_bool_text(value: str) -> Optional[bool]:
    v = (value or "").strip().lower()
    # Keep numeric strings (0/1) numeric; coach_count uses these values.
    if v in {"true", "yes", "on"}:
        return True
    if v in {"false", "no", "off"}:
        return False
    return None


def _canonicalize_other_info_field(name: str) -> Optional[str]:
    k = re.sub(r"[^a-z0-9]+", "", name.lower())
    if "sweat" in k and "difficulty" in k:
        return "sweat_difficulty"
    if "difficulty" in k:
        return "difficulty"
    if "additionaltitle" in k:
        return "additional_title"
    if "camera" in k and "support" in k:
        return "camera_support"
    if "lyrics" in k and "color" in k:
        return "lyrics_color"
    if "title" in k and "logo" in k:
        return "title_logo"
    if "map" in k and "length" in k:
        return "map_length"
    if "original" in k and "version" in k:
        return "original_jd_version"
    if "coach" in k and "count" in k:
        return "coach_count"
    return None


def _extract_kv_pairs_from_text(text: str) -> Dict[str, str]:
    """Extract key/value pairs from plain text response lines (Key: Value)."""
    result: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key in result and value:
            result[key] = f"{result[key]}\n{value}".strip()
        else:
            result[key] = value
    return result


def _extract_labeled_value(text: str, label_pattern: str) -> str:
    """Return the last "<label>: <value>" match from flattened message text."""
    src = re.sub(r"\s+", " ", text or "").strip()
    if not src:
        return ""

    matches = list(
        re.finditer(rf"{label_pattern}\s*:\s*(.+?)(?=$)", src, re.IGNORECASE)
    )
    if not matches:
        return ""
    return matches[-1].group(1).strip()


def _parse_jdnext_button_payloads(metadata_payloads: Dict[str, Dict[str, str]]) -> Dict[str, object]:
    """Parse button response payloads into a structured metadata dict."""
    sections: Dict[str, Dict[str, object]] = {}
    parsed: Dict[str, object] = {
        "sections": sections,
        "tags": [],
        "coach_names": [],
        "credits": "",
        "other_info": {},
    }

    for key, payload in metadata_payloads.items():
        html_src = str(payload.get("accessories_html") or payload.get("content_html") or "")
        text_src = str(payload.get("content_text") or "")
        html_fields = _extract_embed_fields_from_html(html_src)
        text_fields = _extract_kv_pairs_from_text(text_src)
        merged_fields = dict(html_fields)
        for field_name, field_value in text_fields.items():
            if field_name in merged_fields and field_value:
                merged_fields[field_name] = f"{merged_fields[field_name]}\n{field_value}".strip()
            else:
                merged_fields[field_name] = field_value

        combined_text = unescape(_strip_html_tags(payload.get("combined_html", "")))
        combined_text = re.sub(r"\s+", " ", combined_text).strip()
        sections[key] = {
            "fields": merged_fields,
            "text": combined_text,
            "message_id": payload.get("message_id", ""),
        }

    # Tags
    tag_fields = sections.get("tags", {}).get("fields", {}) if sections.get("tags") else {}
    tags: List[str] = []
    for _, value in cast(Dict[str, str], tag_fields).items():
        for token in re.split(r"[,/\\|\n]", value):
            tag = token.strip()
            if tag and tag.lower() not in {"tags", "tag"}:
                tags.append(tag)

    if not tags and sections.get("tags"):
        tag_text = str(sections.get("tags", {}).get("text", "") or "")
        tail = _extract_labeled_value(tag_text, r"tags")
        if tail:
            for token in re.split(r"[,/\\|\n]", tail):
                tag = token.strip()
                if tag and tag.lower() not in {"tags", "tag"}:
                    tags.append(tag)
    parsed["tags"] = list(dict.fromkeys(tags))

    # Coaches (preserve order from Coach 1..4 when present)
    coach_fields = sections.get("coaches", {}).get("fields", {}) if sections.get("coaches") else {}
    coach_ordered: List[str] = []
    indexed: List[tuple[int, str]] = []
    for name, value in cast(Dict[str, str], coach_fields).items():
        m = re.search(r"(\d+)", name)
        if m:
            indexed.append((int(m.group(1)), value.strip()))
        elif value.strip():
            coach_ordered.append(value.strip())
    if indexed:
        for _, value in sorted(indexed, key=lambda pair: pair[0]):
            if value:
                coach_ordered.append(value)

    if not coach_ordered and sections.get("coaches"):
        coach_text = str(sections.get("coaches", {}).get("text", "") or "")
        tail = _extract_labeled_value(coach_text, r"coaches?\s*'?\s*names")
        if tail:
            for token in re.split(r"[,/\\|\n]", tail):
                coach = token.strip()
                if coach:
                    coach_ordered.append(coach)
    parsed["coach_names"] = list(dict.fromkeys([v for v in coach_ordered if v]))

    # Credits
    credit_fields = sections.get("credits", {}).get("fields", {}) if sections.get("credits") else {}
    if credit_fields:
        parsed["credits"] = "\n".join(
            [v for v in cast(Dict[str, str], credit_fields).values() if v]
        ).strip()
    if not parsed["credits"] and sections.get("credits"):
        credit_text = str(sections.get("credits", {}).get("text", "") or "")
        parsed["credits"] = _extract_labeled_value(credit_text, r"credits")

    # Other info
    other_fields = sections.get("other_info", {}).get("fields", {}) if sections.get("other_info") else {}
    other_info: Dict[str, object] = {}
    for name, value in cast(Dict[str, str], other_fields).items():
        canonical = _canonicalize_other_info_field(name)
        if not canonical:
            continue
        bool_val = _parse_bool_text(value)
        other_info[canonical] = bool_val if bool_val is not None else value.strip()
    parsed["other_info"] = other_info

    return parsed


async def _click_button_from_accessory(
    page,
    *,
    accessory_id: str,
    label_patterns: List[re.Pattern[str]],
) -> bool:
    """Click a button within a specific message accessory by label pattern."""
    base = page.locator(f"#{accessory_id} button")
    for pattern in label_patterns:
        candidate = base.filter(has_text=pattern).first
        try:
            if await candidate.count() > 0:
                await candidate.click()
                return True
        except Exception:
            continue
    return False


async def _fetch_jdnext_button_metadata(
    page,
    *,
    assets_accessory_id: str,
    timeout_s: int,
) -> Dict[str, Dict[str, str]]:
    """Click JDNext metadata buttons and capture each reply message payload."""
    button_map: Dict[str, List[re.Pattern[str]]] = {
        "tags": [re.compile(r"^\s*tags\s*$", re.IGNORECASE)],
        "coaches": [
            re.compile(r"coach(?:es)?\s*'?\s*names", re.IGNORECASE),
            re.compile(r"coach\s*names", re.IGNORECASE),
        ],
        "credits": [re.compile(r"^\s*credits\s*$", re.IGNORECASE)],
        "other_info": [
            re.compile(r"other\s*info", re.IGNORECASE),
            re.compile(r"other\s*information", re.IGNORECASE),
        ],
    }
    payloads: Dict[str, Dict[str, str]] = {}

    for key, patterns in button_map.items():
        pre_msg_id = await _get_last_message_id(page)
        clicked = await _click_button_from_accessory(
            page,
            accessory_id=assets_accessory_id,
            label_patterns=patterns,
        )
        if not clicked:
            logger.debug("Could not find JDNext metadata button for %s", key)
            continue

        response_msg_id = await _wait_for_new_message(page, pre_msg_id, timeout_s=timeout_s)
        payloads[key] = await _extract_message_payload(page, response_msg_id)
        await page.wait_for_timeout(300)

    return payloads


async def _fetch_command_with_retry(
    page,
    *,
    command: str,
    choices: List[str],
    codename: str,
    label: str,
    max_retries: int = 2,
    bot_timeout_s: int = 60,
    require_gameplay_video: bool = False,
    allow_textual_codename_fallback: bool = False,
    requester_handles: Optional[List[str]] = None,
) -> str:
    """Send a slash command, wait for bot response, extract and validate HTML.

    Retries up to ``max_retries`` times if the response has no valid CDN links.

    Scanning strategy for multi-user channels:
    1. Skip bot error messages that reference a *different* codename.
    2. Validate that the embed contains our requested codename + valid CDN links.
    3. Use requester identity as a secondary hint — log a warning but don't
       hard-reject, since identity detection is fragile (display names with
       spaces, varying Discord layouts, etc.).
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.debug("Retrying %s (attempt %d/%d)...", label, attempt + 1, max_retries + 1)
            try:
                await page.wait_for_timeout(3000)
            except Exception as exc:
                if _is_browser_closed_error(exc):
                    raise WebExtractionError("Browser was closed by user. Fetch cancelled.") from exc
                raise

        try:
            pre_id = await _get_last_accessory_id(page)
            await _send_slash_command(page, command=command, choices=choices, codename=codename)
            scan_deadline = asyncio.get_running_loop().time() + bot_timeout_s
            cursor_id = pre_id

            while asyncio.get_running_loop().time() < scan_deadline:
                remaining = max(1, int(scan_deadline - asyncio.get_running_loop().time()))
                embed_id = await _wait_for_new_embed(page, cursor_id, timeout_s=remaining)
                html = await _extract_embed_html(page, embed_id)
                cursor_id = embed_id

                # 1. Check for bot error messages first.
                error_message = _extract_embed_error_message(html)
                if error_message:
                    if _embed_mentions_expected_codename(html, codename):
                        raise WebExtractionError(
                            f"{label} bot error for '{codename}': {error_message}"
                        )
                    logger.debug(
                        "Skipping %s response %s: bot error does not reference requested codename '%s'.",
                        label,
                        embed_id,
                        codename,
                    )
                    continue

                # 2. Validate codename + CDN links (the most reliable signal).
                is_valid, reason = _is_valid_embed_response(
                    html,
                    require_gameplay_video=require_gameplay_video,
                    expected_codename=codename,
                    allow_textual_codename_fallback=allow_textual_codename_fallback,
                )
                if not is_valid:
                    logger.debug(
                        "Skipping %s response %s: %s",
                        label,
                        embed_id,
                        reason,
                    )
                    try:
                        Path("LAST_FAILED_EMBED.html").write_text(html, encoding="utf-8")
                    except Exception:
                        pass
                    continue

                # 3. Requester identity is a secondary hint, not a hard gate.
                #    In multi-user channels, another user might query the same
                #    codename — but if the embed passes codename + CDN validation,
                #    it contains the data we need regardless of who triggered it.
                if not _embed_matches_requester(html, requester_handles):
                    logger.debug(
                        "Note: %s response %s was likely triggered by another user, "
                        "but it matches our requested codename '%s'. Accepting.",
                        label,
                        embed_id,
                        codename,
                    )

                logger.debug("Extracted %s embed HTML.", label)
                return html

            raise WebExtractionError(
                f"Timed out waiting for a {label} response matching codename '{codename}'."
            )
        except WebExtractionError as e:
            if attempt == max_retries:
                raise
            logger.debug("%s attempt %d failed: %s", label, attempt + 1, e)
        except Exception as e:
            if _is_browser_closed_error(e):
                raise WebExtractionError("Browser was closed by user. Fetch cancelled.") from e
            if attempt == max_retries:
                raise
            logger.debug("%s attempt %d failed: %s", label, attempt + 1, e)

    if require_gameplay_video:
        raise WebExtractionError(
            f"{label} response contained no valid gameplay video links after "
            f"{max_retries + 1} attempts.\n"
            "The bot may not have data for this codename, or NOHUD links are invalid/expired."
        )

    raise WebExtractionError(
        f"{label} response contained no valid download links after "
        f"{max_retries + 1} attempts.\n"
        "The bot may not have data for this codename."
    )


# ---------------------------------------------------------------------------
# Web extractor class
# ---------------------------------------------------------------------------

class WebPlaywrightExtractor(BaseExtractor):
    """Extractor that downloads JDU map data via Discord bot automation.

    Operates in two modes:
    1. **From pre-saved HTML files** — legacy workflow, uses local
       ``assets.html`` / ``nohud.html``.
     2. **Live Fetch** — launches a persistent-profile Chromium browser,
         automates Discord slash commands (JDU: ``/assets jdu <codename>`` +
         ``/nohud <codename>``; JDNext: ``/asset server:jdnext codename:<codename>``),
         captures the bot's embed HTML, then
       downloads the CDN URLs.
    """

    def __init__(
        self,
        asset_html: Optional[str | Path] = None,
        nohud_html: Optional[str | Path] = None,
        urls: Optional[List[str]] = None,
        codenames: Optional[List[str]] = None,
        source_game: str = "jdu",
        quality: str = "ULTRA_HD",
        config: Optional[AppConfig] = None,
    ) -> None:
        self._asset_html = asset_html
        self._nohud_html = nohud_html
        self._urls = urls or []
        self._quality = quality
        self._config = config or AppConfig()
        self._codenames = codenames or []
        self._codename: Optional[str] = self._codenames[0] if self._codenames else None
        self._source_game = (source_game or "jdu").strip().lower() or "jdu"

    def _download_group(self) -> str:
        """Return the mapDownloads subgroup name for the active source game."""
        if self._source_game == "jdnow":
            return "jdnow"
        return "jdnext" if self._source_game == "jdnext" else "jdu"

    def _download_dir_for_codename(self, codename: str) -> Path:
        """Build the canonical fetch cache path under mapDownloads/<game>/<codename>."""
        safe_codename = (codename or "UnknownMap").strip() or "UnknownMap"
        return self._config.download_root / self._download_group() / safe_codename

    def extract(self, output_dir: Path) -> Path:
        """Download files into download_root and extract them into output_dir."""
        all_urls = list(self._urls)
        if self._asset_html and Path(self._asset_html).exists():
            all_urls.extend(extract_urls_from_file(self._asset_html))
        if self._nohud_html and Path(self._nohud_html).exists():
            all_urls.extend(extract_urls_from_file(self._nohud_html))

        # Live fetch: scrape Discord for each codename
        if not all_urls and self._codenames:
            for codename in self._codenames:
                try:
                    scraped = asyncio.run(self._scrape_codename(codename))
                    all_urls.extend(scraped)
                except Exception as e:
                    logger.error("Failed to scrape codename '%s': %s", codename, e)
                    raise

        if not all_urls:
            raise WebExtractionError("No URLs provided for extraction")

        # V1-style guardrails: fail fast if key media links are missing.
        classified_required = _classify_urls(all_urls, self._quality, self._config)
        missing_required: list[str] = []
        if self._source_game == "jdnow":
            if not classified_required.get("mainscene"):
                missing_required.append("bundle.zip")
            if not classified_required.get("audio"):
                missing_required.append("audio (.mp3)")
            if not classified_required.get("video"):
                missing_required.append("gameplay video (.mp4)")
        else:
            if not classified_required.get("mainscene"):
                if self._source_game == "jdnext":
                    missing_required.append("mapPackage bundle")
                else:
                    missing_required.append("MAIN_SCENE zip")
            if not classified_required.get("audio"):
                if self._source_game == "jdnext":
                    missing_required.append("full audio (.opus/.ogg)")
                else:
                    missing_required.append("full audio (.ogg)")
            if not classified_required.get("video"):
                missing_required.append("gameplay video (.webm)")

        if missing_required:
            raise WebExtractionError(
                "Missing required download links: "
                + ", ".join(missing_required)
                + ". The bot likely returned an error, or the HTML links are stale/invalid."
            )

        inferred_codename = extract_codename_from_urls(all_urls)
        if not inferred_codename and self._codenames:
            inferred_codename = self._codenames[0]
        if not inferred_codename and self._asset_html and Path(self._asset_html).exists():
            try:
                html_content = Path(self._asset_html).read_text(encoding="utf-8", errors="ignore")
                inferred_codename = _extract_embed_title_from_html(html_content)
            except OSError:
                pass

        self._codename = inferred_codename or self._codename
        codename = self._codename or "UnknownMap"
        logger.info("Downloading Assets, this may take a while...")
        
        # 1. Determine download directory (respect hand-picked HTML location if available)
        if self._asset_html and Path(self._asset_html).is_file():
            download_dir = Path(self._asset_html).parent
        else:
            download_dir = self._download_dir_for_codename(codename)
            download_dir.mkdir(parents=True, exist_ok=True)

        downloaded = download_files(all_urls, download_dir, self._quality, self._config)
        
        # 1b. Check for missing critical files (possible link expiration)
        missing_critical = False
        classified = _classify_urls(all_urls, self._quality, self._config)
        for key in ("video", "audio", "mainscene"):
            url = classified[key]
            if url:
                fname = get_filename_from_url(url)
                if fname not in downloaded:
                    missing_critical = True
                    break
        
        if missing_critical and self._codenames:
            logger.warning("Critical files missing (Expired links?). Attempting one re-scrape...")
            scraped_fresh = []
            for codename in self._codenames:
                try:
                    scraped_fresh.extend(asyncio.run(self._scrape_codename(codename)))
                except WebExtractionError as exc:
                    logger.warning("Re-scrape for '%s' failed: %s", codename, exc)
                except Exception as exc:
                    logger.warning("Unexpected re-scrape error for '%s': %s", codename, exc)
            if scraped_fresh:
                all_urls = list(set(all_urls + scraped_fresh))
                refreshed = download_files(all_urls, download_dir, self._quality, self._config)
                downloaded.update(refreshed)

        # V1 parity: fail fast when critical assets remain unavailable.
        classified = _classify_urls(all_urls, self._quality, self._config)
        still_missing: list[str] = []
        for key in ("video", "audio", "mainscene"):
            url = classified.get(key)
            if not url:
                continue
            fname = get_filename_from_url(url)
            if fname not in downloaded:
                still_missing.append(f"{key}:{fname}")

        if still_missing:
            if self._source_game == "jdnext":
                _try_jdnext_missing_fallbacks(
                    all_urls=all_urls,
                    classified=classified,
                    downloaded=downloaded,
                    download_dir=download_dir,
                    config=self._config,
                )
                still_missing = []
                for key in ("video", "audio", "mainscene"):
                    url = classified.get(key)
                    if not url:
                        continue
                    fname = get_filename_from_url(url)
                    if fname not in downloaded:
                        still_missing.append(f"{key}:{fname}")

        if still_missing:
            raise WebExtractionError(
                "Critical download(s) missing after retry: "
                + ", ".join(still_missing)
                + ". Links may have expired; fetch fresh assets/nohud HTML and retry."
            )

        # 2. Extract/Assemble into temporary output_dir
        extract_dir = output_dir / codename
        extract_dir.mkdir(parents=True, exist_ok=True)

        if self._source_game == "jdnext":
            mainscene_url = cast(Optional[str], classified.get("mainscene"))
            if mainscene_url:
                mainscene_name = get_filename_from_url(mainscene_url)
                mainscene_path = download_dir / mainscene_name
                if mainscene_path.exists() and mainscene_path.suffix.lower() == ".bundle":
                    strategy_dir = extract_dir / "jdnext_strategy"
                    try:
                        summary = run_jdnext_bundle_strategy(
                            mainscene_path,
                            strategy_dir,
                            strategy="assetstudio_first",
                            codename=codename,
                            config=self._config,
                        )
                        mapped_root = strategy_dir / "mapped"
                        if mapped_root.exists():
                            for child in mapped_root.iterdir():
                                dst = extract_dir / child.name
                                if child.is_dir():
                                    shutil.copytree(child, dst, dirs_exist_ok=True)
                                else:
                                    shutil.copy2(child, dst)
                        logger.debug("JDNext mapPackage strategy winner: %s", summary.winner)
                    except Exception as exc:
                        logger.debug("JDNext mapPackage strategy extraction failed: %s", exc)

                    try:
                        _extract_jdnext_aux_texture_bundles(
                            download_dir=download_dir,
                            extract_dir=extract_dir,
                            mainscene_name=mainscene_name,
                            codename=codename,
                            config=self._config,
                        )
                    except Exception as exc:
                        logger.debug("JDNext auxiliary texture extraction failed: %s", exc)
        
        # Post-download: extract MAIN_SCENE_*.zip from download_dir into extract_dir
        if self._source_game == "jdnow":
            # Extract JDN zips (bundle.zip and songMetadata.zip)
            for zip_name in ("bundle.zip", "songMetadata.zip"):
                zip_path = download_dir / zip_name
                if zip_path.exists():
                    logger.debug("Extracting JDN zip: %s", zip_name)
                    with zipfile.ZipFile(zip_path, "r") as z:
                        z.extractall(extract_dir)
        else:
            self._extract_scene_zips(download_dir, extract_dir)
        
        # Copy non-extracted assets (e.g. video, audio) to extract_dir for normalizer
        for f in os.listdir(download_dir):
            src_file = download_dir / f
            dst_file = extract_dir / f
            low_name = f.lower()
            if any(token in low_name for token in ("audiopreview", "videopreview", "mappreview")):
                continue
            if src_file.is_file() and not f.endswith(".zip") and not dst_file.exists():
                logger.debug("Copying %s to extraction dir", f)
                shutil.copy2(src_file, dst_file)

        # JDNext HTML mode: derive codename from extracted source payloads.
        # This avoids using opaque UUID map IDs or noisy embed titles as map folder names.
        if self._source_game == "jdnext" and not self._codenames:
            source_codename = _infer_codename_from_source_files(extract_dir)
            if source_codename and source_codename.lower() != codename.lower():
                logger.info(
                    "Resolved JDNext codename from source files: '%s' -> '%s'",
                    codename,
                    source_codename,
                )
                self._codename = source_codename
                target_extract_dir = output_dir / source_codename
                if target_extract_dir != extract_dir:
                    if target_extract_dir.exists():
                        shutil.copytree(extract_dir, target_extract_dir, dirs_exist_ok=True)
                        shutil.rmtree(extract_dir, ignore_errors=True)
                    else:
                        extract_dir.rename(target_extract_dir)
                    extract_dir = target_extract_dir

        return extract_dir

    def get_codename(self) -> Optional[str]:
        return self._codename

    def get_source_dir(self) -> Optional[Path]:
        """Return the download cache directory for the active codename, if known."""
        if self._codename:
            return self._download_dir_for_codename(self._codename)
        return None

    @staticmethod
    def _extract_scene_zips(src_dir: Path, dst_dir: Optional[Path] = None) -> None:
        """Extract MAIN_SCENE_*.zip files from src_dir into dst_dir.

        Mirrors V1 ``step_03_extract_scenes``.  After downloading, the
        normalizer expects loose ``.ckd`` files — not a ZIP.
        """
        if dst_dir is None:
            dst_dir = src_dir

        scene_zips: list[str] = []
        for f in os.listdir(src_dir):
            if "SCENE" in f.upper() and f.endswith(".zip"):
                scene_zips.append(f)

        if not scene_zips:
            logger.debug("No scene ZIPs found in %s — skipping extraction.", src_dir)
            return

        # Prefer DURANGO > NX > SCARLETT > any
        selected: Optional[str] = None
        for plat in SCENE_PLATFORM_PREFERENCE:
            matches = [z for z in scene_zips if f"MAIN_SCENE_{plat}" in z.upper()]
            if matches:
                selected = matches[0]
                break

        if selected:
            zip_path = src_dir / selected
            logger.debug("Extracting scene ZIP: %s", selected)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(dst_dir)
        else:
            # Fallback: extract all scene ZIPs
            for f in scene_zips:
                zip_path = src_dir / f
                logger.debug("Extracting scene ZIP (fallback): %s", f)
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(dst_dir)

        # -- Unpack any .ipk files found after ZIP extraction (mirrors V1 step_04) ---
        for ipk in dst_dir.glob("*.ipk"):
            logger.debug("Unpacking IPK found in scene ZIP: %s", ipk.name)
            try:
                extract_ipk(ipk, dst_dir)
                # Delete IPK after extraction to keep normalization directory clean
                ipk.unlink()
            except Exception as e:
                logger.debug("Failed to unpack IPK %s: %s", ipk.name, e)

    # ------------------------------------------------------------------
    # Live Discord scraping  (async, called via asyncio.run from QThread)
    # ------------------------------------------------------------------

    async def _scrape_codename(self, codename: str) -> List[str]:
        """Full fetch flow for one codename: launch browser → login →
        JDU: ``/assets`` → ``/nohud``; JDNext: ``/asset server:jdnext``;
        then save HTML and return extracted URLs.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise WebExtractionError(
                "playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            )

        channel_url = self._config.discord_channel_url
        if not channel_url:
            raise WebExtractionError(
                "discord_channel_url is not configured. "
                "Set it in your installer_settings.json or via the Settings dialog."
            )

        profile_dir = str(self._config.browser_profile_dir.resolve())
        background_mode = getattr(self._config, "fetch_background_mode", False)

        # When background mode is enabled, use INFO level so actions are
        # visible in the GUI log console (since the browser itself is hidden).
        _log = logger.info if background_mode else logger.debug

        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]
        if background_mode:
            # Move the browser far off-screen so it's invisible but the
            # viewport stays at full size (unlike minimize which collapses
            # the viewport to 0×0, breaking Playwright interactions).
            launch_args.append("--window-position=-32000,-32000")
            _log("Background mode: browser will run off-screen.")

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                viewport={"width": 1280, "height": 800},
                args=launch_args,
            )

            page = context.pages[0] if context.pages else await context.new_page()

            try:
                _log("Fetching codename: %s", codename)

                # Navigate to Discord channel
                _log("Navigating to Discord channel...")
                await page.goto(channel_url, wait_until="domcontentloaded")

                if background_mode:
                    # In background mode, try a quick login check first.
                    # If the session expired, bring the browser on-screen
                    # for manual re-login, then move it back off-screen.
                    try:
                        await _wait_for_login(page, timeout_s=15)
                        _log("Discord session is valid (already logged in).")
                    except Exception:
                        _log(
                            "Discord session expired or not found. "
                            "Bringing browser on-screen for manual login..."
                        )
                        try:
                            cdp = await context.new_cdp_session(page)
                            await cdp.send("Browser.setWindowBounds", {
                                "windowId": 1,
                                "bounds": {"left": 100, "top": 100, "windowState": "normal"},
                            })
                        except Exception as cdp_exc:
                            logger.debug("CDP setWindowBounds failed: %s", cdp_exc)

                        await _wait_for_login(page, self._config.fetch_login_timeout_s)
                        _log("Login successful. Moving browser back off-screen.")

                        try:
                            cdp = await context.new_cdp_session(page)
                            await cdp.send("Browser.setWindowBounds", {
                                "windowId": 1,
                                "bounds": {"left": -32000, "top": -32000, "windowState": "normal"},
                            })
                        except Exception as cdp_exc:
                            logger.debug("CDP setWindowBounds (hide) failed: %s", cdp_exc)
                else:
                    await _wait_for_login(page, self._config.fetch_login_timeout_s)

                # Wait for channel messages to load
                try:
                    await (
                        page.locator(_SEL_MESSAGE_ACCESSORIES)
                        .first
                        .wait_for(timeout=15_000)
                    )
                except Exception:
                    pass  # Channel might be empty

                bot_timeout = self._config.fetch_bot_response_timeout_s
                requester_handles = await _get_logged_in_requester_handles(page)
                if requester_handles:
                    _log("Requester identity detected: %s", ", ".join(requester_handles))
                else:
                    _log("Requester identity not detected; proceeding without requester filter.")

                nohud_html: Optional[str] = None
                jdnext_metadata_payloads: Dict[str, Dict[str, str]] = {}
                if self._source_game == "jdnow":
                    _log("[1/1] Sending /asset jdnow %s", codename)
                    assets_html = await _fetch_command_with_retry(
                        page,
                        command="asset",
                        choices=["jdnow"],
                        codename=codename,
                        label="asset",
                        bot_timeout_s=bot_timeout,
                        require_gameplay_video=False,
                        allow_textual_codename_fallback=True,
                        requester_handles=requester_handles,
                    )
                    _log("Received /asset response for %s.", codename)
                elif self._source_game == "jdnext":
                    _log("[1/1] Sending /asset server:jdnext %s", codename)
                    assets_html = await _fetch_command_with_retry(
                        page,
                        command="asset",
                        choices=["jdnext"],
                        codename=codename,
                        label="asset",
                        bot_timeout_s=bot_timeout,
                        require_gameplay_video=True,
                        allow_textual_codename_fallback=True,
                        requester_handles=requester_handles,
                    )
                    _log("Received /asset response for %s.", codename)
                    assets_accessory_id = await _get_last_accessory_id(page)
                    if assets_accessory_id:
                        try:
                            _log("Fetching JDNext metadata buttons...")
                            jdnext_metadata_payloads = await _fetch_jdnext_button_metadata(
                                page,
                                assets_accessory_id=assets_accessory_id,
                                timeout_s=bot_timeout,
                            )
                            _log("JDNext metadata captured: %s", ", ".join(jdnext_metadata_payloads.keys()) or "none")
                        except Exception as meta_exc:
                            _log("JDNext metadata button capture failed: %s", meta_exc)
                else:
                    # Step 1: /assets jdu <codename>
                    _log("[1/2] Sending /assets jdu %s", codename)
                    assets_html = await _fetch_command_with_retry(
                        page,
                        command="assets",
                        choices=["jdu"],
                        codename=codename,
                        label="assets",
                        bot_timeout_s=bot_timeout,
                        requester_handles=requester_handles,
                    )
                    _log("Received /assets response for %s.", codename)
                    await page.wait_for_timeout(500)

                    # Step 2: /nohud <codename>
                    _log("[2/2] Sending /nohud %s", codename)
                    nohud_html = await _fetch_command_with_retry(
                        page,
                        command="nohud",
                        choices=[],
                        codename=codename,
                        label="nohud",
                        bot_timeout_s=bot_timeout,
                        requester_handles=requester_handles,
                    )
                    _log("Received /nohud response for %s.", codename)

                # Save HTML to download dir for caching / debugging / portability
                # We use download_root which is 'mapDownloads', grouped by source game.
                download_dir = self._download_dir_for_codename(codename)
                download_dir.mkdir(parents=True, exist_ok=True)
                (download_dir / "assets.html").write_text(assets_html, encoding="utf-8")
                if nohud_html is not None:
                    (download_dir / "nohud.html").write_text(nohud_html, encoding="utf-8")
                if self._source_game == "jdnext" and jdnext_metadata_payloads:
                    meta_dir = download_dir / "jdnext_metadata"
                    meta_dir.mkdir(parents=True, exist_ok=True)
                    for key, payload in jdnext_metadata_payloads.items():
                        (meta_dir / f"{key}.message.html").write_text(
                            payload.get("combined_html", ""),
                            encoding="utf-8",
                        )
                        (meta_dir / f"{key}.content.txt").write_text(
                            payload.get("content_text", ""),
                            encoding="utf-8",
                        )
                    metadata_summary = _parse_jdnext_button_payloads(jdnext_metadata_payloads)
                    (download_dir / "jdnext_metadata.json").write_text(
                        json.dumps(metadata_summary, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                _log("Saved HTML to %s", download_dir)

            finally:
                try:
                    await context.close()
                except Exception:
                    # Context may already be closed if user manually closed browser.
                    pass

        # Extract URLs from the fetched HTML payload(s)
        all_urls = extract_urls_from_html(assets_html)
        if nohud_html is not None:
            all_urls += extract_urls_from_html(nohud_html)
        self._codename = extract_codename_from_urls(all_urls) or codename
        return all_urls

    async def scrape_live(self, page_url: str) -> List[str]:
        """Legacy method — kept for backward compatibility.

        For a simple URL scrape (non-Discord), navigates to the URL and
        extracts href links from the page content.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise WebExtractionError(
                "playwright is not installed. Run: pip install playwright && playwright install"
            )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(page_url, wait_until="networkidle")
            content = await page.content()
            await browser.close()

        urls = extract_urls_from_html(content)
        self._codename = extract_codename_from_urls(urls)
        return urls
