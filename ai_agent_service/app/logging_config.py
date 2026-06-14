"""服务日志配置。"""

from __future__ import annotations

import logging

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def normalize_log_level(level: str) -> str:
    """规范化日志等级字符串，非法值回退为 INFO。"""
    normalized = level.strip().upper()
    return normalized if normalized in _VALID_LEVELS else "INFO"


def configure_logging(level: str) -> None:
    """配置进程级日志输出，重复调用时只更新 root logger 等级。"""
    normalized = normalize_log_level(level)
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(normalized)
        for handler in root.handlers:
            handler.setLevel(normalized)
        return
    logging.basicConfig(level=normalized, format=_DEFAULT_FORMAT)
