# Documentation Index

> Last Updated: June 2026

This is the central navigation page for JD2021 Map Installer documentation.

## Folder Layout

- [01_getting_started/](01_getting_started/) - Setup, GUI flow, troubleshooting
- [02_core/](02_core/) - Architecture, pipeline, core data docs
- [03_media/](03_media/) - Audio, video, asset-specific behavior
- [04_reference/](04_reference/) - Config and tool references
- [05_guides/](05_guides/) - Manual porting guides
- [06_research/](06_research/) - Gap and opportunity research

## Start Here

- [Usage Guide](01_getting_started/USAGE_GUIDE.md) - Beginner walkthrough with UI, settings, and mode-by-mode usage
- [Getting Started](01_getting_started/GETTING_STARTED.md) - Setup, dependencies, first run
- [Modes Guide](01_getting_started/MODES_GUIDE.md) - Complete instructions for all installer modes
- [GUI Reference](01_getting_started/GUI_REFERENCE.md) - Main window controls and workflow
- [Troubleshooting](01_getting_started/TROUBLESHOOTING.md) - Common problems and fixes

## Core Technical Docs

- [Architecture](02_core/ARCHITECTURE.md) - High-level system structure
- [Pipeline Reference](02_core/PIPELINE_REFERENCE.md) - Extract -> Normalize -> Install flow
- [Data Formats](02_core/DATA_FORMATS.md) - CKD/IPK and generated file formats
- [Data Mapping](02_core/DATA_MAPPING.md) - JDU source fields to JD2021 outputs
- [Map Config Format](04_reference/MAP_CONFIG_FORMAT.md) - Per-map adjustment schema
- [Game Config Reference](04_reference/GAME_CONFIG_REFERENCE.md) - Game-side configuration files

## Media and Timing

- [Audio Timing](03_media/AUDIO_TIMING.md) - `videoStartTime`, marker behavior, caveats
- [Video](03_media/VIDEO.md) - Quality tiers and processing behavior
- [Assets](03_media/ASSETS.md) - HTML asset exports and related inputs

## Manual Porting and Gap Tracking

- [Manual JDU Porting Guide](05_guides/MANUAL_JDU_PORTING_GUIDE.md)
- [Manual JDNext Porting Guide](05_guides/MANUAL_JDNEXT_PORTING_GUIDE.md)
- [Manual IPK Porting Guide](05_guides/MANUAL_IPK_PORTING_GUIDE.md)
- [Known Gaps](06_research/KNOWN_GAPS.md)
- [Camera Gesture Remediation Log](06_research/CAMERA_GESTURE_REMEDIATION_PLAN.md) — JDNext→Durango gesture compilation (experimental; branch abandoned)
- [JDU Unused Data Opportunities](06_research/JDU_UNUSED_DATA_OPPORTUNITIES.md)
- [Third-Party Tools](04_reference/THIRD_PARTY_TOOLS.md)

## Supporting Material

- [reports/](reports/) - Investigations, audits, and change reports
- [sessions/](sessions/) - Session notes and deep dives
- [todo/](todo/) - Planning notes and prompts