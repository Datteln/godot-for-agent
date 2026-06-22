"""可选的统一多模态资产描述客户端。"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_JPEG_QUALITIES = (85, 70, 55, 40)
_MIN_IMAGE_EDGE = 256
_SUPPORTED_IMAGE_MIME_BY_SUFFIX = {
    ".bmp": "image/bmp",
    ".jpe": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".heic": "image/heic",
}


def _open_image(path: Path, data: bytes) -> Image.Image:
    """将栅格或 SVG 图片解码为可转换的 Pillow 图像。"""
    if path.suffix.lower() == ".svg":
        from resvg_py import svg_to_bytes

        data = svg_to_bytes(svg_path=str(path), resources_dir=str(path.parent))
    if path.suffix.lower() == ".heic":
        from pillow_heif import register_heif_opener

        register_heif_opener()
    with Image.open(BytesIO(data)) as source:
        source.seek(0)
        return ImageOps.exif_transpose(source).copy()


def _encode_png_with_limit(path: Path, image: Image.Image, original_size: int) -> bytes:
    """将图片编码为 PNG，并在超限时逐级缩小尺寸。"""
    while True:
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        converted = output.getvalue()
        if len(converted) <= _MAX_IMAGE_BYTES:
            logger.info(
                "Asset image converted to PNG asset=%s original_bytes=%d converted_bytes=%d "
                "width=%d height=%d",
                path.name,
                original_size,
                len(converted),
                image.width,
                image.height,
            )
            return converted
        if max(image.size) <= _MIN_IMAGE_EDGE:
            raise ValueError(f"图片转换为 PNG 后仍超过 10 MB: {path}")
        image.thumbnail(
            (max(_MIN_IMAGE_EDGE, image.width // 2), max(_MIN_IMAGE_EDGE, image.height // 2)),
            Image.Resampling.LANCZOS,
        )


def _encode_jpeg_with_limit(path: Path, image: Image.Image, original_size: int) -> bytes:
    """将超限图片编码为不超过 10 MB 的 JPEG。"""
    if image.mode not in {"RGB", "L"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode == "L":
        image = image.convert("RGB")

    while True:
        for quality in _JPEG_QUALITIES:
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            compressed = output.getvalue()
            if len(compressed) <= _MAX_IMAGE_BYTES:
                logger.info(
                    "Asset image compressed asset=%s original_bytes=%d compressed_bytes=%d "
                    "width=%d height=%d quality=%d",
                    path.name,
                    original_size,
                    len(compressed),
                    image.width,
                    image.height,
                    quality,
                )
                return compressed

        if max(image.size) <= _MIN_IMAGE_EDGE:
            raise ValueError(f"图片压缩后仍超过 10 MB: {path}")
        image.thumbnail(
            (max(_MIN_IMAGE_EDGE, image.width // 2), max(_MIN_IMAGE_EDGE, image.height // 2)),
            Image.Resampling.LANCZOS,
        )


def _prepare_image(path: Path) -> tuple[bytes, str]:
    """读取图片，规范化非白名单格式，并压缩超过 10 MB 的图片。"""
    original = path.read_bytes()
    supported_mime = _SUPPORTED_IMAGE_MIME_BY_SUFFIX.get(path.suffix.lower())
    if supported_mime is not None and len(original) <= _MAX_IMAGE_BYTES:
        return original, supported_mime

    image = _open_image(path, original)
    if supported_mime is None:
        return _encode_png_with_limit(path, image, len(original)), "image/png"
    return _encode_jpeg_with_limit(path, image, len(original)), "image/jpeg"


@dataclass(frozen=True)
class AssetLLMConfig:
    enabled: bool = False
    model: str = ""
    endpoint: str = ""
    api_key: str = ""
    timeout_s: float = 10.0
    max_tokens: int = 500
    concurrency: int = 3


class AssetLLMClient:
    def __init__(self, config: AssetLLMConfig | None = None) -> None:
        self.config = config or AssetLLMConfig()

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.config.model and self.config.endpoint)

    def describe(self, path: Path, type_hint: str) -> str:
        """调用多模态模型生成图片或音频资源的简短语义描述。"""
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

            client = OpenAI(api_key=self.config.api_key, base_url=self.config.endpoint, timeout=self.config.timeout_s)
            if type_hint == "image":
                image_bytes, mime = _prepare_image(path)
                data = base64.b64encode(image_bytes).decode()
                content = [{"type": "text", "text": "请简洁描述这个游戏资源的视觉内容，不要输出分类标签。"},
                           {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]
            else:
                content = f"请根据文件名和音频资源元数据描述用途，不要输出分类标签：{path.name}, {path.stat().st_size} bytes"
            response = client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=self.config.max_tokens,
                extra_body={"enable_thinking": False},
            )
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

    def _describe_item(self, item: tuple[Path, str]) -> str:
        """将线程池中的单项参数转发给资产描述方法。"""
        return self.describe(*item)

    def describe_many(self, assets: Sequence[tuple[Path, str]]) -> list[str]:
        """使用受配置限制的线程池并发生成多项资产描述。"""
        if not assets:
            return []
        workers = min(max(1, self.config.concurrency), len(assets))
        logger.info(
            "Asset semantic batch start assets=%d concurrency=%d",
            len(assets),
            workers,
        )
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asset-llm") as executor:
            descriptions = list(executor.map(self._describe_item, assets))
        logger.info(
            "Asset semantic batch complete assets=%d concurrency=%d elapsed_ms=%.3f",
            len(assets),
            workers,
            (time.perf_counter() - started) * 1000,
        )
        return descriptions
