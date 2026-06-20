"""本地持久化的通用存储工具（原子写入等）。"""

from app.storage.atomic import atomic_write_json, atomic_write_text

__all__ = ["atomic_write_json", "atomic_write_text"]
