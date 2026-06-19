"""可选的统一多模态资产描述客户端。"""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetLLMConfig:
    enabled: bool = False
    model: str = ""
    endpoint: str = ""
    api_key: str = ""
    timeout_s: float = 10.0
    max_tokens: int = 500


class AssetLLMClient:
    def __init__(self, config: AssetLLMConfig | None = None) -> None:
        self.config = config or AssetLLMConfig()

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.config.model and self.config.endpoint)

    def describe(self, path: Path, type_hint: str) -> str:
        if not self.available or type_hint not in {"image", "audio"}:
            logger.debug(
                "Asset semantic description skipped asset=%s type=%s available=%s",
                path.name,
                type_hint,
                self.available,
            )
            return ""
        started = time.perf_counter()
        try:
            from openai import OpenAI

            data = base64.b64encode(path.read_bytes()).decode()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            client = OpenAI(api_key=self.config.api_key, base_url=self.config.endpoint, timeout=self.config.timeout_s)
            if type_hint == "image":
                content = [{"type": "text", "text": "请简洁描述这个游戏资源的视觉内容，不要输出分类标签。"},
                           {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]
            else:
                content = f"请根据文件名和音频资源元数据描述用途，不要输出分类标签：{path.name}, {path.stat().st_size} bytes"
            response = client.chat.completions.create(model=self.config.model, messages=[{"role": "user", "content": content}], max_tokens=self.config.max_tokens)
            description = (response.choices[0].message.content or "").strip()
            logger.debug(
                "Asset semantic description complete asset=%s type=%s model=%s chars=%d "
                "elapsed_ms=%.3f",
                path.name,
                type_hint,
                self.config.model,
                len(description),
                (time.perf_counter() - started) * 1000,
            )
            return description
        except Exception as exc:
            logger.warning("Asset semantic description unavailable for %s: %s", path, exc)
            return ""
