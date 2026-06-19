"""Doctor 自检（§18.3）。

M0 只做无副作用事实检查：配置是否存在、路径是否可访问、工具是否注册、
provider 能力标志等。不会主动访问 LLM 端点，避免 `/doctor` 产生费用或外联。
"""

from __future__ import annotations

import logging
import sys

from app.api.schemas import DoctorResponse
from app.config import AppSettings
from app.llm.provider import LLMProvider
from app.lsp.status import lsp_status
from app.mcp.status import mcp_status
from app.memory.store import MemoryStore
from app.output_styles.catalog import OutputStyleCatalog
from app.rag.status import rag_status
from app.security.settings import SecuritySettings
from app.skills.catalog import SkillCatalog
from app.tools.registry import REGISTRY

logger = logging.getLogger(__name__)


def run_doctor(
    settings: AppSettings,
    security: SecuritySettings,
    llm: LLMProvider,
    *,
    auth_enabled: bool,
    skill_catalog: SkillCatalog | None = None,
    output_style_catalog: OutputStyleCatalog | None = None,
    memory_store: MemoryStore | None = None,
) -> DoctorResponse:
    """生成 `/doctor` 响应。"""
    logger.info("Doctor check start project_root=%s", settings.project_root)
    warnings: list[str] = []

    if not settings.project_root.exists():
        warnings.append(f"project_root 不存在：{settings.project_root}")
    if not settings.project_root.is_dir():
        warnings.append(f"project_root 不是目录：{settings.project_root}")
    if settings.host not in {"127.0.0.1", "localhost"}:
        warnings.append("服务未绑定到 127.0.0.1/localhost，请确认不会暴露到局域网")
    if not auth_enabled:
        warnings.append("未启用一次性 token，HTTP 请求会被拒绝")
    if (
        settings.llm_base_url.rstrip("/") == "https://api.openai.com/v1"
        and not settings.llm_api_key.get_secret_value()
    ):
        warnings.append("默认 OpenAI 端点未配置 API key")
    if not REGISTRY:
        warnings.append("没有注册任何工具")
    if settings.embedding_provider == "openai" and not settings.embedding_api_key.get_secret_value():
        warnings.append("Embedding 已选择 OpenAI，但未配置 embedding API key；将降级为 BM25")
    if settings.asset_understanding_enabled and not (
        settings.asset_understanding_model and settings.asset_understanding_endpoint
    ):
        warnings.append("资产语义理解已启用但 model/endpoint 不完整；仅使用确定性元数据索引")
    if not settings.rerank_model:
        warnings.append("未配置 rerank 模型；检索结果使用融合分数排序（跳过 cross-encoder 重排）")
    if not llm.supports_tool_calling:
        warnings.append("当前 LLM provider 未声明支持 tool calling")
    if skill_catalog is not None:
        skill_summaries = [summary.__dict__ for summary in skill_catalog.summaries()]
    else:
        skill_summaries = []
    output_style_summaries = (
        [summary.__dict__ for summary in output_style_catalog.summaries()]
        if output_style_catalog is not None
        else []
    )
    if memory_store is not None:
        memory_count = len(memory_store.list())
        if memory_count > 0:
            warnings.append(f"已启用项目记忆：{memory_count} 条")

    store_dir = settings.resolved_session_store_dir()
    response = DoctorResponse(
        python_version=sys.version.split()[0],
        auth_enabled=auth_enabled,
        project_root=str(security.project_root),
        permission_mode=security.permission_mode,
        trusted_project=security.trusted,
        enabled_domains=sorted(security.enabled_domains),
        registered_tools=sorted(REGISTRY),
        skills=skill_summaries,
        output_styles=output_style_summaries,
        capabilities={
            "rag": rag_status(security, settings.resolved_rag_index_path()),
            "lsp": lsp_status(),
            "mcp": mcp_status(),
        },
        llm_base_url_configured=settings.llm_base_url != "https://api.openai.com/v1",
        llm_model=settings.llm_model,
        session_store_dir=str(store_dir),
        warnings=warnings,
    )
    logger.info(
        "Doctor check complete warnings=%d tools=%d skills=%d output_styles=%d",
        len(response.warnings),
        len(response.registered_tools),
        len(response.skills),
        len(response.output_styles),
    )
    return response
