"""Application entry point.

Creates the PyQt6 application, initializes logging, and shows the main window.
"""

from __future__ import annotations

import logging
import json
import sys
from pathlib import Path

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
