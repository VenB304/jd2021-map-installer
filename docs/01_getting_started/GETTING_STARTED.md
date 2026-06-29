# Getting Started

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This guide shows the fastest way to run JD2021 Map Installer v2 on Windows, plus a full manual setup path.

---

## Current Limitations (Read First)

1. **Intro AMB generation is active and enabled by default.**
   The pipeline generates intro AMB audio for all maps with a negative `videoStartTime` (pre-roll window). Results are reliable for most maps; edge cases with unusual source layouts may still require manual offset verification.

2. **IPK-derived `videoStartTime` is approximate by design.**
   For many IPK maps, binary metadata does not reliably encode lead-in timing. Manual video offset tuning is expected.

3. **External media tools are required for full results.**
   FFmpeg/FFprobe and vgmstream are required for complete media processing paths.

4. **JDNext extraction is in active integration.**
   JDNext mapPackage extraction relies on third-party tooling (AssetStudioModCLI and/or UnityPy) under `tools/`. Both strategies are supported with automatic fallback.

---

## Quick Start (Recommended)

Use this path first. It is the intended Windows setup flow.

1. Open PowerShell or Command Prompt in the project root.
2. Run:

```bat
setup.bat
```

3. Launch the installer with:

```bat
RUN.bat
```

What `setup.bat` handles:

