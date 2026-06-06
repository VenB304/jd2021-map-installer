"""Application entry point.

Creates the PyQt6 application, initializes logging, and shows the main window.
"""

from __future__ import annotations

import logging
import json
import os
import sys
from pathlib import Path

def _assign_to_windows_job():
    if os.name != "nt":
        return
    import ctypes
    job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]
    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
    res = ctypes.windll.kernel32.SetInformationJobObject(
        job, 9, ctypes.pointer(info), ctypes.sizeof(info)
    )
    if res:
        hProcess = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.AssignProcessToJobObject(job, hProcess)

if sys.version_info < (3, 12):
    raise SystemExit("JD2021 Map Installer requires Python 3.12 or newer.")

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.logging_config import apply_log_detail
from jd2021_installer.core.theme import load_theme_stylesheet
from jd2021_installer.utils.icon_gen import ensure_default_icons
from jd2021_installer.ui.main_window import MainWindow


def setup_logging(log_detail_level: str = "user") -> None:
    """Configure root logger for the application."""
    root = logging.getLogger("jd2021")
    if root.handlers:
        apply_log_detail(log_detail_level)
        return
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    apply_log_detail(log_detail_level)


def load_startup_config(project_root: Path) -> AppConfig:
    """Load persisted config for startup concerns (like theme)."""
    settings_file = project_root / "installer_settings.json"
    if not settings_file.exists():
        return AppConfig()

    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        return AppConfig(**data)
    except Exception:
        return AppConfig()


def main() -> int:
    """Application entry point."""
    _assign_to_windows_job()
    setup_logging()
    project_root = Path(__file__).resolve().parent.parent
    ensure_default_icons(project_root)
    startup_config = load_startup_config(project_root)
    app_icon_value = getattr(startup_config, "app_icon_path", Path("./assets/app_icon.png"))
    app_icon_path = Path(app_icon_value)
    if not app_icon_path.is_absolute():
        app_icon_path = project_root / app_icon_path
    if not app_icon_path.exists():
        fallback_icon = project_root / "assets" / "app_icon.svg"
        if fallback_icon.exists():
            app_icon_path = fallback_icon

    app = QApplication([sys.argv[0]])
    app.setApplicationName("JD2021 Map Installer")
    app.setApplicationVersion("2.0.0")
    if app_icon_path.exists():
        app.setWindowIcon(QIcon(str(app_icon_path)))
    app.setStyleSheet(
        load_theme_stylesheet(
            startup_config.theme,
            project_root,
            getattr(startup_config, "style_debug_mode", False),
        )
    )

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
