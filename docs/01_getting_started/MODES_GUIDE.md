# Modes Guide

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This guide explains every installer mode in detail — when to use each one, what inputs are required, step-by-step instructions, and mode-specific troubleshooting.

---

## Table of Contents

- [What "Mode" Means](#what-mode-means)
- [Mode Quick Comparison](#mode-quick-comparison)
- [Before You Use Any Mode](#before-you-use-any-mode)
- [Mode 1: Fetch JDU](#mode-1-fetch-jdu)
- [Mode 2: HTML JDU](#mode-2-html-jdu)
- [Mode 3: Fetch JDNext](#mode-3-fetch-jdnext)
- [Mode 4: HTML JDNext](#mode-4-html-jdnext)
- [Mode 5: IPK Archive](#mode-5-ipk-archive)
- [Mode 6: Batch (Directory)](#mode-6-batch-directory)
- [Mode 7: Manual (Directory)](#mode-7-manual-directory)
- [Choosing the Right Mode](#choosing-the-right-mode)
- [After Install: Sync Refinement](#after-install-sync-refinement)
- [Mode-Specific Troubleshooting Checklist](#mode-specific-troubleshooting-checklist)
- [Related Docs](#related-docs)

---

## What "Mode" Means

A **mode** tells the installer where and how to get source map data. All modes eventually go through the same backend pipeline:

1. **Extract** source files/data
2. **Normalize** into a canonical map model
3. **Install** generated files into JD2021 PC
4. Optionally **readjust sync offsets** after install

The only difference between modes is **how source data is collected and validated**.

---

## Mode Quick Comparison

| Mode | Best For | Input Type | Internet Needed | Typical Difficulty |
|------|----------|------------|-----------------|---------------------|
| Fetch JDU | JDU maps when you know the codename | Codename text | Yes | Low |
| HTML JDU | JDU maps from saved HTML exports | `assets.html` + `nohud.html` | No (if already downloaded) | Low–Medium |
| Fetch JDNext | JDNext maps when you know the codename | Codename text | Yes | Low |
| HTML JDNext | JDNext maps from saved HTML export | `assets.html` | No (if already downloaded) | Low–Medium |
| IPK Archive | Xbox 360 `.ipk` bundles | `.ipk` file | No | Low |
| Batch (Directory) | Installing many maps at once | Folder of candidates | Depends on sources | Medium |
| Manual (Directory) | Advanced / custom map layouts | Folder + individual files | No | High |

---

## Before You Use Any Mode

Complete these steps once before your first install:

### One-Time Setup

1. Run **`setup.bat`** from the project root. This installs Python dependencies and configures FFmpeg.
2. Launch with **`RUN.bat`**.
3. In the **Configuration Panel** (left column), set your **Game Directory** to the JD2021 folder that contains both `data` and `engine`.
4. Confirm external tools are available:
   - `ffmpeg`
   - `ffprobe`
   - `vgmstream-cli` (important for XMA2 audio decode)

### For Fetch modes only

5. Confirm Playwright Chromium is installed:
   ```bash
   python -m playwright install chromium
   ```
6. Set your **Discord Channel URL** in **Settings → Integrations tab**.

### For JDNext modes only

7. Confirm the following exist (run `setup.bat` if not yet done):
   - `tools/AssetStudioModCLI/AssetStudioModCLI.exe` (auto-downloaded by `setup.bat`)
   - **UnityPy** installed in the Python environment (pinned in `requirements.txt`; serves as automatic fallback)

### Before Every Install

1. Run **Pre-flight Check** (in the Action Panel).
2. Verify mode input fields are filled and point to the expected files.
3. Read any warnings in the **Log Console** before pressing Install.

---

## Mode 1: Fetch JDU

### When to use it

Use Fetch mode when you know the JDU song codename(s) and want the installer to automatically retrieve everything via a Discord bot.

### What you need

1. One or more codenames (comma-separated).
2. An active internet connection.
3. Playwright Chromium installed (`python -m playwright install chromium`).
4. A valid **Discord Channel URL** and **Bot Provider** (`sev4nty` or `rama`) configured in **Settings → Integrations**.

### Where to look in the UI

- **Mode Selector** → choose **Fetch JDU**
- The input area shows a **Codename(s)** text field

### Finding codenames

- Codename reference: https://justdance.fandom.com
- JDU map list: https://justdance.fandom.com/wiki/Just_Dance_Unlimited

### Step-by-step

1. Select **Fetch JDU** from the Mode dropdown.
2. Read the info banner — it reminds you to configure your Discord channel link.
3. Enter one or more codenames in the text field.
   - Single: `TemperatureAlt`
   - Multiple: `TemperatureAlt, Koi, RainOnMe`
4. Set your desired **Video Quality** in the Configuration Panel.
5. Click **Pre-flight Check**.
6. Click **Install Map**.
7. A Chromium browser window will open for Discord login (first time only).
8. Wait for download + pipeline completion.
9. Test in game.
10. If sync is off, click **Re-adjust Offset** in the Action Panel.

### Common issues

| Problem | Solution |
|---------|----------|
| "Chromium not installed" error | Run `python -m playwright install chromium` |
| Timeout or no response | Retry with one codename at a time |
| Codename fails repeatedly | Check codename spelling/casing, then try HTML mode as fallback |
| Map installs but timing is off | Use Re-adjust Offset |

---

## Mode 2: HTML JDU

### When to use it

Use HTML mode when you already have saved JDU bot HTML exports and want a reproducible offline install.

### What you need

1. **Asset HTML** file (commonly `assets.html`).
2. **NOHUD HTML** file (commonly `nohud.html`).
3. Both files must be from the **same map and export session**.

### Where to look in the UI

- **Mode Selector** → choose **HTML JDU**
- The input area shows **Asset HTML** and **NOHUD HTML** rows with Browse buttons

### Step-by-step

1. Select **HTML JDU** from the Mode dropdown.
2. Read the warning banner about link expiration (~30 minutes).
3. Click **Browse** next to "Asset HTML" and pick the asset file.
4. The installer will try to **auto-detect** the matching NOHUD file in the same folder.
5. If it didn't auto-detect, manually click **Browse** next to "NOHUD HTML" and pick it.
6. Click **Pre-flight Check**.
7. Click **Install Map**.
8. Review logs for any pairing or parsing warnings.
9. Use Sync Refinement if timing is off after install.

### Best practices

1. Store each map's HTML pair in its own folder.
2. Keep original filenames when possible.
3. Do not mix files from different export sessions.

### Common issues

| Problem | Solution |
|---------|----------|
| Expired or malformed HTML export | Re-export from source bot and retry |
| Pair mismatch (assets/nohud don't match) | Re-select the correct matching files |
| Missing media references | Re-fetch files or switch to Fetch mode |

---

## Mode 3: Fetch JDNext

### When to use it

Use Fetch JDNext when you know the JDNext song codename and want the installer to retrieve assets automatically via the Discord bot.

### What you need

1. One or more JDNext codenames.
2. An active internet connection.
3. Playwright Chromium installed.
4. A valid **Discord Channel URL** and **Bot Provider** (`sev4nty` or `rama`) configured in **Settings → Integrations**.
5. JDNext third-party tools installed (auto-provisioned by `setup.bat`):
   - `tools/AssetStudioModCLI/AssetStudioModCLI.exe`
   - **UnityPy** Python package (fallback; installed via `requirements.txt`)

### Where to look in the UI

- **Mode Selector** → choose **Fetch JDNext**
- The input area shows a **Codename(s)** text field

### Step-by-step

1. Select **Fetch JDNext** from the Mode dropdown.
2. Read the info banner — it reminds you to configure your Discord channel link.
3. Enter the JDNext codename (e.g., `TelephoneALT`).
4. Set your desired **Video Quality** in the Configuration Panel.
5. Click **Pre-flight Check** — this verifies both internet and JDNext tools.
6. Click **Install Map**.
7. A Chromium browser window will open for Discord login if needed.
8. Wait for download, Unity asset extraction, and pipeline completion.
9. Test in game.
10. Use **Re-adjust Offset** if sync is off.

### Common issues

| Problem | Solution |
|---------|----------|
| "Chromium not installed" error | Run `python -m playwright install chromium` |
| AssetStudio CLI not found | Re-run `setup.bat`, or set `assetstudio_cli_path` in Settings → Advanced to the `.exe` location |
| Unity extraction falls back to UnityPy | Expected behavior when CLI binary is missing; UnityPy (from `requirements.txt`) handles extraction automatically |
| Codename not recognized | Verify the JDNext codename — JDNext codenames may differ from JDU |

---

## Mode 4: HTML JDNext

### When to use it

Use HTML JDNext mode when you already have a saved JDNext bot HTML export and want to install offline.

### What you need

1. **Asset HTML** file from a JDNext bot export.
2. JDNext third-party tools installed (same as Fetch JDNext mode).

### Where to look in the UI

- **Mode Selector** → choose **HTML JDNext**
- The input area shows an **Asset HTML** row with a Browse button

> **Note:** Unlike JDU HTML mode, JDNext HTML mode requires only **one** HTML file (no separate NOHUD file).

### Step-by-step

1. Select **HTML JDNext** from the Mode dropdown.
2. Read the warning banner about link expiration (~30 minutes).
3. Click **Browse** next to "Asset HTML" and pick the JDNext asset HTML file.
4. Click **Pre-flight Check**.
5. Click **Install Map**.
6. Watch the Log Console for Unity extraction and pipeline progress.
7. Test in game.
8. Use Sync Refinement if timing is off.

### Common issues

| Problem | Solution |
|---------|----------|
| Expired HTML links | Re-export from the bot or switch to Fetch JDNext mode |
| AssetStudio CLI not found | Re-run `setup.bat` or set `assetstudio_cli_path` in Settings → Advanced |
| Install succeeds but assets look wrong | Check that the HTML file is from JDNext (not JDU) |

---

## Mode 5: IPK Archive

### When to use it

Use IPK mode when you have a local Xbox 360 `.ipk` map archive file.

### What you need

1. A valid `.ipk` file.
2. Enough free disk space for extraction and conversion.

### Where to look in the UI

- **Mode Selector** → choose **IPK Archive**
- The input area shows an **IPK File** row with a **Browse** button

### Step-by-step

1. Open the **Mode** dropdown at the top of the left column and select **IPK Archive**.
2. Click **Browse** next to "IPK File" and select your `.ipk` file.
3. Click **Pre-flight Check** in the Action Panel.
4. Click **Install Map**.
5. Watch the **Progress Panel** (left column) and **Log Console** (right column) for progress.
6. When complete, test the map in game.
7. If the audio or video timing is off, use **Re-adjust Offset** to fine-tune.

### Special behavior

- **Single-map IPKs** install directly.
- **Bundle IPKs** (containing multiple maps) open a selection dialog so you can choose which map(s) to install.

### Important timing note

IPK-derived timing can be approximate. Video lead-in often needs manual refinement after install — this is expected.

### Common issues

| Problem | Solution |
|---------|----------|
| Invalid or corrupt IPK | Re-obtain the file and verify its size/hash |
| Missing decode tools | Confirm `ffmpeg`, `ffprobe`, and `vgmstream-cli` are installed |
| Video starts too early or late | Use Sync Refinement to apply offset adjustments |

---

## Mode 6: Batch (Directory)

### When to use it

Use Batch mode when you want to process many maps from a single folder in one run.

### What you need

1. A root directory containing map candidates — these can be:
   - `.ipk` files
   - Folders with `assets.html` + `nohud.html` pairs
   - Already-extracted map folders
2. Consistent naming and layout to reduce skipped entries.

### Where to look in the UI

- **Mode Selector** → choose **Batch (Directory)**
- The input area shows a **Maps Folder** row with a **Browse** button

### Step-by-step

1. Select **Batch (Directory)** from the Mode dropdown.
2. Click **Browse** and choose the root folder that contains your map candidates.
3. Read the info banner to confirm what file types are accepted.
4. Click **Pre-flight Check**.
5. Click **Install Map**.
6. Monitor the **Log Console** for per-map success/failure status.
7. For any failed maps, re-run them individually in the most suitable mode.

### Best practices

1. Dry-run with a small subset first (2–3 maps).
2. Keep one map per subfolder where possible.
3. Review log output for skipped candidates before re-running.

### Common issues

| Problem | Solution |
|---------|----------|
| Mixed or ambiguous folder structure | Reorganize into one-map-per-subfolder layout |
| Wrong file type assumptions | Separate IPKs from HTML exports and manual roots |
| Partial batch success | Retry failures individually via the appropriate mode |

---

## Mode 7: Manual (Directory)

### When to use it

Use Manual mode for advanced cases where you provide pre-extracted source files directly, organized in your own directory layout.

### What you need

1. A prepared source folder with map files.
2. Understanding of the expected map asset structure.
3. Willingness to manually troubleshoot missing or inconsistent inputs.

### Where to look in the UI

- **Mode Selector** → choose **Manual (Directory)**
- The input area expands to show a **scrollable form** with many fields

### Available controls

**Top:**
- **Source Type** dropdown: `JDU` / `IPK` / `Mixed` — determines which field groups are visible
- **Root Folder** + **Browse** + **Scan** button — set the root and auto-populate fields
- **Codename** — auto-detected or manually entered

**Required Files group:**
- Audio File (`.ogg`, `.wav`, `.wav.ckd`)
- Video File (`.webm`)
- Musictrack (`.ckd`, `.trk`)

**Tapes & Config group** (shown for IPK/Mixed source type):
- Songdesc / Dance Tape / Karaoke Tape / Mainseq Tape

**Asset Folders group:**
- Moves Folder / Pictos Folder / MenuArt Folder / AMB Folder

**JDU MenuArt group** (shown for JDU/Mixed source type):
- Cover Generic / Cover Online / Banner / Banner Bkg / Map Bkg
- Cover AlbumCoach / Cover AlbumBkg
- Coach 1–4 Art

### Step-by-step

1. Select **Manual (Directory)** from the Mode dropdown.
2. Choose your **Source Type** (`JDU`, `IPK`, or `Mixed`).
3. Click **Browse** next to "Root Folder" and select your prepared folder.
4. The installer will **auto-scan** the folder and pre-fill fields it can detect.
5. Fill in any remaining blank fields manually — check the **Codename** is correct.
6. Click **Pre-flight Check**.
7. Click **Install Map**.
8. Inspect the **Log Console** closely for missing-component warnings.
9. Fix issues in your source folder and retry if needed.

### Best practices

1. Start from a known-good map structure and modify gradually.
2. Keep backup copies of your manual source sets.
3. Validate one map fully before scaling to many.

### Common issues

| Problem | Solution |
|---------|----------|
| Missing required assets | Check folder structure and expected media/config files |
| Unsupported naming or casing | Normalize filenames and folder casing |
| Install succeeds but playback is wrong | Verify source media fidelity and toolchain availability |

---

## Choosing the Right Mode

Use this decision flow:

```
Do you have a .ipk archive file?
  → Yes: use IPK Archive

Do you want to process many maps from one folder?
  → Yes: use Batch (Directory)

Do you have a folder of pre-extracted map files?
  → Yes: use Manual (Directory)

Are you installing a JDU map?
  ├─ Have the codename? → use Fetch JDU
  └─ Have saved HTML exports? → use HTML JDU

Are you installing a JDNext map?
  ├─ Have the codename? → use Fetch JDNext
  └─ Have a saved HTML export? → use HTML JDNext
```

---

## After Install: Sync Refinement

No matter which mode you used, if the audio/video timing looks off in-game:

1. Click **Re-adjust Offset** in the Action Panel.
2. Select the installed map from the dialog that appears.
3. In the **Sync Refinement Widget** (right column), adjust the **Audio Offset** using the ± buttons.
4. Click **Preview** to hear/see the result.
5. Keep adjusting and previewing until the timing feels right.
6. Click **Apply Offset** to save the changes.
7. If you're in a multi-map flow, use **Prev Map** / **Next Map** to move between maps.

---

## Mode-Specific Troubleshooting Checklist

If an install fails, check in this order:

1. ✅ Correct mode selected for your source type.
2. ✅ Input paths point to the exact expected files or folder.
3. ✅ Game directory is set to the JD2021 root (contains `data` + `engine`).
4. ✅ Pre-flight check passes.
5. ✅ Required tools are available:
   - All modes: `ffmpeg`, `ffprobe`
   - IPK/XMA2: `vgmstream-cli`
   - Fetch modes: Playwright Chromium + Discord URL
   - JDNext modes: `tools/AssetStudioModCLI/AssetStudioModCLI.exe` (or UnityPy fallback)
6. ✅ Source files are complete and from the same map/version.
7. ✅ Retry one map at a time to isolate the root cause.

---

## Related Docs

- [Usage Guide](USAGE_GUIDE.md)
- [GUI Reference](GUI_REFERENCE.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Audio Timing](../03_media/AUDIO_TIMING.md)
- [Pipeline Reference](../02_core/PIPELINE_REFERENCE.md)