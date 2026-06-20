"""原子 JSON / 文本写入（§14.2 持久化崩溃安全）。

正式 JSON 文件若用 `Path.write_text()` 直接覆盖，会先以截断模式打开目标
文件再写入；一旦磁盘写满、进程被杀或系统断电，旧文件已被清空而新内容
只写了一半，便会留下半截 JSON，导致下次启动无法恢复会话/记忆，甚至被
后续逻辑当成空集合再次覆盖掉残余数据。

这里采用"写临时文件 + fsync + 原子重命名"的事务式写法：先把完整内容写入
同目录下的临时文件并 `fsync` 落盘，再用 `os.replace()` 原子替换目标。
`os.replace()` 在同一文件系统内保证替换是原子的——任意时刻读到的目标文件
要么是旧的完整版本，要么是新的完整版本，绝不会出现半截内容。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """以原子方式把 `text` 写入 `path`。

    Args:
        path: 目标文件路径，父目录会按需创建。
        text: 待写入的完整文本内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    """以原子方式把 `value` 序列化为 JSON 后写入 `path`。

    Args:
        path: 目标文件路径，父目录会按需创建。
        value: 任意可被 `json.dumps` 序列化的值。
    """
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    atomic_write_text(path, payload)
