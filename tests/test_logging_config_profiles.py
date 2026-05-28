from __future__ import annotations

import logging

import pytest

from jd2021_installer.core.logging_config import apply_log_detail, normalize_log_detail


class QtLogHandler(logging.Handler):
    """Test double that mirrors the production Qt handler class name."""


@pytest.mark.parametrize(
    ("detail", "logger_level", "console_level", "ui_level", "file_level", "console_fmt", "ui_fmt", "file_fmt"),
    [
        (
            "quiet",
            logging.DEBUG,
            logging.WARNING,
            logging.WARNING,
            logging.DEBUG,
            "[%(levelname)s] %(message)s",
            "[%(levelname)s] %(message)s",
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        ),
        (
            "user",
            logging.DEBUG,
            logging.INFO,
            logging.INFO,
            logging.DEBUG,
            "%(message)s",
            "[%(levelname)s] %(message)s",
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        ),
        (
            "detailed",
            logging.DEBUG,
            logging.INFO,
            logging.INFO,
            logging.DEBUG,
            "[%(levelname)s] %(message)s",
            "[%(levelname)s] %(message)s",
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        ),
        (
            "developer",
            logging.DEBUG,
            logging.DEBUG,
            logging.DEBUG,
            logging.DEBUG,
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        ),
    ],
)
def test_apply_log_detail_applies_levels_and_formats(
    tmp_path,
    detail: str,
    logger_level: int,
    console_level: int,
    ui_level: int,
    file_level: int,
    console_fmt: str,
    ui_fmt: str,
    file_fmt: str,
) -> None:
    root_logger = logging.getLogger()
    jd_logger = logging.getLogger("jd2021")

    old_root_handlers = list(root_logger.handlers)
    old_jd_handlers = list(jd_logger.handlers)
    old_root_level = root_logger.level
    old_jd_level = jd_logger.level

    for handler in old_root_handlers:
        root_logger.removeHandler(handler)
    for handler in old_jd_handlers:
        jd_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    ui_handler = QtLogHandler()
    file_handler = logging.FileHandler(tmp_path / "profile.log", encoding="utf-8")

    root_logger.addHandler(console_handler)
    root_logger.addHandler(ui_handler)
    jd_logger.addHandler(file_handler)

    try:
        assert apply_log_detail(detail) == detail

        assert jd_logger.level == logger_level

        assert console_handler.level == console_level
        assert ui_handler.level == ui_level
        assert file_handler.level == file_level

        assert console_handler.formatter is not None
        assert ui_handler.formatter is not None
        assert file_handler.formatter is not None

        assert console_handler.formatter._fmt == console_fmt
        assert ui_handler.formatter._fmt == ui_fmt
        assert file_handler.formatter._fmt == file_fmt
    finally:
        root_logger.removeHandler(console_handler)
        root_logger.removeHandler(ui_handler)
        jd_logger.removeHandler(file_handler)

        file_handler.close()
        console_handler.close()
        ui_handler.close()

        for handler in old_root_handlers:
            root_logger.addHandler(handler)
        for handler in old_jd_handlers:
            jd_logger.addHandler(handler)

        root_logger.setLevel(old_root_level)
        jd_logger.setLevel(old_jd_level)


def test_normalize_log_detail_defaults_to_user() -> None:
    assert normalize_log_detail(None) == "user"
    assert normalize_log_detail("unknown") == "user"
