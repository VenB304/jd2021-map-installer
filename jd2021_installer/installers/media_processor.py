"""Media processor — FFmpeg and Pillow wrappers for map asset processing.

Handles video transcoding, audio format conversion, image processing,
and preview generation.  All heavy subprocess work is designed to run
in a background QThread.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Callable, Optional

try:
    from PIL import Image
except ImportError:
    Image = None

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.exceptions import MediaProcessingError
from jd2021_installer.core.models import (
    CinematicTape,
    MusicTrackStructure,
    SongDescription,
)

logger = logging.getLogger("jd2021.installers.media_processor")

# Intro AMB generation is enabled by default for all supported source modes.
INTRO_AMB_ATTEMPT_ENABLED = True


def _write_silent_stereo_wav(path: Path, duration_s: float = 0.25) -> None:
    frames = max(1, int(round(48000 * duration_s)))
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\x00\x00\x00\x00" * frames)


def _write_silent_ogg(path: Path, config: Optional[AppConfig] = None) -> bool:
    """Create a tiny silent OGG using FFmpeg, returning True on success."""
    tmp_wav = path.with_suffix(".tmp_silence.wav")
    try:
        _write_silent_stereo_wav(tmp_wav, duration_s=0.25)
        run_ffmpeg([
            "-y",
            "-i", str(tmp_wav),
            "-c:a", "libvorbis",
            str(path),
        ], config=config)
        return path.exists()
    except Exception as exc:
        logger.debug("Silent OGG generation failed for %s: %s", path.name, exc)
        return False
    finally:
        if tmp_wav.exists():
            tmp_wav.unlink()


# ---------------------------------------------------------------------------
# FFmpeg / FFprobe subprocess wrappers
# ---------------------------------------------------------------------------

def run_ffmpeg(
    args: list[str],
    config: Optional[AppConfig] = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run an FFmpeg command and return the result.

    Raises MediaProcessingError on failure.
    """
    cfg = config or AppConfig()
    ffmpeg_args = list(args)
    if (
        getattr(cfg, "ffmpeg_hwaccel", "auto") == "auto"
        and "-hwaccel" not in ffmpeg_args
        and "-i" in ffmpeg_args
    ):
        i_index = ffmpeg_args.index("-i")
        ffmpeg_args = ffmpeg_args[:i_index] + ["-hwaccel", "auto"] + ffmpeg_args[i_index:]

    cmd = [cfg.ffmpeg_path] + ffmpeg_args
    logger.debug("FFmpeg: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise MediaProcessingError(
                f"FFmpeg failed (exit {result.returncode}): {result.stderr[:500]}"
            )
        return result
    except subprocess.TimeoutExpired:
        raise MediaProcessingError(f"FFmpeg timed out after {timeout}s")
    except FileNotFoundError:
        raise MediaProcessingError(
            f"FFmpeg not found at '{cfg.ffmpeg_path}'. "
            "Ensure FFmpeg is installed and in PATH."
        )