1. **Python check and provisioning** — if Python 3.12+ is not found on `PATH`, downloads and extracts a portable Python 3.14.0 runtime from NuGet into `tools/python/`.
2. **Git check and provisioning** — if Git is not found on `PATH`, downloads the latest MinGit (Portable Git for Windows) into `tools/git/`.
3. **Python dependencies** — installs `requirements.txt` (includes `scipy` and `pytest-qt`) via `pip`.
4. **Playwright Chromium** — runs `python -m playwright install chromium` (required for Fetch modes).
5. **AssetStudioModCLI** — downloads the latest release from [aelurum/AssetStudio](https://github.com/aelurum/AssetStudio) directly into `tools/AssetStudioModCLI/`. No git clone is performed.
6. **vgmstream** — downloads the latest nightly release from [vgmstream/vgmstream-releases](https://github.com/vgmstream/vgmstream-releases) into `tools/vgmstream/`.

> [!NOTE]
> **UnityPy** (pinned in `requirements.txt`) serves as a Python fallback extractor for JDNext bundles when AssetStudioModCLI is unavailable. The installer supports a dual-strategy approach (`assetstudio_first` or `unitypy_first` with automatic fallback).

> [!WARNING]
> **FFmpeg and FFprobe are not auto-installed by `setup.bat`**. They must be available on the system `PATH` before running the installer. Without FFmpeg, audio conversion, AMB generation, and VP9→VP8 re-encoding will fail.

If `setup.bat` fails or you want full control, follow **Manual Setup**.

---

## Manual Setup (Alternative)

### Step 1 — Install Python

Install **Python 3.12+** from <https://www.python.org/downloads/> and enable **Add Python to PATH**.

If you plan to use `setup.bat`, it can also provision a portable Python 3.14.0 runtime automatically when no supported interpreter is available.

Verify:

```bash
python --version
```

---

### Step 2 — Install Python Dependencies

From the project root:

```bash
pip install -r requirements.txt
```

Core packages include:
- **PyQt6** (6.10.2) — GUI framework
- **Playwright** (1.58.0) — Fetch mode browser automation
- **Pydantic** (2.12.5) — Configuration validation
- **Pillow** (12.1.1) — Image processing (cover art, pictograms, textures)
- **requests** (2.33.0) — HTTP downloads
- **UnityPy** (1.25.0) — JDNext bundle extraction fallback
- **scipy** (1.17.1) — Scientific computing (gesture biomechanics)
- **fsspec** (2026.3.0) — Filesystem abstraction
- **pytest / pytest-qt** (dev) — Test framework (development only)

---

### Step 3 — Install Playwright Browser Runtime

Required for Fetch/codename workflows:

```bash
python -m playwright install chromium
```

If you only use IPK, batch folder, or manual source workflows, this may be optional.

---

### Step 4 — Install FFmpeg and FFprobe

Install FFmpeg from <https://ffmpeg.org/download.html> so both `ffmpeg` and `ffprobe` are available on PATH.

Verify:

```bash
ffmpeg -version
ffprobe -version
```

---

### Step 5 — Install vgmstream

`vgmstream` is required for X360/XMA2 decode paths.

Recommended options:

1. Run `setup.bat` once and let the project place vgmstream in the expected runtime location.
2. Or install manually and ensure the CLI binary is available to the installer.

Verify (if available in PATH):

```bash
vgmstream-cli -h
```

PowerShell path check:

```powershell
Get-Command vgmstream-cli
```

---

### Step 6 — Run the Installer

Windows entrypoint:

```bat
RUN.bat
```

Manual Python entrypoint:

```bash
python -m jd2021_installer.main
```

---

## First Run Workflow

In the app:

1. Pick your source mode: **Fetch**, **HTML**, **IPK**, **Batch**, or **Manual**.
2. Provide required files/paths.
3. Start install and monitor progress/log output.
4. Use preview/readjust if timing changes are needed.
5. **Review the install summary** — the installer shows a checklist of required and optional files after each install.

Timing notes:

1. **IPK maps:** manual video offset tuning is commonly required because binary CKD metadata does not encode per-map video lead-in.
2. **AMB intro:** generated automatically for maps with a pre-roll window (`videoStartTime < 0`). No action needed for standard installs.

---

## Theming

The installer ships with **Light** and **Dark** themes. The theme can be switched in the Settings dialog. Themes are loaded from `style_light.qss` and `style_dark.qss` in the project root.

---

## Auto-Updates

The installer includes a built-in update checker. On launch, it can silently check for updates against the configured GitHub branch (default: `v2`). Updates can be applied via `git pull` (for git-cloned installs) or zip download (for standalone distributions). The update system preserves user data files (`installer_settings.json`, `mapDownloads/`, `cache/`, readjust index, etc.).

Branch selection and manual update checking are available in the **Settings** dialog.

---

## Troubleshooting Setup

1. `ffmpeg` / `ffprobe` not found:
   Reinstall FFmpeg and add its `bin` directory to PATH.
2. `vgmstream-cli` not found:
   Re-run `setup.bat` or add the vgmstream binary directory to PATH.
3. Fetch mode fails early:
   Re-run `python -m playwright install chromium`.
4. Media output is missing or degraded:
   Confirm all three tools resolve: ffmpeg, ffprobe, vgmstream-cli.
5. JDNext extract fails with `AssetStudioModCLI.exe not found`:
   Re-run `setup.bat` to auto-download the CLI binary into `tools/AssetStudioModCLI/`, or point `assetstudio_cli_path` in Settings → Advanced to the executable directly.
6. JDNext extract falls back to UnityPy unexpectedly:
   Check that `tools/AssetStudioModCLI/AssetStudioModCLI.exe` exists. UnityPy (already installed via `requirements.txt`) is the automatic fallback — this is expected behavior if the CLI binary is missing.
7. Game directory not found:
   The installer performs heuristic discovery (looks for `jd21/` relative to project root, the `data/World/SkuScenes/SkuScene_Maps_PC_All.isc` file). Set the correct path in Settings if auto-detection fails.

---

## Further Reading

- **[README.md](../../README.md)** — Project overview, features, and limitations
- **[Architecture](../02_core/ARCHITECTURE.md)** — Components and data flow
- **[Pipeline Reference](../02_core/PIPELINE_REFERENCE.md)** — Extract → Normalize → Install phases
- **[GUI Reference](GUI_REFERENCE.md)** — Main window and controls
- **[Audio Timing](../03_media/AUDIO_TIMING.md)** — `videoStartTime`, offset tuning, and AMB intro caveats
- **[Troubleshooting](TROUBLESHOOTING.md)** — Operational troubleshooting
- **[Third-Party Tools](../04_reference/THIRD_PARTY_TOOLS.md)** — External dependency details
