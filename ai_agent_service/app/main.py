"""FastAPI app 与 CLI 入口（§9.0 / §14）。

默认通过一次性 token 保护所有 HTTP 路由：
- Godot 前端可用 `--token-stdin` 把 token 作为 stdin 首行传入；
- 开发/测试也可设置 `AI_AGENT_AUTH_TOKEN`。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from collections.abc import Callable, Sequence

from fastapi import Depends, FastAPI, Header, HTTPException, status

from app.api.routes import create_router
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import OpenAICompatibleProvider
from app.logging_config import configure_logging
from app.mcp.server import run_mcp_stdio
from app.memory.store import MemoryStore
from app.output_styles.catalog import OutputStyleCatalog
from app.query.engine import QueryEngine
from app.recovery.pointer import RecoveryPointerStore
from app.security.settings import security_settings_from_app
from app.sessions.store import SessionStore
from app.skills.catalog import SkillCatalog
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY
from app.tools.server_tools import register_server_tools

logger = logging.getLogger(__name__)


def _auth_dependency(expected_token: str | None) -> Callable[[str | None], None]:
    """生成 Bearer token 鉴权依赖。"""

    def verify(authorization: str | None = Header(default=None)) -> None:
        if not expected_token:
            logger.warning("HTTP auth rejected: service token is not configured")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="服务未配置鉴权 token",
            )

        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected_token):
            logger.warning("HTTP auth rejected: invalid bearer token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的鉴权 token",
            )

    return verify


def _token_from_env() -> str | None:
    """读取开发/测试用 token 环境变量。"""
    value = os.environ.get("AI_AGENT_AUTH_TOKEN")
    return value if value else None


def create_app(settings: AppSettings | None = None, token: str | None = None) -> FastAPI:
    """创建 FastAPI 应用实例。"""
    resolved_settings = settings or AppSettings()
    configure_logging(resolved_settings.log_level, log_dir=resolved_settings.resolved_log_dir())
    resolved_token = token if token is not None else _token_from_env()

    logger.info(
        "Creating AI agent service app project_root=%s permission_mode=%s trusted=%s auth_enabled=%s model=%s",
        resolved_settings.project_root,
        resolved_settings.permission_mode,
        resolved_settings.trusted_project,
        resolved_token is not None,
        resolved_settings.llm_model,
    )

    register_server_tools()
    register_front_tools()

    security = security_settings_from_app(resolved_settings)
    llm = OpenAICompatibleProvider(
        base_url=resolved_settings.llm_base_url,
        api_key=resolved_settings.llm_api_key.get_secret_value(),
        default_model=resolved_settings.llm_model,
        timeout_s=resolved_settings.llm_request_timeout_s,
        fallback_model=resolved_settings.llm_fallback_model,
    )
    store = SessionStore(resolved_settings.resolved_session_store_dir())
    event_store = EventStore()
    recovery_store = RecoveryPointerStore(
        resolved_settings.resolved_recovery_pointer_path(),
        resolved_settings.project_root,
    )
    skill_catalog = SkillCatalog(resolved_settings, security, set(REGISTRY))
    output_style_catalog = OutputStyleCatalog(resolved_settings)
    memory_store = MemoryStore(resolved_settings.resolved_memory_store_path())
    query_engine = QueryEngine(
        settings=resolved_settings,
        session_store=store,
        llm=llm,
        base_security=security,
        skill_catalog=skill_catalog,
        output_style_catalog=output_style_catalog,
        event_store=event_store,
        recovery_store=recovery_store,
    )

    app = FastAPI(
        title="Godot AI Agent Service",
        version="0.1.0",
        dependencies=[Depends(_auth_dependency(resolved_token))],
    )
    app.include_router(
        create_router(
            settings=resolved_settings,
            security=security,
            llm=llm,
            query_engine=query_engine,
            auth_enabled=resolved_token is not None,
            event_store=event_store,
            recovery_store=recovery_store,
            skill_catalog=skill_catalog,
            output_style_catalog=output_style_catalog,
            memory_store=memory_store,
        )
    )
    logger.info(
        "AI agent service app ready tools=%d session_store=%s",
        len(REGISTRY),
        resolved_settings.resolved_session_store_dir(),
    )
    return app


app = create_app()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Run the local Godot AI agent service.")
    parser.add_argument("--token-stdin", action="store_true", help="从 stdin 首行读取 HTTP token")
    parser.add_argument("--mcp-stdio", action="store_true", help="以 MCP stdio server 模式启动")
    args = parser.parse_args(argv)

    settings = AppSettings()
    configure_logging(settings.log_level, log_dir=settings.resolved_log_dir())
    if args.mcp_stdio:
        logger.info("Starting AI agent service in MCP stdio mode")
        return asyncio.run(run_mcp_stdio(settings))

    token = sys.stdin.readline().strip() if args.token_stdin else _token_from_env()
    cli_app = create_app(settings=settings, token=token)

    import uvicorn

    logger.info("Starting HTTP server host=%s port=%s", settings.host, settings.port)
    uvicorn.run(cli_app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
