"""服务日志配置。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# 单个日志文件最大 10 MB，保留最近 5 个轮转文件。
_MAX_LOG_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def normalize_log_level(level: str) -> str:
    """规范化日志等级字符串，非法值回退为 INFO。"""
    normalized = level.strip().upper()
    return normalized if normalized in _VALID_LEVELS else "INFO"


def configure_logging(level: str, log_dir: Path | None = None) -> None:
    """配置进程级日志输出，同时写入控制台与文件（若指定 log_dir）。

    Args:
        level: 日志等级字符串。
        log_dir: 日志文件存储目录；为 None 时仅输出到控制台。
    """
    normalized = normalize_log_level(level)
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(normalized)
        for handler in root.handlers:
            handler.setLevel(normalized)
        return

    formatter = logging.Formatter(_DEFAULT_FORMAT)

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(normalized)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # 文件输出（带轮转）
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_dir / "service.log",
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(normalized)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(normalized)
