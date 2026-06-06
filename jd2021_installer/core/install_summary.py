"""Build checklist-style summaries for completed map installs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jd2021_installer.core.models import NormalizedMapData


@dataclass
class InstallChecklistItem:
    label: str
    present: bool
    required: bool


@dataclass
class InstallSummary:
    success: bool
    codename: str
    map_name: str
    source_mode: str
    quality: str
    duration_s: float
    required_items: list[InstallChecklistItem]
    optional_items: list[InstallChecklistItem]
    files_written_count: int
    total_size_bytes: int

    @property
    def missing_required_count(self) -> int:
        return sum(1 for item in self.required_items if not item.present)

    @property
    def missing_optional_count(self) -> int:
        return sum(1 for item in self.optional_items if not item.present)

    @property
    def has_required_missing(self) -> bool:
        return self.missing_required_count > 0

    @property
    def status_label(self) -> str:
        if not self.success:
            return "FAILED"
        if self.has_required_missing:
            return "PARTIAL/RISKY"
        if self.missing_optional_count > 0:
            return "SUCCESS (WARNINGS)"
        return "SUCCESS"

    @property
    def actionable_note(self) -> str:
        if not self.success:
            return "Installation failed. Check logs before launching the map."
        if self.has_required_missing:
            return "Required files are missing. Install is risky and may not be playable."
        if self.missing_optional_count > 0:
            return "Only optional files are missing. Install should work with reduced polish."
        return "All required and optional checklist items were fulfilled."


def _exists_any(base_dir: Path, rel_candidates: list[str]) -> bool:
    for rel in rel_candidates:
        if (base_dir / rel).exists():
            return True
    return False


def _has_any_file_in_dir(base_dir: Path, rel_dirs: list[str]) -> bool:
    for rel in rel_dirs:
        candidate = base_dir / rel
        if not candidate.is_dir():
            continue
        for child in candidate.rglob("*"):
            if child.is_file():
                return True
    return False


def _count_files_and_size(base_dir: Path) -> tuple[int, int]:
    if not base_dir.exists():
        return 0, 0

    file_count = 0
    total_size = 0
    for child in base_dir.rglob("*"):
        if not child.is_file():
            continue
        file_count += 1
        try:
            total_size += child.stat().st_size
        except OSError:
            continue
    return file_count, total_size


def _has_trk_preview_markers(base_dir: Path, codename: str) -> bool:
    trk_candidates = [
        base_dir / "Audio" / f"{codename}.trk",
        base_dir / "audio" / f"{codename}.trk",
    ]
    for trk in trk_candidates:
        if not trk.exists():
            continue
        try:
            text = trk.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "previewEntry" in text and "previewLoopStart" in text and "previewLoopEnd" in text:
            return True
    return False


def _has_main_video(base_dir: Path, codename: str) -> bool:
    return _exists_any(
        base_dir,
        [
            f"VideosCoach/{codename}.webm",
            f"videoscoach/{codename}.webm",
        ],
    )


def _required_items(base_dir: Path, codename: str) -> list[InstallChecklistItem]:
    return [
        InstallChecklistItem(
            label="Main scene",
            present=_exists_any(base_dir, [f"{codename}_MAIN_SCENE.isc"]),
            required=True,
        ),
        InstallChecklistItem(
            label="SongDesc tpl/act",
            present=_exists_any(base_dir, ["SongDesc.tpl", "SongDesc.act"])
            and _exists_any(base_dir, ["SongDesc.tpl"])
            and _exists_any(base_dir, ["SongDesc.act"]),
            required=True,
        ),
        InstallChecklistItem(
            label="Audio core files (trk/musictrack/sequence/audio isc/stape/config)",
            present=all(
                _exists_any(base_dir, [rel])
                for rel in [
                    f"Audio/{codename}.trk",
                    f"Audio/{codename}_musictrack.tpl",
                    f"Audio/{codename}_sequence.tpl",
                    f"Audio/{codename}_audio.isc",
                    f"Audio/{codename}.stape",
                    "Audio/ConfigMusic.sfi",
                ]
            ),
            required=True,
        ),
        InstallChecklistItem(
            label="Main gameplay video",
            present=_has_main_video(base_dir, codename),
            required=True,
        ),
        InstallChecklistItem(
            label="Core timeline files (dance/karaoke)",
            present=all(
                _exists_any(base_dir, [rel])
                for rel in [
                    f"Timeline/{codename}_tml.isc",
                    f"Timeline/{codename}_TML_Dance.dtape",
                    f"Timeline/{codename}_TML_Karaoke.ktape",
                ]
            ),
            required=True,
        ),
    ]


def _optional_items(base_dir: Path, codename: str) -> list[InstallChecklistItem]:
    return [
        InstallChecklistItem(
            label="Banner/background art",
            present=any(
                _exists_any(base_dir, [rel])
                for rel in [
                    f"menuart/textures/{codename}_banner_bkg.png",
                    f"menuart/textures/{codename}_banner_bkg.tga",
                    f"menuart/textures/{codename}_map_bkg.png",
                    f"menuart/textures/{codename}_map_bkg.tga",
                    f"MenuArt/textures/{codename}_banner_bkg.png",
                    f"MenuArt/textures/{codename}_banner_bkg.tga",
                    f"MenuArt/textures/{codename}_map_bkg.png",
                    f"MenuArt/textures/{codename}_map_bkg.tga",
                ]
            ),
            required=False,
        ),
        InstallChecklistItem(
            label="Album coach/album bg art",
            present=any(
                _exists_any(base_dir, [rel])
                for rel in [
                    f"menuart/textures/{codename}_cover_albumbkg.png",
                    f"menuart/textures/{codename}_cover_albumbkg.tga",
                    f"menuart/textures/{codename}_cover_albumcoach.png",
                    f"menuart/textures/{codename}_cover_albumcoach.tga",
                    f"MenuArt/textures/{codename}_cover_albumbkg.png",
                    f"MenuArt/textures/{codename}_cover_albumbkg.tga",
                    f"MenuArt/textures/{codename}_cover_albumcoach.png",
                    f"MenuArt/textures/{codename}_cover_albumcoach.tga",
                ]
            ),
            required=False,
        ),
        InstallChecklistItem(
            label="Map preview video",
            present=_has_main_video(base_dir, codename) and _has_trk_preview_markers(base_dir, codename),
            required=False,
        ),
        InstallChecklistItem(
            label="Pictograms",
            present=_has_any_file_in_dir(base_dir, ["Timeline/pictos", "timeline/pictos"]),
            required=False,
        ),
        InstallChecklistItem(
            label="Moves",
            present=_has_any_file_in_dir(base_dir, ["Timeline/Moves", "timeline/moves"]),
            required=False,
        ),
        InstallChecklistItem(
            label="Autodance payload",
            present=_has_any_file_in_dir(base_dir, ["Autodance", "autodance"]),
            required=False,
        ),
        InstallChecklistItem(
            label="Intro AMB artifacts",
            present=any(
                _exists_any(base_dir, [rel])
                for rel in [
                    f"Audio/AMB/amb_{codename.lower()}_intro.wav",
                    f"audio/amb/amb_{codename.lower()}_intro.wav",
                    f"Audio/AMB/amb_{codename.lower()}_intro.tpl",
                    f"audio/amb/amb_{codename.lower()}_intro.tpl",
                    f"Audio/AMB/amb_{codename.lower()}_intro.ilu",
                    f"audio/amb/amb_{codename.lower()}_intro.ilu",
                ]
            ),
            required=False,
        ),
    ]


def build_install_summary(
    map_data: NormalizedMapData,
    target_map_dir: Path,
    *,
    source_mode: str,
    quality: str,
    duration_s: float,
    success: bool,
) -> InstallSummary:
    codename = map_data.codename
    map_name = map_data.song_desc.title or codename
    required_items = _required_items(target_map_dir, codename)
    optional_items = _optional_items(target_map_dir, codename)
    files_written_count, total_size_bytes = _count_files_and_size(target_map_dir)

    return InstallSummary(
        success=success,
        codename=codename,
        map_name=map_name,
        source_mode=source_mode,
        quality=quality,
        duration_s=max(0.0, float(duration_s)),
        required_items=required_items,
        optional_items=optional_items,
        files_written_count=files_written_count,
        total_size_bytes=total_size_bytes,
    )


def format_size(total_size_bytes: int) -> str:
    size = float(max(0, total_size_bytes))
    units = ["B", "KB", "MB", "GB"]
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def _status_prefix(present: bool) -> str:
    return "[OK]" if present else "[MISSING]"


def render_install_summary(summary: InstallSummary) -> str:
    lines: list[str] = []
    lines.append("Install Result")
    lines.append(f"- Status: {summary.status_label}")
    lines.append(f"- Codename: {summary.codename}")
    lines.append(f"- Map Name: {summary.map_name}")
    lines.append(f"- Source Mode: {summary.source_mode}")
    lines.append(f"- Quality: {summary.quality}")
    lines.append(f"- Duration: {summary.duration_s:.2f}s")
    lines.append("")

    lines.append("Required Files Checklist")
    for item in summary.required_items:
        lines.append(f"- {_status_prefix(item.present)} {item.label}")
    lines.append("")

    lines.append("Optional Files Checklist")
    for item in summary.optional_items:
        lines.append(f"- {_status_prefix(item.present)} {item.label}")
    lines.append("")

    lines.append("Stats")
    lines.append(f"- Files Written: {summary.files_written_count}")
    lines.append(f"- Total Size Written: {format_size(summary.total_size_bytes)}")
    lines.append(f"- Missing Required: {summary.missing_required_count}")
    lines.append(f"- Missing Optional: {summary.missing_optional_count}")
    lines.append("")

    lines.append("Actionable Note")
    lines.append(f"- {summary.actionable_note}")
    return "\n".join(lines)