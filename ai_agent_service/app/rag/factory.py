"""按服务配置装配完整 EARS 索引。"""

from __future__ import annotations

import logging

from app.config import AppSettings
from app.rag.asset_llm_client import AssetLLMClient, AssetLLMConfig
from app.rag.embedding_client import EmbeddingClient, EmbeddingConfig
from app.rag.index import CodebaseIndex
from app.security.settings import SecuritySettings

logger = logging.getLogger(__name__)


def create_codebase_index(settings: AppSettings, security: SecuritySettings) -> CodebaseIndex:
    logger.debug(
        "Creating EARS index project_root=%s embedding_provider=%s embedding_model=%s "
        "asset_understanding=%s rerank_enabled=%s router_enabled=%s graph_depth=%d",
        security.project_root,
        settings.embedding_provider,
        settings.embedding_model,
        settings.asset_understanding_enabled,
        bool(settings.rerank_model),
        settings.rag_query_router_enabled,
        settings.graph_max_depth,
    )
    embedding = EmbeddingClient(EmbeddingConfig(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        endpoint=settings.embedding_endpoint,
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout_s=settings.embedding_timeout_s,
        retries=settings.embedding_retries,
    ))
    asset_llm = AssetLLMClient(AssetLLMConfig(
        enabled=settings.asset_understanding_enabled,
        model=settings.asset_understanding_model,
        endpoint=settings.asset_understanding_endpoint,
        api_key=settings.asset_understanding_api_key.get_secret_value(),
        timeout_s=settings.asset_understanding_timeout_s,
        max_tokens=settings.asset_understanding_max_tokens,
    ))
    return CodebaseIndex(
        security,
        settings.resolved_rag_index_path(),
        embedding,
        asset_llm_client=asset_llm,
        asset_enabled=settings.asset_understanding_enabled,
        query_router_enabled=settings.rag_query_router_enabled,
        graph_max_depth=settings.graph_max_depth,
        graph_max_neighbors=settings.graph_max_neighbors,
        rerank_model=settings.rerank_model,
        rerank_timeout_s=settings.rerank_timeout_s,
        token_budget=settings.rag_token_budget,
    )