async def run_ffmpeg_async(
    args: list[str],
    config: Optional[AppConfig] = None,
    timeout: int = 300,
) -> None:
    """Non-blocking FFmpeg invocation using ``asyncio.create_subprocess_exec``.

    Designed for batch-converting multiple media assets concurrently without
    blocking the caller.  Can be composed with ``asyncio.gather()`` to run
    several FFmpeg operations in parallel::

        await asyncio.gather(
            run_ffmpeg_async(audio_args, config=config),
            run_ffmpeg_async(preview_args, config=config),
        )

    Raises:
        MediaProcessingError: On non-zero exit code, timeout, or missing binary.
    """
    cfg = config or AppConfig()
    ffmpeg_args = list(args)
    if (
        getattr(cfg, "ffmpeg_hwaccel", "auto") == "auto"
        and "-hwaccel" not in ffmpeg_args
        and "-i" in ffmpeg_args
    ):
        i_index = ffmpeg_args.index("-i")
        ffmpeg_args = ffmpeg_args[:i_index] + ["-hwaccel", "auto"] + ffmpeg_args[i_index:]

    cmd = [cfg.ffmpeg_path] + ffmpeg_args
    logger.debug("FFmpeg (async): %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise MediaProcessingError(f"FFmpeg timed out after {timeout}s")

        if proc.returncode != 0:
            err = (stderr_data or b"").decode(errors="replace")[:500]
            raise MediaProcessingError(
                f"FFmpeg failed (exit {proc.returncode}): {err}"
            )
    except FileNotFoundError:
        raise MediaProcessingError(
            f"FFmpeg not found at '{cfg.ffmpeg_path}'. "
            "Ensure FFmpeg is installed and in PATH."
        )


def run_ffprobe(
    args: list[str],
    config: Optional[AppConfig] = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run an FFprobe command and return the result."""
    cfg = config or AppConfig()
    cmd = [cfg.ffprobe_path] + args

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result
    except FileNotFoundError:
        raise MediaProcessingError(
            f"FFprobe not found at '{cfg.ffprobe_path}'. "
            "Ensure FFmpeg is installed and in PATH."
        )


def get_video_duration(video_path: str | Path, config: Optional[AppConfig] = None) -> float:
    """Get video duration in seconds using FFprobe."""
    result = run_ffprobe(
        [
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(video_path),
        ],
        config=config,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise MediaProcessingError(f"Cannot determine duration of {video_path}")


def _get_video_codec(video_path: str | Path, config: Optional[AppConfig] = None) -> str:
    """Return the primary video codec name in lowercase, or empty string."""
    result = run_ffprobe(
        [
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(video_path),
        ],
        config=config,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip().lower()


# ---------------------------------------------------------------------------
# Video processing
# ---------------------------------------------------------------------------

def copy_video(
    src_path: str | Path,
    dst_path: str | Path,
    config: Optional[AppConfig] = None,
    force_reencode: bool = False,
) -> Path:
    """Copy a video file to the destination, creating dirs as needed.

    Keep source video bytes unchanged unless re-encoding is explicitly forced
    or the destination extension requires format conversion.
    """
    src = Path(src_path)
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        raise MediaProcessingError(f"Source video not found: {src}")

    same_extension = src.suffix.lower() == dst.suffix.lower()
    cfg = config or AppConfig()
    vp9_mode = getattr(cfg, "vp9_handling_mode", "reencode_to_vp8")
    src_codec = _get_video_codec(src, config=config)
    looks_like_vp9_variant = src.name.lower().endswith(".vp9.webm")
    source_is_vp9 = src_codec == "vp9" or looks_like_vp9_variant
    requires_vp9_compat_transcode = source_is_vp9 and vp9_mode == "reencode_to_vp8"

    if not force_reencode and same_extension and not requires_vp9_compat_transcode:
        if source_is_vp9 and vp9_mode == "fallback_compatible_down":
            logger.warning(
                "VP9 source reached install with fallback_compatible_down mode; "
                "copying original file unchanged: %s",
                src.name,
            )
        shutil.copy2(src, dst)
        logger.debug("Copied video: %s -> %s", src.name, dst)
        return dst

    # Conversion path is only used when caller explicitly requests it or when
    # output container differs from the source extension. We also transcode VP9
    # sources to VP8 for better JD2021 runtime compatibility.
    logger.debug("Converting video to target format: %s -> %s", src.name, dst.name)
    run_ffmpeg(
        [
           "-y",
            "-hwaccel", "auto",
            "-i", str(src),
            "-an",
            "-c:v", "libvpx",
            "-pix_fmt", "yuv420p",
            # --- THE CHANGES START HERE ---
            "-deadline", "good",      # 'best' is overkill; 'good' is the standard high-quality
            "-cpu-used", "2",          # 0-1 is extremely slow. 2 is the "sweet spot" for quality/speed
            "-row-mt", "1",            # CRITICAL: Enables multithreading for VP8
            "-threads", "0",           # Use all available CPU cores
            # ------------------------------
            "-b:v", "8500k",
            "-maxrate", "11000k",
            "-bufsize", "22000k",
            "-qmin", "4",
            "-qmax", "32",
            "-g", "25",
            "-keyint_min", "25",
            "-sc_threshold", "0",
            str(dst),
        ],
        config=config,
    )
    logger.debug("Converted video: %s -> %s", src.name, dst)
    return dst


def generate_map_preview(
    video_path: str | Path,
    output_path: str | Path,
    start_time: float = 0.0,
    duration: float = 30.0,
    config: Optional[AppConfig] = None,
) -> Path:
    """Generate a map preview video clip from the main video.

    Creates a lower-quality excerpt for the map selection screen.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    run_ffmpeg(
        [
            "-y",
            "-i", str(video_path),
            "-ss", str(start_time),
            "-t", str(duration),
            "-c:v", "libvpx-vp9",
            "-b:v", "4M",
            "-an",
            str(output),
        ],
        config=config,
    )
    return output


# ---------------------------------------------------------------------------
# Audio processing
# ---------------------------------------------------------------------------

def copy_audio(
    src_path: str | Path,
    dst_path: str | Path,
) -> Path:
    """Copy or transcode an audio file to the destination."""
    src = Path(src_path)
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        raise MediaProcessingError(f"Source audio not found: {src}")

    # JD2017 PC requires .wav for stable engine compatibility.
    # If source is .ogg but we are writing .wav, let's transcode:
    if src.suffix.lower() == ".ogg" and dst.suffix.lower() == ".wav":
        logger.debug("Transcoding OGG -> WAV: %s -> %s", src.name, dst.name)
        run_ffmpeg([
            "-y",
            "-i", str(src),
            "-c:a", "pcm_s16le",
            "-ar", "48000",
            str(dst)
        ])
    else:
        shutil.copy2(src, dst)
        logger.debug("Copied audio: %s -> %s", src.name, dst)
        
    return dst


def generate_audio_preview(
    audio_path: str | Path,
    output_path: str | Path,
    start_time: float = 0.0,
    duration: float = 30.0,
    fade_out: float = 2.0,
    config: Optional[AppConfig] = None,
) -> Path:
    """Generate an audio preview with fade-out."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    run_ffmpeg(
        [
            "-y",
            "-i", str(audio_path),
            "-ss", str(start_time),
            "-t", str(duration),
            "-af", f"afade=t=out:st={duration - fade_out}:d={fade_out}",
            "-c:a", "libvorbis",
            str(output),
        ],
        config=config,
    )
    return output


def convert_audio(
    audio_path: str | Path,
    map_name: str,
    target_dir: str | Path,
    a_offset: float = 0.0,
    config: Optional[AppConfig] = None,
) -> None:
    """Process map audio, handling .ckd extraction, padding, and previews.
    
    Generates both .wav (for PC) and .ogg (for menu preview).
    """
    audio_path = Path(audio_path)
    target_dir = Path(target_dir)
    wav_out = target_dir / "audio" / f"{map_name}.wav"
    ogg_out = target_dir / "audio" / f"{map_name}.ogg"
    video_out = target_dir / "videoscoach" / f"{map_name}.webm"
    video_out.parent.mkdir(parents=True, exist_ok=True)
    wav_out.parent.mkdir(parents=True, exist_ok=True)

    # 1. Handle .ckd extraction if needed
    effective_audio = audio_path
    temp_dir = target_dir / "_temp_audio"
    if audio_path.suffix.lower() == ".ckd":
        temp_dir.mkdir(parents=True, exist_ok=True)
        extracted = extract_ckd_audio_v1(audio_path, temp_dir, config=config)
        if extracted:
            effective_audio = Path(extracted)
            logger.debug("Using extracted audio payload for conversion: %s", effective_audio.name)
        else:
            logger.warning(
                "Could not decode CKD audio '%s'; generating silent fallback audio to allow install.",
                audio_path.name,
            )
            wav_out.parent.mkdir(parents=True, exist_ok=True)
            _write_silent_stereo_wav(wav_out, duration_s=1.0)
            if not ogg_out.exists():
                if not _write_silent_ogg(ogg_out, config=config):
                    # Last-resort fallback if ffmpeg/ogg encode fails
                    shutil.copy2(wav_out, ogg_out)
            return

    try:
        # 2. Generate menu preview OGG
        if not ogg_out.exists():
            if effective_audio.suffix.lower() == ".ogg":
                logger.debug("Copying menu preview OGG...")
                shutil.copy2(effective_audio, ogg_out)
            else:
                logger.debug("Converting to menu preview OGG...")
                run_ffmpeg(["-y", "-i", str(effective_audio), str(ogg_out)], config=config)

        # 3. Generate engine WAV with offset/alignment
        if a_offset == 0.0:
            logger.debug("Converting to 48kHz WAV (no offset)...")
            run_ffmpeg(["-y", "-i", str(effective_audio), "-ar", "48000", str(wav_out)], config=config)
        elif a_offset < 0:
            trim_s = abs(a_offset)
            logger.debug("Converting to 48kHz WAV (trimming first %.3fs)...", trim_s)
            run_ffmpeg([
                "-y", "-i", str(effective_audio), 
                "-ss", f"{trim_s:.6f}",
                "-ar", "48000", str(wav_out)
            ], config=config)
        else:
            delay_ms = int(a_offset * 1000)
            logger.debug("Converting to 48kHz WAV (padding %dms silence)...", delay_ms)
            af_filter = f"adelay={delay_ms}|{delay_ms},asetpts=PTS-STARTPTS"
            run_ffmpeg([
                "-y", "-i", str(effective_audio), 
                "-af", af_filter,
                "-ar", "48000", str(wav_out)
            ], config=config)
    finally:
        # Cleanup temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def apply_audio_gain(
    audio_path: str | Path,
    gain_db: float,
    config: Optional[AppConfig] = None,
) -> Path:
    """Apply FFmpeg gain to a single audio file in-place.

    Uses a temporary sibling file and atomic replace to avoid leaving
    a partially written output if FFmpeg fails.
    """
    src = Path(audio_path)
    if not src.exists():
        raise MediaProcessingError(f"Audio file not found: {src}")

    tmp = src.with_name(src.stem + ".gain_tmp" + src.suffix)
    gain_expr = f"volume={gain_db}dB"

    run_ffmpeg(
        [
            "-y",
            "-i", str(src),
            "-af", gain_expr,
            str(tmp),
        ],
        config=config,
    )

    tmp.replace(src)
    logger.debug("Applied %+0.1fdB gain: %s", gain_db, src.name)
    return src




def generate_intro_amb(
    ogg_path: str | Path,
    map_name: str,
    target_dir: str | Path,
    a_offset: float,
    v_override: Optional[float] = None,
    marker_preroll_ms: Optional[float] = None,
    attempt_enabled: bool = True,
    config: Optional[AppConfig] = None,
) -> None:
    """Ported from V1: Generate an intro AMB WAV for negative videoStartTime.
    
    Strategy: AMB plays from t=0, covering silence before main WAV.
    """
    ogg_path = Path(ogg_path)
    target_dir = Path(target_dir)
    map_lower = map_name.lower()
    # Resolve AMB directory with compatibility for legacy lower-case installs.
    amb_candidates = [
        target_dir / "Audio" / "AMB",
        target_dir / "audio" / "AMB",
        target_dir / "Audio" / "amb",
        target_dir / "audio" / "amb",
    ]
    amb_dir = next((p for p in amb_candidates if p.exists()), amb_candidates[0])

    if not attempt_enabled:
        amb_dir.mkdir(parents=True, exist_ok=True)
        intro_wavs = list(amb_dir.glob("*_intro.wav"))
        if not intro_wavs:
            intro_wavs = [amb_dir / f"amb_{map_lower}_intro.wav"]
        for wav in intro_wavs:
            _write_silent_stereo_wav(wav)
        logger.warning(
            "Intro AMB attempt disabled: wrote silent intro WAV(s) for '%s'",
            map_name,
        )
        return

    # If no pre-roll silence, silence any existing intro WAV
    if a_offset >= 0 and (v_override is None or v_override >= 0):
        if amb_dir.exists():
            for wav in amb_dir.glob("*_intro.wav"):
                with wave.open(str(wav), 'w') as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(48000)
                    wf.writeframes(b'\x00\x00\x00\x00' * 4800)
        return

    amb_dir.mkdir(parents=True, exist_ok=True)

    intro_dur = abs(v_override) if v_override is not None and v_override < 0 else abs(a_offset)
    # V1 parity + IPK/JDU fix: when marker pre-roll is available, use it as the
    # source intro length. Using |a_offset| alone breaks maps where a_offset=0
    # but the source audio still contains a pre-roll segment.
    source_preroll_dur = (marker_preroll_ms / 1000.0) if marker_preroll_ms is not None else abs(a_offset)
    audio_delay = max(0.0, intro_dur - source_preroll_dur)
    trim_front_s = 0.0
    
    # Marker-based AMB length is the closest match to cutter behavior whenever
    # beat marker pre-roll is available.
    if marker_preroll_ms is not None:
        audio_content_dur = marker_preroll_ms / 1000.0
        trim_front_s = max(0.0, source_preroll_dur - audio_content_dur)
        logger.debug(
            "Using marker-based AMB duration: %.3fs (front trim %.3fs)",
            audio_content_dur,
            trim_front_s,
        )
    elif a_offset < 0:
        # For Fetch/HTML maps, match intro AMB to the effective video lead-in and
        # trim the front of AMB source when audio pre-roll is longer.
        target_window = abs(v_override) if v_override is not None and v_override < 0 else abs(a_offset)
        audio_content_dur = target_window
        trim_front_s = max(0.0, source_preroll_dur - target_window)
        logger.debug("Using video-aligned AMB duration: %.3fs (front trim %.3fs)", audio_content_dur, trim_front_s)
    else:
        audio_content_dur = abs(a_offset) + 1.355
        logger.debug("Using legacy AMB duration heuristic: %.3fs", audio_content_dur)
    
    amb_duration = audio_delay + audio_content_dur

    intro_wavs = list(amb_dir.glob("*_intro.wav"))
    intro_tpls = list(amb_dir.glob("*_intro.tpl"))

    if intro_tpls:
        intro_name = intro_tpls[0].stem
        intro_wav = intro_wavs[0] if intro_wavs else amb_dir / f"{intro_name}.wav"
    else:
        if intro_wavs:
            intro_wav = intro_wavs[0]
            intro_name = intro_wav.stem
        else:
            intro_name = f"amb_{map_lower}_intro"
            intro_wav = amb_dir / f"{intro_name}.wav"

        wav_rel_path = f"world/maps/{map_lower}/audio/amb/{intro_wav.name}"
        ilu_content = f'''DESCRIPTOR =
{{
\t{{
\t\tNAME = "SoundDescriptor_Template",
\t\tSoundDescriptor_Template =
\t\t{{
\t\t\tname = "{intro_name}",
\t\t\tvolume = 0,
			category = "AMB",
\t\t\tlimitCategory = "",
\t\t\tlimitMode = 0,
\t\t\tmaxInstances = 4294967295,
\t\t\tfiles =
\t\t\t{{
\t\t\t\t{{
\t\t\t\t\tVAL = "{wav_rel_path}",
\t\t\t\t}},
\t\t\t}},
\t\t\tserialPlayingMode = 0,
\t\t\tserialStoppingMode = 0,
\t\t\tparams =
\t\t\t{{
\t\t\t\tNAME = "SoundParams",
\t\t\t\tSoundParams =
\t\t\t\t{{
\t\t\t\t\tloop = 0,
\t\t\t\t\tplayMode = 1,
\t\t\t\t\tplayModeInput = "",
\t\t\t\t\trandomVolMin = 0,
\t\t\t\t\trandomVolMax = 0,
\t\t\t\t\tdelay = 0,
\t\t\t\t\trandomDelay = 0,
\t\t\t\t\trandomPitchMin = 1,
\t\t\t\t\trandomPitchMax = 1,
\t\t\t\t\tfadeInTime = 0,
\t\t\t\t\tfadeOutTime = 0,
\t\t\t\t\tfilterFrequency = 0,
\t\t\t\t\tfilterType = 2,
\t\t\t\t\ttransitionSampleOffset = 0,
\t\t\t\t}},
\t\t\t}},
\t\t\tpauseInsensitiveFlags = 0,
\t\t\toutDevices = 4294967295,
\t\t\tsoundPlayAfterdestroy = 0,
\t\t}},
\t}},
}}
appendTable(component.SoundComponent_Template.soundList,DESCRIPTOR)'''

        tpl_content = f'''params=
{{
\tNAME="Actor_Template",
\tActor_Template=
\t{{
\t\tCOMPONENTS=
\t\t{{
\t\t}}
\t}}
}}
includeReference("EngineData/Misc/Components/SoundComponent.ilu")
includeReference("world/maps/{map_lower}/audio/amb/{intro_name}.ilu")'''

        (amb_dir / f"{intro_name}.ilu").write_text(ilu_content, encoding="utf-8")
        (amb_dir / f"{intro_name}.tpl").write_text(tpl_content, encoding="utf-8")
        logger.debug("Created intro AMB files: %s.tpl/.ilu", intro_name)

    # Intro AMB is started from MainSequence SoundSetClip timing only.

    delay_ms = int(audio_delay * 1000)
    if delay_ms > 0:
        af_filter = f"adelay={delay_ms}|{delay_ms},asetpts=PTS-STARTPTS"
        logger.debug("Intro audio delayed by %.3fs", audio_delay)
    else:
        af_filter = ""

    ffmpeg_args = ["-y"]
    if trim_front_s > 0:
        ffmpeg_args += ["-ss", f"{trim_front_s:.3f}"]
    ffmpeg_args += [
        "-t", f"{audio_content_dur:.3f}",
        "-i", str(ogg_path),
    ]
    if af_filter:
        ffmpeg_args += ["-af", af_filter]
    ffmpeg_args += [
        "-ar", "48000", str(intro_wav)
    ]
    run_ffmpeg(ffmpeg_args, config=config)
    logger.debug("Generated intro AMB: %s (%.3fs)", intro_wav.name, amb_duration)


def extract_amb_clips(
    cinematic_tape: CinematicTape,
    audio_path: Path,
    target_dir: Path,
    codename: str,
    config: Optional[AppConfig] = None,
) -> int:
    """Extract audio clips for cinematic ambient sounds (V1 parity).

    Scans the cinematic tape for SoundSetClips and extracts them from the
    main audio source using each clip's timeline position and duration.
    """
    if not cinematic_tape or not audio_path.exists():
        return 0

    count = 0
    map_lower = codename.lower()
    amb_dir = target_dir / "audio" / "amb"
    amb_dir.mkdir(parents=True, exist_ok=True)

    for clip in cinematic_tape.clips:
        if not hasattr(clip, "sound_set_path"):
            continue

        clip_name = clip.sound_set_path.split("/")[-1].split(".")[0]
        # Skip if it's the main intro (which we handle separately)
        if f"amb_{map_lower}_intro" in clip_name:
            continue

        output_wav = amb_dir / f"{clip_name}.wav"
        # If it's already a real file (>100KB), don't overwrite
        if output_wav.exists() and output_wav.stat().st_size > 102400:
            continue

        start_s = max(0.0, clip.start_time / 1000.0)
        duration_s = clip.duration / 1000.0
        fade_start = max(0, duration_s - 0.200)

        logger.debug(
            "Extracting cinematic AMB clip: %s (start %.3fs, duration %.3fs)",
            clip_name,
            start_s,
            duration_s,
        )
        ffmpeg_args = ["-y"]
        if start_s > 0:
            ffmpeg_args += ["-ss", f"{start_s:.6f}"]
        ffmpeg_args += [
            "-i", str(audio_path),
            "-t", f"{duration_s:.6f}",
            "-af", f"afade=t=out:st={fade_start:.6f}:d=0.200",
            "-ar", "48000", str(output_wav),
        ]
        run_ffmpeg(ffmpeg_args, config=config)
        count += 1

    return count



# ---------------------------------------------------------------------------
# Image processing (Pillow)
# ---------------------------------------------------------------------------

def convert_image(
    src_path: str | Path,
    dst_path: str | Path,
    target_size: Optional[tuple[int, int]] = None,
) -> Path:
    """Convert an image file to a different format, optionally resizing.

    Uses Pillow for format detection and conversion.
    """
    try:
        from PIL import Image
    except ImportError:
        raise MediaProcessingError("Pillow is not installed. Run: pip install Pillow")

    src = Path(src_path)
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        raise MediaProcessingError(f"Source image not found: {src}")

    img = Image.open(src)
    if target_size:
        img = img.resize(target_size, Image.Resampling.LANCZOS)

    img.save(dst)
    logger.debug("Converted image: %s -> %s", src.name, dst)
    return dst


def generate_cover_tga(
    src_path: str | Path,
    dst_path: str | Path,
    size: tuple[int, int] = (720, 720),
) -> Path:
    """Convert a cover image to TGA format for the game engine."""
    res = convert_image(src_path, dst_path, target_size=size)
    # Re-save as uncompressed 32-bit RGBA TGA for engine compatibility.
    try:
        from PIL import Image
        img = Image.open(res)
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        img.save(res, format='TGA')
    except Exception as exc:
        logger.warning("TGA re-save failed for %s: %s", Path(dst_path).name, exc)
    return res


# ---------------------------------------------------------------------------
# vgmstream — Xbox 360 XMA2 audio decoding
# ---------------------------------------------------------------------------

# Default paths relative to project root (V1 parity)
VGMSTREAM_DEFAULT_PATHS = (
    Path("tools/vgmstream/vgmstream-cli.exe"),
    Path("tools/vgmstream/vgmstream.exe"),
)


def _resolve_vgmstream_binary(vgmstream_path: Optional[str | Path] = None) -> Path:
    """Locate a usable vgmstream binary.

    Resolution order:
    - explicit path parameter
    - local tools/vgmstream binaries
    - PATH lookup
    """
    if vgmstream_path:
        candidate = Path(vgmstream_path).expanduser().resolve()
        if candidate.exists():
            return candidate

    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root / rel for rel in VGMSTREAM_DEFAULT_PATHS]

    for command_name in ("vgmstream-cli.exe", "vgmstream.exe"):
        on_path = shutil.which(command_name)
        if on_path:
            candidates.append(Path(on_path))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    checked = "\n  - ".join(str(p) for p in candidates)
    raise MediaProcessingError(
        "vgmstream binary not found. Checked:\n"
        f"  - {checked}\n"
        "Install vgmstream in tools/vgmstream/ (run setup.bat to auto-install)."
    )


def _find_audio_magic_offset(data: bytes, search_limit: int = 4 * 1024 * 1024) -> tuple[int | None, str | None]:
    """Find RIFF/OggS magic in a CKD payload region.

    Some bundles prepend metadata blocks larger than 44 bytes before the audio
    stream starts. Search a wider window so we can recover standard payloads
    without requiring vgmstream.
    """
    if not data:
        return None, None

    window = data[: max(0, min(len(data), search_limit))]
    riff_offset = window.find(b"RIFF")
    ogg_offset = window.find(b"OggS")

    if riff_offset >= 0 and (ogg_offset < 0 or riff_offset <= ogg_offset):
        return riff_offset, ".wav"
    if ogg_offset >= 0:
        return ogg_offset, ".ogg"
    return None, None


def _ffmpeg_decode_unknown_payload(payload_file: Path, output_wav: Path) -> bool:
    """Attempt to decode unknown payloads with FFmpeg directly."""
    try:
        run_ffmpeg([
            "-y",
            "-i", str(payload_file),
            "-ar", "48000",
            "-ac", "2",
            str(output_wav),
        ])
        return output_wav.exists()
    except Exception as exc:
        logger.debug("FFmpeg unknown-payload decode failed for %s: %s", payload_file.name, exc)
        return False


# Ported from V1: _extract_ckd_audio
CKD_HEADER_SIZE = 44

def extract_ckd_audio_v1(
    ckd_path: str | Path,
    output_dir: str | Path,
    config: Optional[AppConfig] = None,
) -> Optional[str]:
    """Strip the 44-byte CKD header from a cooked audio file and write raw audio.
    
    Ported from V1 source_analysis.py.
    For standard OGG/WAV payloads the header is simply stripped.
    For X360 proprietary formats (XMA etc.) vgmstream is used to decode to WAV.
    """
    ckd_path = Path(ckd_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        data = ckd_path.read_bytes()
    except OSError:
        return None

    if len(data) <= CKD_HEADER_SIZE:
        return None

    base = ckd_path.stem
    if base.lower().endswith(".wav") or base.lower().endswith(".ogg"):
        base = base[:-4]

    # V1 Parity: try vgmstream on raw Xbox 360 .wav.ckd first.
    # For non-.wav.ckd payloads this probe is often unnecessary and can stall.
    should_try_raw_vgm = ckd_path.name.lower().endswith(".wav.ckd")
    skip_secondary_vgm_retry = False

    vgm_raw_out = output_dir / f"{base}_raw_vgm.wav"
    if should_try_raw_vgm:
        try:
            decoded = decode_xma2_audio(
                ckd_path,
                vgm_raw_out,
                vgmstream_path=getattr(config, "vgmstream_path", None),
                timeout=45,
            )
            if decoded and decoded.exists():
                if is_valid_wav(decoded):
                    logger.debug("Decoded raw CKD directly via vgmstream: %s", decoded.name)
                    return str(decoded)
                else:
                    # Transcode to 48kHz Stereo if it's a valid audio file but wrong format
                    logger.debug("Decoded WAV has wrong format; transcoding to 48kHz Stereo...")
                    fixed_wav = output_dir / f"{base}_fixed.wav"
                    run_ffmpeg(["-y", "-i", str(decoded), "-ar", "48000", "-ac", "2", str(fixed_wav)])
                    if fixed_wav.exists():
                        return str(fixed_wav)
        except Exception as e:
            logger.debug("vgmstream raw attempt failed: %s", e)
            if "timed out" in str(e).lower():
                # Do not run another long vgmstream decode pass on the same input.
                skip_secondary_vgm_retry = True
            if vgm_raw_out.exists():
                vgm_raw_out.unlink()

    # Fallback: Strip header and try again
    payload = data[CKD_HEADER_SIZE:]
    ext = None

    if payload[:4] == b"OggS":
        ext = ".ogg"
    elif payload[:4] == b"RIFF":
        ext = ".wav"
    else:
        # CKD header may be much larger than 44 bytes.
        magic_offset, magic_ext = _find_audio_magic_offset(data)
        if magic_offset is not None and magic_ext:
            payload = data[magic_offset:]
            ext = magic_ext
        else:
            # Proprietary format (XMA, etc.) -- write payload and try decoders.
            temp_payload = output_dir / f"{base}_payload.bin"
            temp_payload.write_bytes(payload)
            out_path = output_dir / (base + "_decoded.wav")

            try:
                try:
                    if not skip_secondary_vgm_retry:
                        decoded = decode_xma2_audio(
                            temp_payload,
                            out_path,
                            vgmstream_path=getattr(config, "vgmstream_path", None),
                            timeout=45,
                        )
                        if decoded and decoded.exists():
                            if is_valid_wav(decoded):
                                return str(decoded)
                            logger.debug("Fallback decoded WAV has wrong format; transcoding...")
                            fixed_wav = output_dir / f"{base}_fallback_fixed.wav"
                            run_ffmpeg(["-y", "-i", str(decoded), "-ar", "48000", "-ac", "2", str(fixed_wav)])
                            if fixed_wav.exists():
                                return str(fixed_wav)
                    else:
                        logger.debug(
                            "Skipping secondary vgmstream retry for %s after prior timeout",
                            ckd_path.name,
                        )
                except Exception as e:
                    logger.debug("vgmstream fallback failed for payload %s: %s", ckd_path.name, e)

                ffmpeg_out = output_dir / f"{base}_ffmpeg_fallback.wav"
                if _ffmpeg_decode_unknown_payload(temp_payload, ffmpeg_out):
                    logger.debug("Recovered audio using FFmpeg fallback: %s", ffmpeg_out.name)
                    return str(ffmpeg_out)
                return None
            finally:
                if temp_payload.exists():
                    temp_payload.unlink()

    # Standard OGG/WAV payload found
    out_path = output_dir / (base + ext)
    out_path.write_bytes(payload)
    logger.debug("Extracted %s payload from CKD: %s", ext.upper()[1:], out_path.name)
    return str(out_path)


def is_valid_wav(path: str | Path) -> bool:
    """Check if the file is a valid 48kHz Stereo WAV.
    
    Ported from V1 metadata checks.
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".wav":
        return False
    try:
        with wave.open(str(path), "rb") as wf:
            n_channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            is_valid = (n_channels == 2 and sample_rate == 48000)
            if not is_valid:
                logger.debug("WAV validation failed for %s: %dch %dHz", path.name, n_channels, sample_rate)
            return is_valid
    except Exception:
        return False


def is_xma2_audio(file_path: str | Path) -> bool:
    """Quick check: does this look like an Xbox 360 .wav.ckd (XMA2) file?"""
    name = Path(file_path).name.lower()
    return name.endswith(".wav.ckd")


def decode_xma2_audio(
    input_ckd: str | Path,
    output_wav: str | Path,
    vgmstream_path: Optional[str | Path] = None,
    timeout: int = 120,
) -> Path:
    """Decode an Xbox 360 XMA2 audio file to WAV using vgmstream-cli."""
    input_ckd = Path(input_ckd)
    output_wav = Path(output_wav)
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    vgm_bin = _resolve_vgmstream_binary(vgmstream_path)

    cmd = [str(vgm_bin), "-o", str(output_wav), str(input_ckd)]
    logger.debug("Decoding X360 audio: %s", input_ckd.name)
    logger.debug("vgmstream cmd: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.stdout:
            logger.debug("vgmstream stdout: %s", result.stdout.strip())
        logger.debug("Decoded X360 audio → %s", output_wav.name)
        return output_wav
    except subprocess.CalledProcessError as e:
        if e.returncode == 3221225781:
            raise MediaProcessingError(
                "vgmstream failed (0xC0000135: missing runtime dependency). "
                "Re-run setup.bat so tools/vgmstream includes required DLL files."
            )
        raise MediaProcessingError(
            f"vgmstream failed (exit {e.returncode}):\n"
            f"  stdout: {e.stdout[:300]}\n  stderr: {e.stderr[:300]}"
        )
    except subprocess.TimeoutExpired:
        raise MediaProcessingError(
            f"vgmstream timed out after {timeout}s decoding {input_ckd.name}"
        )
    except FileNotFoundError:
        raise MediaProcessingError(
            f"Could not execute vgmstream at '{vgm_bin}'. "
            "Check that the binary is not blocked by antivirus."
        )


def copy_moves(
    moves_src_dir: str | Path,
    target_dir: str | Path,
    *,
    skip_gestures: bool = False,
) -> int:
    """Extract and merge move files while preserving source platform layout.

    Args:
        moves_src_dir: The extracted root 'moves' folder containing 'nx', 'durango', etc.
        target_dir: The map's root installation target directory.
        skip_gestures: When True, do not import any .gesture files.

    Returns:
        The number of valid move files copied.
    """
    src_root = Path(moves_src_dir)
    if not src_root.is_dir():
        return 0

    def _is_probably_valid_kinect_gesture(gesture_path: Path) -> tuple[bool, str]:
        """Best-effort guard against non-Kinect or malformed gesture payloads.

        JD2021 PC only accepts Kinect v1/v2-compatible gesture binaries.
        Some newer exports can produce gesture files that are tiny or text-like;
        those are rejected before they reach the installed map.
        """
        try:
            size = gesture_path.stat().st_size
        except OSError as exc:
            return False, f"cannot stat file ({exc})"

        if size < 256:
            return False, f"file too small ({size} bytes)"

        try:
            head = gesture_path.read_bytes()[:128]
        except OSError as exc:
            return False, f"cannot read file ({exc})"

        if not head:
            return False, "empty file"

        trimmed = head.lstrip()
        if trimmed.startswith((b"{", b"[")):
            return False, "text/json-like payload"

        printable = sum(1 for b in head if 32 <= b <= 126 or b in (9, 10, 13))
        if printable / max(1, len(head)) > 0.95:
            return False, "payload appears text-like"

        return True, "ok"

    moves_target_root = Path(target_dir) / "timeline" / "moves"
    durango_moves_dir = moves_target_root / "durango"
    wiiu_moves_dir = moves_target_root / "wiiu"
    total_copied = 0
    skipped_gesture_names: set[str] = set()

    def _copy_to_canonical(src_file: Path, platform_name: str) -> bool:
        """Copy to the canonical output folder based on file extension.

        Gesture files are placed into durango/, MSM files into wiiu/.
        The original platform layout is no longer preserved separately.
        """
        ext_low = src_file.suffix.lower()
        if ext_low == ".gesture":
            canon_dir = durango_moves_dir
        elif ext_low == ".msm":
            canon_dir = wiiu_moves_dir
        else:
            canon_dir = durango_moves_dir

        dest = canon_dir / src_file.name
        if dest.exists():
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        return True

    def _copy_to_platform_preserve(src_file: Path, platform_name: str) -> bool:
        """Copy a file to its canonical folder (durango for gestures, wiiu for msm)."""
        return _copy_to_canonical(src_file, platform_name)

    def _collect_expected_gestures_from_dtape() -> set[str]:
        """Extract expected gesture filenames from installed dance tape paths."""
        expected: set[str] = set()
        timeline_dir = Path(target_dir) / "timeline"
        if not timeline_dir.is_dir():
            return expected

        dtapes = sorted(timeline_dir.glob("*_TML_Dance.dtape"))
        classifier_re = re.compile(r'ClassifierPath\s*=\s*"([^"]+)"', re.IGNORECASE)
        for dtape_path in dtapes:
            try:
                content = dtape_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for match in classifier_re.finditer(content):
                classifier_path = match.group(1).strip().replace("\\", "/")
                move_name = Path(classifier_path).name
                if not move_name:
                    continue

                stem, ext = os.path.splitext(move_name)
                ext_low = ext.lower()
                if ext_low == ".gesture":
                    expected.add(move_name)
                elif ext_low == ".msm":
                    expected.add(f"{stem}.gesture")

        return expected

    KINECT_GESTURE_PLATFORMS = {"DURANGO", "X360"}

    if skip_gestures:
        logger.debug("Gesture import disabled for this source; only .msm files will be copied.")

    # Pass 1: Keep original platform paths and mirror files into PC.
    for plat_dir in src_root.iterdir():
        if not plat_dir.is_dir():
            continue

        plat_name = plat_dir.name
        plat_upper = plat_name.upper()

        if skip_gestures:
            skipped = list(plat_dir.rglob("*.gesture"))
            if skipped:
                skipped_gesture_names.update(g.name for g in skipped)
                logger.info(
                    "Skipping %d gesture file(s) from platform '%s' (source flagged incompatible)",
                    len(skipped),
                    plat_upper,
                )
                for gesture_file in skipped:
                    if _copy_to_platform_preserve(gesture_file, plat_name):
                        total_copied += 1
        elif plat_upper in KINECT_GESTURE_PLATFORMS:
            for gesture_file in plat_dir.rglob("*.gesture"):
                is_valid, reason = _is_probably_valid_kinect_gesture(gesture_file)
                if not is_valid:
                    logger.warning(
                        "Skipping incompatible gesture '%s' from %s: %s",
                        gesture_file.name,
                        plat_upper,
                        reason,
                    )
                    continue
                if _copy_to_canonical(gesture_file, plat_name):
                    total_copied += 1
        else:
            # Non-Kinect gestures are still preserved and duplicated for users
            # who need cross-platform originals alongside the PC mirror.
            for gesture_file in plat_dir.rglob("*.gesture"):
                if _copy_to_canonical(gesture_file, plat_name):
                    total_copied += 1

        for msm_file in plat_dir.rglob("*.msm"):
            if _copy_to_canonical(msm_file, plat_name):
                total_copied += 1

    # Pass 2: Substitute non-Kinect naming variants with already accepted gestures
    # (disabled when gesture import is explicitly skipped)
    if not skip_gestures:
        durango_gestures = {f.name for f in durango_moves_dir.glob("*.gesture")} if durango_moves_dir.is_dir() else set()

        for plat_dir in src_root.iterdir():
            if (
                not plat_dir.is_dir()
                or plat_dir.name.upper() in KINECT_GESTURE_PLATFORMS
            ):
                continue

            for gesture_file in plat_dir.glob("*.gesture"):
                fname = gesture_file.name
                if fname in durango_gestures or (durango_moves_dir / fname).exists():
                    continue

                stem = gesture_file.stem
                base = stem.rstrip("0123456789")
                sub_src = durango_moves_dir / (base + ".gesture")

                if base != stem and sub_src.exists():
                    durango_moves_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sub_src, durango_moves_dir / fname)
                    total_copied += 1

    # Recovery path: synthesize expected classifier names from a known-good
    # Kinect template (prefer discorope). Missing names are derived from
    # skipped gestures and dance-tape classifier references.
    expected_names: set[str] = set(skipped_gesture_names)
    expected_names.update(_collect_expected_gestures_from_dtape())

    expected_names = {
        name
        for name in expected_names
        if name and not (durango_moves_dir / name).exists()
    }

    if expected_names:
        def _pick_template_gesture() -> Optional[Path]:
            bundled_candidates = [
                Path(__file__).resolve().parents[2] / "assets" / "gesture_templates" / "durango_template.gesture",
                Path(__file__).resolve().parents[2] / "assets" / "gesture_templates" / "discorope.gesture",
            ]
            for candidate in bundled_candidates:
                if candidate.exists():
                    ok, _ = _is_probably_valid_kinect_gesture(candidate)
                    if ok:
                        return candidate

            explicit_candidates: list[Path] = []
            for plat_dir in src_root.iterdir():
                if not plat_dir.is_dir():
                    continue
                explicit_candidates.extend([
                    plat_dir / "discorop.gesture",
                    plat_dir / "Discorop.gesture",
                    plat_dir / "discorope.gesture",
                    plat_dir / "Discorope.gesture",
                    plat_dir / "generic.gesture",
                    plat_dir / "Generic.gesture",
                ])

            for candidate in explicit_candidates:
                if candidate.exists():
                    ok, _ = _is_probably_valid_kinect_gesture(candidate)
                    if ok:
                        return candidate

            return None

        template = _pick_template_gesture()
        if template is None:
            logger.warning(
                "No valid gesture template found; cannot synthesize %d missing gesture file(s).",
                len(expected_names),
            )
        else:
            durango_moves_dir.mkdir(parents=True, exist_ok=True)
            created = 0
            for name in sorted(expected_names):
                dest = durango_moves_dir / name
                if dest.exists():
                    continue
                shutil.copy2(template, dest)
                created += 1

            if created:
                total_copied += created
                logger.info(
                    "Synthesized %d fallback gesture file(s) from template '%s'.",
                    created,
                    template.name,
                )
                if skip_gestures and not skipped_gesture_names:
                    logger.info(
                        "While camera gesture conversion is turned on, %s will be used because the map being installed does not have camera gestures.",
                        template.name,
                    )

    if total_copied:
        logger.debug("Merged %d gesture/msm file(s) from %s into durango/wiiu", total_copied, src_root)

    return total_copied


def process_menu_art(target_dir: str | Path, codename: str) -> int:
    """Validate, heal, and duplicate MenuArt textures for parity.

    Ensures both cover_generic and cover_online exist, and re-saves
    all covers as 32-bit RGBA TGAs to ensure game engine compatibility.
    """
    tex_dir = Path(target_dir) / "menuart" / "textures"
    if not tex_dir.is_dir():
        logger.debug("MenuArt textures directory not found at %s", tex_dir)
        return 0

    codename_low = codename.lower()
    expected_suffixes = [
        "cover_generic", "cover_online", "cover_albumbkg",
        "cover_albumcoach", "banner_bkg", "map_bkg"
    ]
    expected_tgas = [f"{codename}_{s}.tga" for s in expected_suffixes]

    # Build case-insensitive lookup
    actual_files = list(tex_dir.iterdir())
    actual_lower_map = {f.name.lower(): f for f in actual_files if f.is_file()}

    found_tgas = {}
    for expected in expected_tgas:
        actual_path = actual_lower_map.get(expected.lower())
        if actual_path:
            # Fix case mismatch
            if actual_path.name != expected:
                new_path = tex_dir / expected
                actual_path.rename(new_path)
                actual_path = new_path
            found_tgas[expected.lower()] = actual_path
        else:
            logger.debug("Missing MenuArt: %s", expected)

    # If canonical TGA files are missing but PNG/JPG variants exist, convert them.
    if Image is not None:
        for expected in expected_tgas:
            key = expected.lower()
            if key in found_tgas:
                continue

            stem = Path(expected).stem
            source_candidate = None
            for ext in (".png", ".jpg", ".jpeg", ".tga"):
                cand = actual_lower_map.get(f"{stem}{ext}".lower())
                if cand and cand.is_file():
                    source_candidate = cand
                    break

            if source_candidate is None:
                continue

            out_path = tex_dir / expected
            try:
                with Image.open(source_candidate) as img:
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                    img.save(out_path, format="TGA")
                found_tgas[key] = out_path
                actual_lower_map[out_path.name.lower()] = out_path
                logger.debug("Canonicalized MenuArt to TGA: %s -> %s", source_candidate.name, out_path.name)
            except Exception as e:
                logger.debug("Failed to canonicalize MenuArt %s: %s", source_candidate.name, e)

        # Coaches are also actor-referenced as TGA; synthesize from PNG when needed.
        for png_coach in sorted(tex_dir.glob(f"{codename}_coach_*.png")):
            if "_phone" in png_coach.stem.lower():
                continue
            tga_coach = tex_dir / f"{png_coach.stem}.tga"
            if tga_coach.exists():
                continue
            try:
                with Image.open(png_coach) as img:
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                    img.save(tga_coach, format="TGA")
                logger.debug("Canonicalized coach texture to TGA: %s", tga_coach.name)
            except Exception as e:
                logger.debug("Failed to canonicalize coach texture %s: %s", png_coach.name, e)

    # V1 Parity Synthesis Logic
    online_key = f"{codename}_cover_online.tga".lower()
    generic_key = f"{codename}_cover_generic.tga".lower()

    def _synthesize(dst_key: str, src_key: str, desc: str) -> None:
        if dst_key not in found_tgas and src_key in found_tgas:
            # Find the expected case for the destination
            for expected in expected_tgas:
                if expected.lower() == dst_key:
                    dst_name = expected
                    break
            else:
                dst_name = dst_key
            
            dst_path = tex_dir / dst_name
            logger.debug("Synthesizing missing %s from %s", desc, src_key)
            try:
                shutil.copy2(found_tgas[src_key], dst_path)
                found_tgas[dst_key] = dst_path
            except Exception as e:
                logger.error("Failed to synthesize %s: %s", dst_name, e)

    _synthesize(online_key, generic_key, "cover_online")
    _synthesize(generic_key, online_key, "cover_generic")

    # Re-save as 32-bit RGBA TGA (V1 Parity)
    if Image is None:
        logger.debug("Pillow not installed; skipping TGA re-save/healing")
        return 0

    resaved = 0
    for path in found_tgas.values():
        try:
            with Image.open(path) as img:
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                # Save as uncompressed TGA
                img.save(path, format='TGA')
                resaved += 1
        except Exception as e:
            logger.debug("Could not re-save MenuArt %s: %s", path.name, e)

    if resaved:
        logger.debug("Validated and re-saved %d MenuArt TGA(s) as uncompressed 32-bit RGBA", resaved)
    
    return resaved


