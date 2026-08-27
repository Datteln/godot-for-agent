"""`read_file`：安全读取工程内文本文件（按行分页，类似 Claude Code 的 Read 工具）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

# 单次系统调用最多扫描的字节数：无论文件实际多大，一次 read() 只读这么多，
# 避免把几百 MB/GB 的文件整体读进内存导致瞬时 OOM。超过此上限的部分对本次
# 调用不可见（scan_truncated=True），需要换用 grep_code 之类的工具定位。
MAX_SCAN_BYTES = 4 * 1024 * 1024
# 单次调用最多返回的字节数（即便请求的行数范围内字节数更大也会截断）。
MAX_RETURN_BYTES = 128 * 1024
DEFAULT_LIMIT_LINES = 200
MAX_LIMIT_LINES = 20000
MAX_SELECTOR_CONTEXT_LINES = 400

# 任务 8.4：超长物理行（生成/压缩/单行大文件）不作为普通文本整行返回，
# 返回定位符提示；结构化场景/资源文件里的序列化数组字段同理。
OPAQUE_LINE_CHARS = 4000
_STRUCTURED_SUFFIXES = (".tscn", ".tres", ".import", ".godot")
_SERIALIZED_FIELD_RE = re.compile(r"Packed(?:Byte|Int|Float|String|Vector|Color)Array\(|tile_data")

logger = logging.getLogger(__name__)

READ_FILE_SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "读取当前 Godot 工程根目录内的一个文本文件（只读，自动遵守安全边界）。"
        "生成/压缩/不透明的超长物理行不会整行返回，而是给出定位符与后续检索方式。"
        "默认从第 1 行开始最多返回 200 行；返回结果里 has_more=true 表示文件还有更多内容，"
        "此时应带上更大的 offset 再调用一次以继续读取，而不是假定文件已读完。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工程根目录的文件路径。"},
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始），默认 1。",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的行数，默认 200，不能超过服务端上限。",
            },
            "selector": {
                "type": "object",
                "description": "按已知定位符选择小片段；指定时优先于 offset/limit 的整页读取。",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["match", "symbol", "json_path"],
                        "description": "match=文本/配置键，symbol=代码符号，json_path=JSON 路径。",
                    },
                    "value": {
                        "type": "string",
                        "description": "匹配文本、符号名或 JSON 路径（如 render.layers[0].name）。",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "match/symbol 前后的行数，默认 24，最多 400。",
                    },
                },
                "required": ["kind", "value"],
            },
        },
        "required": ["path"],
    },
}


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(minimum, min(value, maximum))


def _selector_config(args: dict[str, Any]) -> tuple[str, str, int] | None:
    """验证并归一化可选的精确读取 selector。"""
    selector = args.get("selector")
    if selector is None:
        return None
    if not isinstance(selector, dict):
        raise ValueError("selector 必须是对象")
    kind = selector.get("kind")
    value = selector.get("value")
    if kind not in {"match", "symbol", "json_path"}:
        raise ValueError("selector.kind 必须是 match、symbol 或 json_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("selector.value 不能为空")
    context_lines = _clamp_int(
        selector.get("context_lines", 24),
        default=24,
        minimum=0,
        maximum=MAX_SELECTOR_CONTEXT_LINES,
    )
    return kind, value.strip(), context_lines


def _json_path_value(value: Any, path: str) -> Any:
    """读取受限 JSON 路径，支持点分键与方括号数组下标。"""
    parts = re.findall(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]", path)
    if not parts:
        raise ValueError("selector.value 不是有效 JSON 路径")
    current = value
    for key, index in parts:
        if key:
            if not isinstance(current, dict) or key not in current:
                raise ValueError(f"JSON 路径不存在：{path}")
            current = current[key]
        else:
            if not isinstance(current, list):
                raise ValueError(f"JSON 路径不是数组：{path}")
            parsed_index = int(index)
            if parsed_index >= len(current):
                raise ValueError(f"JSON 数组下标越界：{path}")
            current = current[parsed_index]
    return current


def _select_file_lines(
    lines: list[str], selector: tuple[str, str, int] | None
) -> tuple[list[str], int, str | None]:
    """按 selector 取得小范围文本，未指定 selector 时保持原始分页语义。"""
    if selector is None:
        return lines, 0, None
    kind, value, context_lines = selector
    if kind == "json_path":
        try:
            document = json.loads("\n".join(lines))
        except json.JSONDecodeError as exc:
            raise ValueError("json_path 仅支持有效 JSON 文档") from exc
        selected = json.dumps(
            _json_path_value(document, value), ensure_ascii=False, indent=2, default=str
        ).splitlines()
        return selected, 0, f"json_path:{value}"

    if kind == "symbol":
        pattern = re.compile(
            rf"^\s*(?:class(?:_name)?|func|def|signal|var|const)\s+{re.escape(value)}\b"
        )
    else:
        pattern = re.compile(re.escape(value), re.IGNORECASE)
    match_index = next((index for index, line in enumerate(lines) if pattern.search(line)), None)
    if match_index is None:
        raise ValueError(f"未找到 selector：{kind}={value}")
    start = max(0, match_index - context_lines)
    end = min(len(lines), match_index + context_lines + 1)
    return lines[start:end], start, f"{kind}:{value}"


async def read_file_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """按行分页读取工程内文本文件，超出单次扫描上限时标记 scan_truncated。"""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path 不能为空")
    if not path_ok(path, ctx.security, write=False):
        raise ValueError("path 不在允许读取范围内")

    offset = _clamp_int(args.get("offset", 1), default=1, minimum=1, maximum=2**31 - 1)
    limit = _clamp_int(args.get("limit", DEFAULT_LIMIT_LINES), default=DEFAULT_LIMIT_LINES, minimum=1, maximum=MAX_LIMIT_LINES)
    selector = _selector_config(args)

    full_path = ctx.security.project_root / path
    logger.info(
        "read_file start session=%s path=%s offset=%d limit=%d",
        ctx.session_id,
        path,
        offset,
        limit,
    )

    with full_path.open("rb") as stream:
        chunk = stream.read(MAX_SCAN_BYTES + 1)
    scan_truncated = len(chunk) > MAX_SCAN_BYTES
    chunk = chunk[:MAX_SCAN_BYTES]
    text = chunk.decode("utf-8", errors="replace")
    # 统一换行符，避免 CRLF 文件里每一行末尾都带一个看不见的 \r 混进返回内容。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    all_lines = text.split("\n")
    total_lines_scanned = len(all_lines)
    selected_lines, selector_start, selected_by = _select_file_lines(all_lines, selector)
    if selector is None:
        start = offset - 1
        end = min(start + limit, total_lines_scanned)
        page_lines = selected_lines[start:end] if start < total_lines_scanned else []
    else:
        selected_offset = offset - 1
        start = selector_start + selected_offset
        end = min(selected_offset + limit, len(selected_lines))
        page_lines = selected_lines[selected_offset:end]
    page_lines, protected_line_count = _protect_opaque_lines(page_lines, start, path)
    page_text = "\n".join(page_lines)

    byte_truncated = False
    encoded = page_text.encode("utf-8")
    if len(encoded) > MAX_RETURN_BYTES:
        page_text = encoded[:MAX_RETURN_BYTES].decode("utf-8", errors="ignore")
        byte_truncated = True

    has_more = (end < len(selected_lines) if selector is not None else end < total_lines_scanned) or scan_truncated
    logger.info(
        "read_file success session=%s path=%s lines_returned=%d has_more=%s scan_truncated=%s",
        ctx.session_id,
        path,
        len(page_lines),
        has_more,
        scan_truncated,
    )
    return {
        "path": path,
        "content": page_text,
        "encoding": "utf-8",
        "offset": offset,
        "limit": limit,
        "lines_returned": len(page_lines),
        "total_lines_scanned": total_lines_scanned,
        "has_more": has_more,
        "scan_truncated": scan_truncated,
        "truncated": byte_truncated or has_more,
        "protected_line_count": protected_line_count,
        "selected_by": selected_by,
        "locator": (
            f"read_file(path={path!r}, selector={selected_by!r})"
            if selected_by is not None
            else f"read_file(path={path!r}, offset={offset}, limit=...)"
        ),
    }


def _protect_opaque_lines(
    page_lines: list[str], start_index: int, path: str
) -> tuple[list[str], int]:
    """把生成/压缩/不透明的超长物理行替换为定位符提示（任务 8.4）。

    `read_file` 保持"已定位普通文本范围的精确读取器"语义：遇到序列化
    `Packed*Array`/`tile_data`、minified 或异常长的物理行时，不整行吐出，
    而是返回带行号身份的提示与后续检索方式。

    Args:
        page_lines: 本页行内容。
        start_index: 本页首行在文件中的 0 基下标。
        path: 相对文件路径（用于判断结构化来源）。

    Returns:
        `(处理后的行列表, 被保护行数)`。
    """
    is_structured = path.lower().endswith(_STRUCTURED_SUFFIXES)
    protected = 0
    result: list[str] = []
    for index, line in enumerate(page_lines):
        line_no = start_index + index + 1
        too_long = len(line) > OPAQUE_LINE_CHARS
        serialized_field = (
            is_structured
            and len(line) > 500
            and _SERIALIZED_FIELD_RE.search(line[:512]) is not None
        )
        if too_long or serialized_field:
            reason = "超长物理行（生成/压缩/单行大文件）" if too_long else "结构化序列化字段"
            result.append(
                f"<<第 {line_no} 行未展开：{reason}，约 {len(line)} 字符。"
                "定位符：用 grep_code(pattern=...) 定位键/字段；场景/资源语义用 "
                "read_scene_tree / set_resource_property；地图单元用 "
                "describe_map_region(target_path=...)；普通文本请缩小 offset/limit 范围>>"
            )
            protected += 1
        else:
            result.append(line)
    return result, protected


def register_read_file_tool() -> None:
    """把 `read_file` 注册进全局工具表。"""
    register(
        ToolDef(
            name="read_file",
            domain="project",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="读取工程内文本文件内容",
            schema=READ_FILE_SCHEMA,
            handler=read_file_handler,
            path_args=["path"],
        )
    )
