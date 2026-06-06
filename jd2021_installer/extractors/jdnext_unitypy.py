"""Standalone JDNext bundle unpack pass powered by UnityPy.

This module is intentionally decoupled from the installer pipeline so we can
inspect real JDNext bundle payloads before wiring the stage into production
extract/install flow.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.fs_utils import write_json

logger = logging.getLogger("jd2021.extractors.jdnext_unitypy")

_DEFAULT_UNITY_FALLBACK_VERSION = "2021.3.0f1"

# Cached reference to the UnityPy module so repeated calls within a session
# skip the sys.path manipulation and FALLBACK_UNITY_VERSION patching.
_unitypy_cache: Any = None


@dataclass
class JDNextUnpackSummary:
    bundle_path: str
    output_dir: str
    total_objects: int = 0
    exported_objects: int = 0
    textures: int = 0
    text_assets: int = 0
    mono_behaviours: int = 0
    audio_clips: int = 0
    video_clips: int = 0
    json_typetrees: int = 0
    unknown_objects: int = 0
    failed_objects: int = 0


def _safe_name(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = fallback
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._")
    return safe or fallback


def _load_unitypy(config: AppConfig | None = None) -> Any:
    """Import UnityPy from site-packages or a local tools clone.

    The result is cached at module level so repeated calls within the same
    process incur no additional I/O or sys.path manipulation.
    """
    global _unitypy_cache
    if _unitypy_cache is not None:
        return _unitypy_cache

    try:
        unitypy = importlib.import_module("UnityPy")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[2]
        local_roots: list[Path] = [repo_root / "tools"]

        local_unitypy = None
        for root in local_roots:
            candidate = root / "UnityPy"
            if candidate.exists():
                local_unitypy = candidate
                break

        if local_unitypy is not None:
            local_path = str(local_unitypy)
            if local_path not in sys.path:
                sys.path.insert(0, local_path)
            unitypy = importlib.import_module("UnityPy")
        else:
            raise RuntimeError(
                "UnityPy is not available. Install it or clone it to "
                "tools/UnityPy."
            )

    try:
        cfg = importlib.import_module("UnityPy.config")
    except ModuleNotFoundError:
        cfg = None
    if cfg is not None and getattr(cfg, "FALLBACK_UNITY_VERSION", None) in (None, ""):
        cfg.FALLBACK_UNITY_VERSION = _DEFAULT_UNITY_FALLBACK_VERSION

    _unitypy_cache = unitypy
    return unitypy


def _extract_encryption_hints(error_text: str) -> dict[str, str]:
    key_sig = ""
    data_sig = ""
    key_match = re.search(r"key_sig\s*=\s*b'([^']+)'", error_text)
    data_match = re.search(r"data_sig\s*=\s*b'([^']+)'", error_text)
    if key_match:
        key_sig = key_match.group(1)
    if data_match:
        data_sig = data_match.group(1)
    return {"key_sig": key_sig, "data_sig": data_sig}


def unpack_jdnext_bundle_with_unitypy(
    bundle_path: str | Path,
    output_dir: str | Path,
    config: AppConfig | None = None,
) -> JDNextUnpackSummary:
    """Run a single UnityPy extraction pass for a JDNext bundle.

    Args:
        bundle_path: Path to one downloaded JDNext .bundle file.
        output_dir:  Destination root for extracted diagnostics/artifacts.
        config:      Optional AppConfig carrying configuration.

    Returns:
        JDNextUnpackSummary with counts and destination paths.
    """
    bundle = Path(bundle_path)
    out_root = Path(output_dir)
    if not bundle.is_file():
        raise FileNotFoundError(f"Bundle file not found: {bundle}")

    unitypy = _load_unitypy(config=config)
    try:
        env = unitypy.load(str(bundle))
    except Exception as exc:
        msg = str(exc)
        if "encrypted" in msg.lower() and "no key was provided" in msg.lower():
            hints = _extract_encryption_hints(msg)
            raise RuntimeError(
                "JDNext bundle appears encrypted and UnityPy has no decrypt key. "
                f"key_sig={hints.get('key_sig') or 'unknown'} "
                f"data_sig={hints.get('data_sig') or 'unknown'}"
            ) from exc
        raise

    out_root.mkdir(parents=True, exist_ok=True)
    textures_dir = out_root / "textures"
    audio_dir = out_root / "audio"
    video_dir = out_root / "video"
    text_dir = out_root / "text"
    typetree_dir = out_root / "typetree"

    summary = JDNextUnpackSummary(bundle_path=str(bundle), output_dir=str(out_root))

    object_entries: list[dict[str, Any]] = []
    for obj in env.objects:
        summary.total_objects += 1
        type_name = getattr(getattr(obj, "type", None), "name", "Unknown")
        path_id = int(getattr(obj, "path_id", getattr(obj, "m_PathID", 0)) or 0)

        exported = False
        error_msg: str | None = None
        base_name = _safe_name(getattr(obj, "peek_name", lambda: "")() or "", f"obj_{path_id}")

        try:
            if type_name == "Texture2D":
                tex = obj.parse_as_object()
                name = _safe_name(getattr(tex, "m_Name", ""), base_name)
                out_file = textures_dir / f"{name}.png"
                out_file.parent.mkdir(parents=True, exist_ok=True)
                img = tex.image
                if img is None:
                    raise ValueError(
                        "tex.image is None — unsupported texture format or missing platform data"
                    )
                img.save(out_file)
                summary.textures += 1
                exported = True

            elif type_name == "AudioClip":
                clip = obj.parse_as_object()
                samples = getattr(clip, "samples", {}) or {}
                if isinstance(samples, dict) and samples:
                    for sample_name, sample_data in samples.items():
                        sample_path = Path(str(sample_name))
                        ext = sample_path.suffix or ".bin"
                        stem = _safe_name(sample_path.stem, base_name)
                        out_file = audio_dir / f"{stem}{ext}"
                        out_file.parent.mkdir(parents=True, exist_ok=True)
                        out_file.write_bytes(sample_data)
                else:
                    # Fallback if clip has no decoded samples available.
                    typetree = obj.parse_as_dict()
                    write_json(typetree_dir / f"{base_name}_audioclip.json", typetree)
                    summary.json_typetrees += 1
                summary.audio_clips += 1
                exported = True

            elif type_name == "VideoClip":
                clip = obj.parse_as_object()
                raw = getattr(clip, "m_VideoData", None)
                name = _safe_name(getattr(clip, "m_Name", ""), base_name)
                if isinstance(raw, (bytes, bytearray)) and raw:
                    (video_dir / f"{name}.bin").parent.mkdir(parents=True, exist_ok=True)
                    (video_dir / f"{name}.bin").write_bytes(bytes(raw))
                else:
                    typetree = obj.parse_as_dict()
                    write_json(typetree_dir / f"{name}_videoclip.json", typetree)
                    summary.json_typetrees += 1
                summary.video_clips += 1
                exported = True

            elif type_name == "TextAsset":
                text_obj = obj.parse_as_object()
                name = _safe_name(getattr(text_obj, "m_Name", ""), base_name)
                text_value = getattr(text_obj, "m_Script", "")
                text_dir.mkdir(parents=True, exist_ok=True)
                (text_dir / f"{name}.txt").write_text(str(text_value), encoding="utf-8", errors="ignore")
                summary.text_assets += 1
                exported = True

            elif type_name == "MonoBehaviour":
                typetree = obj.parse_as_dict()
                write_json(typetree_dir / f"{base_name}_monobehaviour.json", typetree)
                summary.mono_behaviours += 1
                summary.json_typetrees += 1
                exported = True

            else:
                # Keep one-pass diagnostics broad to learn unknown structures quickly.
                typetree = obj.parse_as_dict()
                write_json(typetree_dir / f"{base_name}_{type_name.lower()}.json", typetree)
                summary.json_typetrees += 1
                summary.unknown_objects += 1
                exported = True

        except Exception as exc:
            error_msg = str(exc)
            summary.failed_objects += 1
            logger.warning(
                "Failed to export object path_id=%s type=%s name=%s: %s",
                path_id,
                type_name,
                base_name,
                exc,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )

        if exported:
            summary.exported_objects += 1

        object_entries.append(
            {
                "path_id": path_id,
                "type": type_name,
                "name_hint": base_name,
                "exported": exported,
                **({"error": error_msg} if error_msg else {}),
            }
        )

    # Warn on high failure rate so operators know extraction is degraded.
    if summary.total_objects > 0:
        failure_rate = summary.failed_objects / summary.total_objects
        if failure_rate > 0.25:
            logger.warning(
                "High object failure rate: %d/%d objects failed to export (%.0f%%). "
                "Bundle may be encrypted or use an unsupported type-tree schema.",
                summary.failed_objects,
                summary.total_objects,
                failure_rate * 100,
            )

    write_json(out_root / "summary.json", asdict(summary))
    write_json(out_root / "objects_index.json", {"objects": object_entries})

    return summary
