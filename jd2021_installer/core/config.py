"""Application configuration using Pydantic settings.

Handles paths, quality preferences, and runtime settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    """Top-level application configuration."""

    # Paths
    game_directory: Optional[Path] = None
    download_root: Path = Path("./mapDownloads")
    cache_directory: Path = Path("./cache")
    temp_directory: Path = Path("./temp")
    app_icon_path: Path = Path("./assets/app_icon.jpg")
    gesture_template_path: Path = Path("./assets/gesture_templates/durango_template.gesture")

    # Video quality preference (descending fallback)
    video_quality: str = Field(
        default="ULTRA_HD",
        pattern=r"^(ULTRA_HD|ULTRA|HIGH_HD|HIGH|MID_HD|MID|LOW_HD|LOW)$",
    )

    # UI/UX settings
    enable_legacy_sync_refinement: bool = False
    skip_preflight: bool = False
    suppress_offset_notification: bool = False
    cleanup_behavior: str = Field(default="ask", pattern=r"^(ask|delete|keep|aggressive)$")
    locked_status_behavior: str = Field(default="ask", pattern=r"^(ask|force3|keep)$")
    show_preflight_success_popup: bool = True
    show_install_summary_popup: bool = True
    show_quickstart_on_launch: bool = True
    skip_quickstart: bool = False
    log_detail_level: str = Field(default="user", pattern=r"^(quiet|user|detailed|developer)$")
    theme: str = Field(default="light", pattern=r"^(light|dark)$")
    enforce_min_window_size: bool = True
    min_window_width: int = 1000
    min_window_height: int = 920
    show_window_size_overlay: bool = False
    style_debug_mode: bool = False
    window_size_overlay_timeout_ms: int = 1100
    # Converted gestures are still ass
    convert_jdnext_gestures: bool = False 
    # AlbumCoach compositing behavior for JDNext multi-coach maps:
    # "ask" = prompt per map, "always_customize" = always show editor,
    # "always_default" = use automatic compositing silently.
    albumcoach_behavior: str = Field(
        default="ask", pattern=r"^(ask|always_customize|always_default)$"
    )
    # Cover art synthesis behavior for JDNext maps:
    # "synthesized" = always composite from map_bkg + Title asset
    # "original"    = always use original cover art as-is (may be squished)
    # "ask"         = prompt per map (default)
    jdnext_cover_behavior: str = Field(
        default="ask", pattern=r"^(ask|synthesized|original)$"
    )
    # Scoring conversion behavior: handled via gesture template selection

    # Download settings
    download_timeout_s: int = 600
    max_retries: int = 3
    retry_base_delay_s: int = 2
    inter_request_delay_s: float = 1.5
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # UbiArt engine constants
    ticks_per_ms: int = 48
    max_jd_version: int = 2021
    min_jd_version: int = 2014
    preview_fps: int = 25

    preview_only_audio_offset_ms: float = 0.0
    audio_preview_fade_s: float = 2.0

    # Discord Fetch Mode (replaces Node.js JDH_Downloader)
    discord_channel_url: str = ""
    browser_profile_dir: Path = Path("./.browser-profile")
    fetch_login_timeout_s: int = 300
    fetch_bot_response_timeout_s: int = 60
    fetch_background_mode: bool = False

    # Update checker settings
    check_updates_on_launch: bool = True
    update_branch: str = ""  # empty = auto-detect from git or state file
    
    # JDLO Integration
    jdlo_auth_path: Optional[Path] = None

    # FFmpeg configuration
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    vgmstream_path: Optional[str] = None
    assetstudio_cli_path: Optional[str] = None
    ffmpeg_hwaccel: str = Field(default="auto", pattern=r"^(auto|none)$")
    vp9_handling_mode: str = Field(
        default="fallback_compatible_down",
        pattern=r"^(reencode_to_vp8|fallback_compatible_down)$",
    )
    preview_video_mode: str = Field(default="original", pattern=r"^(proxy_low|original)$")
    
    # Missing/incompatible quality fallback behavior
    # "fallback_down" = Try lower qualities if the selected one is missing/incompatible
    # "fallback_up" = Try higher qualities before falling back to lower qualities
    video_fallback_behavior: str = Field(
        default="fallback_down", 
        pattern=r"^(fallback_down|fallback_up)$"
    )

    class Config:
        env_prefix = "JD2021_"
        env_file = ".env"


# Quality tiers and their filename suffix patterns
QUALITY_ORDER = [
    "ULTRA_HD", "ULTRA",
    "HIGH_HD", "HIGH",
    "MID_HD", "MID",
    "LOW_HD", "LOW",
]

QUALITY_PATTERNS = {
    "ULTRA_HD": "_ULTRA.hd.webm",
    "ULTRA":    "_ULTRA.webm",
    "HIGH_HD":  "_HIGH.hd.webm",
    "HIGH":     "_HIGH.webm",
    "MID_HD":   "_MID.hd.webm",
    "MID":      "_MID.webm",
    "LOW_HD":   "_LOW.hd.webm",
    "LOW":      "_LOW.webm",
}

SCENE_PLATFORM_PREFERENCE = ["DURANGO", "NX", "PC", "ORBIS", "SCARLETT", "PROSPERO", "WIIU", "X360"]
