"""服务日志配置。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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


class _SafeRotatingFileHandler(RotatingFileHandler):
    """Windows 兼容的轮转文件处理器。"""

    def doRollover(self) -> None:
        """尝试标准轮转；失败时将旧文件改为时间戳备份后新建文件。"""
        try:
            super().doRollover()
            return
        except (PermissionError, OSError):
            pass

        # 标准轮转失败，回退策略：
        # 1. 关闭当前写入流（确保缓冲区刷出）
        if self.stream:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None

        # 2. 尝试把当前文件重命名为带时间戳的备份
        log_path = Path(self.baseFilename)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = log_path.with_name(f"{log_path.stem}_{timestamp}{log_path.suffix}")
        try:
            if log_path.exists():
                log_path.rename(backup_path)
        except (PermissionError, OSError):
            # 重命名也失败（文件仍被锁住），保留原文件不动
            pass

        # 3. 打开新的 service.log 继续写入
        self.stream = self._open()


def configure_logging(level: str, log_dir: Path | None = None, console: bool = True) -> None:
    """配置进程级日志输出，同时写入控制台与文件（若指定 log_dir）。

    Args:
        level: 日志等级字符串。
        log_dir: 日志文件存储目录；为 None 时仅输出到控制台。
        console: 是否附加控制台 handler。Godot 插件通过 `OS.execute_with_pipe`
            启动本进程时，stdout/stderr 管道没有消费方在读取；管道写满后
            控制台 handler 的下一次写入会永久阻塞、冻住整个事件循环（所有
            HTTP 请求都会因此卡死)。这种受管进程必须传 `console=False`，
            只依赖文件日志。
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
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(normalized)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # 文件输出（带 Windows 安全轮转）
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = _SafeRotatingFileHandler(
            filename=log_dir / "service.log",
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(normalized)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(normalized)
