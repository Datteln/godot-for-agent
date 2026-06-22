"""服务级配置：大模型端点/密钥/模型与运行参数。

所有取值均来自环境变量或本地 `.env` 文件（不入版本库），对应需求文档
FR-18/NFR-4：端点、API key、模型名由用户本地配置，不硬编码、不进版本库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.permissions.types import PermRule


class AppSettings(BaseSettings):
    """服务运行所需的全部配置项。

    所有字段都可通过环境变量（前缀 `AI_AGENT_`）或工作目录下的 `.env` 文件覆盖。
    `llm_api_key` 使用 `SecretStr`，避免在日志、`/doctor`、异常信息中被原样打印。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI 兼容 Chat Completions 端点 base_url，支持任意 BYO key 的兼容端点。",
    )
    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="大模型 API key，仅服务端本地持有，不写入响应、日志或导出包。",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="默认对话模型名。",
    )
    llm_quick_model: str | None = Field(default=None, description="quick effort 模型；为空时使用 llm_model。")
    llm_standard_model: str | None = Field(default=None, description="standard effort 模型；为空时使用 llm_model。")
    llm_deep_model: str | None = Field(default=None, description="deep effort 模型；为空时使用 llm_model。")
    llm_verify_model: str | None = Field(default=None, description="verify effort 模型；为空时使用 llm_model。")
    llm_advisor_model: str | None = Field(default=None, description="advisor effort 模型；为空时使用 llm_model。")
    llm_thinking_budget_quick: int | None = Field(default=None, description="quick effort 的 thinking token 预算；为空时使用内置默认值（1024）。")
    llm_thinking_budget_standard: int | None = Field(default=None, description="standard effort 的 thinking token 预算；为空时使用内置默认值（4096）。")
    llm_thinking_budget_deep: int | None = Field(default=None, description="deep effort 的 thinking token 预算；为空时使用内置默认值（16384）。")
    llm_thinking_budget_verify: int | None = Field(default=None, description="verify effort 的 thinking token 预算；为空时使用内置默认值（0，关闭 thinking）。")
    llm_thinking_budget_advisor: int | None = Field(default=None, description="advisor effort 的 thinking token 预算；为空时使用内置默认值（2048）。")
    llm_fallback_model: str | None = Field(
        default=None,
        description="主模型不可用时的降级模型名；为空表示不降级。",
    )
    llm_request_timeout_s: float = Field(
        default=60.0,
        description="单次 LLM 请求超时时间（秒）。",
    )
    log_level: str = Field(
        default="DEBUG",
        description="服务日志等级，可选 DEBUG/INFO/WARNING/ERROR/CRITICAL。",
    )

    log_dir: Path = Field(
        default_factory=lambda: Path("logs"),
        description="日志文件存储目录，相对路径相对于 project_root。",
    )
    managed_process: bool = Field(
        default=False,
        description=(
            "是否由 Godot 插件通过 `OS.execute_with_pipe` 启动并持有 stdio 管道。"
            "该管道目前没有被消费方读取，写满后会让控制台 handler 的下一次写入"
            "永久阻塞、冻住整个事件循环；此时禁用控制台日志，只保留文件日志。"
        ),
    )

    project_root: Path = Field(
        default_factory=Path.cwd,
        description="当前 Godot 工程根目录；server 工具的检索/读取均限定于此目录。",
    )
    permission_mode: Literal["default", "plan", "auto_approve", "read_only"] = Field(
        default="default",
        description="会话初始权限模式，可被单次请求的 permission_mode 覆盖。",
    )
    trusted_project: bool = Field(
        default=False,
        description="工程是否已被用户标记为受信任；未信任时 auto_approve/allow 规则降级（§9 信任模型）。",
    )
    deny_rules: list[PermRule] = Field(
        default_factory=list,
        description=(
            "显式 deny 规则列表（§8.3），不受信任状态影响，始终优先生效。"
            "通过环境变量配置时使用 JSON 数组，例如 "
            '[{"match": {"tool": "run_*"}, "effect": "deny"}]。'
        ),
    )
    allow_rules: list[PermRule] = Field(
        default_factory=list,
        description=(
            "显式 allow 规则列表（§8.3），仅在 trusted_project=true 时生效，"
            "JSON 数组格式同 deny_rules。"
        ),
    )

    host: str = Field(default="127.0.0.1", description="仅绑定本机回环地址。")
    port: int = Field(default=0, description="监听端口；0 表示由操作系统分配随机端口。")

    session_store_dir: Path = Field(
        default_factory=lambda: Path(".ai_agent_service") / "sessions",
        description="会话本地持久化目录（§14.2），相对路径相对于 project_root。",
    )
    recovery_pointer_path: Path = Field(
        default_factory=lambda: Path(".ai_agent_service") / "recovery_pointer.json",
        description="最小恢复指针路径（§14.3），相对路径相对于 project_root。",
    )
    user_skills_dir: Path = Field(
        default_factory=lambda: Path.home() / ".ai_agent" / "skills",
        description="用户级 Skill 目录；每个子目录包含 SKILL.md。",
    )
    project_skills_dir: Path = Field(
        default_factory=lambda: Path(".ai_agent") / "skills",
        description="项目级 Skill 目录；未信任工程默认不启用项目级 Skill。",
    )
    memory_store_path: Path = Field(
        default_factory=lambda: Path(".ai_agent_service") / "memory.json",
        description="项目本地记忆存储；不保存 token、API key 或完整敏感对话。",
    )
    rag_index_path: Path = Field(
        default_factory=lambda: Path(".ai_agent_service") / "rag_index.json",
        description="本地 RAG 检索索引路径；仅保存工程内代码片段 token，不包含密钥。",
    )
    embedding_provider: Literal["disabled", "openai", "local", "bge-m3"] = Field(
        default="disabled", description="Embedding 提供方；disabled 时静默降级为 BM25。"
    )
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_endpoint: str = Field(default="https://api.openai.com/v1")
    embedding_api_key: SecretStr = Field(default=SecretStr(""))
    embedding_timeout_s: float = Field(default=3.0, ge=0.1, le=3.0)
    embedding_retries: int = Field(default=1, ge=0, le=2)
    rerank_model: str = Field(default="", description="空值表示跳过 cross-encoder 重排。")
    rerank_timeout_s: float = Field(default=2.0, ge=0.1, le=2.0)
    rag_query_router_enabled: bool = Field(default=True)
    rag_auto_build_enabled: bool = Field(
        default=True,
        description="服务启动后是否在后台自动增量构建 RAG/EARS 全部索引。",
    )
    rag_auto_watch_interval_s: float = Field(default=1.0, ge=0.1, le=60.0)
    rag_auto_watch_debounce_s: float = Field(default=0.75, ge=0.0, le=30.0)
    rag_auto_watch_scan_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=120.0,
        description="文件监视器扫描项目目录的超时；超时仅跳过本轮，不阻塞事件循环。",
    )
    rag_token_budget: int = Field(default=1500, ge=128)
    graph_max_depth: int = Field(default=2, ge=0, le=8)
    graph_max_neighbors: int = Field(default=5, ge=1, le=100)
    asset_understanding_enabled: bool = Field(default=False)
    asset_understanding_model: str = Field(default="")
    asset_understanding_endpoint: str = Field(default="")
    asset_understanding_api_key: SecretStr = Field(default=SecretStr(""))
    asset_understanding_timeout_s: float = Field(default=10.0, ge=0.1)
    asset_understanding_max_tokens: int = Field(default=500, ge=1)
    asset_understanding_concurrency: int = Field(default=3, ge=1, le=16)
    output_styles_dir: Path = Field(
        default_factory=lambda: Path(".ai_agent") / "output_styles",
        description="项目级 OutputStyle 目录；未信任工程默认不启用项目级样式。",
    )

    max_turns: int = Field(
        default=36,
        description="单次用户消息驱动的 agent 循环最大轮数（跨根帧与所有委派子帧的全局兜底上限；"
        "各帧自身的预算见 AgentDefinition.max_turns）。",
    )

    auto_compact_enabled: bool = Field(
        default=True,
        description="是否在驱动 LLM 前自动检查会话历史体积，超出阈值时自动执行一次本地压缩"
        "（§16.1 策略 A）；为 False 时仅保留手动 /compact 命令。",
    )
    auto_compact_token_threshold: int = Field(
        default=200_000,
        ge=1000,
        description="自动压缩的预估 token 阈值（按 estimate_message_tokens 粗估，非精确计费值）；"
        "当前活跃帧的消息预估 token 数超过该值时，在本轮驱动 LLM 之前自动压缩一次，"
        "为模型上下文窗口与延迟留出余量，而不是等到手动 /compact 或请求失败才处理。",
    )
    auto_compact_keep_recent: int = Field(
        default=12,
        ge=6,
        description="自动压缩时保留的最近消息数，语义与 /compact 命令的 keep_recent 参数一致。",
    )

    verify_after_edit: bool = Field(
        default=True,
        description="编辑类 front 工具成功落地后是否自动触发校验；为 False 时完全跳过 Verify 功能。",
    )
    verify_trigger_tools: list[str] = Field(
        default_factory=lambda: [
            "propose_script_edit",
            "write_file",
            "apply_text_edit",
            "propose_tests",
            "propose_content_file",
        ],
        description="触发自动校验的工具名集合，可通过环境变量覆盖（JSON 数组）。",
    )
    verify_syntax_enabled: bool = Field(
        default=True,
        description="是否启用 Phase 1 语法快检（bash/CLI）；为 False 时直接进入 Phase 2 语义校验。",
    )
    verify_syntax_timeout: int = Field(
        default=10,
        description="Phase 1 语法快检命令的超时秒数，超时视为该阶段跳过。",
    )
    verify_godot_path: str = Field(
        default="godot",
        description="Godot 可执行文件路径，用于 GDScript 语法检查（--headless --check-only）。",
    )
    verify_effort: str = Field(
        default="verify",
        description="Phase 2 语义校验使用的 effort 档位，决定模型与采样温度。",
    )
    verify_max_retries: int = Field(
        default=2,
        description="单次编辑（按文件路径计）允许的最大校验-修复重试次数，超过后跳过后续校验。",
    )

    def resolved_log_dir(self) -> Path:
        """返回日志文件存储目录的绝对路径。

        Returns:
            日志目录的绝对路径。
        """
        if self.log_dir.is_absolute():
            return self.log_dir
        return self.project_root / self.log_dir

    def resolved_session_store_dir(self) -> Path:
        """返回会话持久化目录的绝对路径，必要时基于 project_root 解析相对路径。

        Returns:
            会话持久化目录的绝对路径。
        """
        if self.session_store_dir.is_absolute():
            return self.session_store_dir
        return self.project_root / self.session_store_dir

    def resolved_recovery_pointer_path(self) -> Path:
        """返回最小恢复指针的绝对路径。"""
        if self.recovery_pointer_path.is_absolute():
            return self.recovery_pointer_path
        return self.project_root / self.recovery_pointer_path

    def resolved_project_skills_dir(self) -> Path:
        """返回项目级 Skill 目录绝对路径。"""
        if self.project_skills_dir.is_absolute():
            return self.project_skills_dir
        return self.project_root / self.project_skills_dir

    def resolved_memory_store_path(self) -> Path:
        """返回项目本地 memory 文件绝对路径。"""
        if self.memory_store_path.is_absolute():
            return self.memory_store_path
        return self.project_root / self.memory_store_path

    def resolved_rag_index_path(self) -> Path:
        """返回本地 RAG 索引文件绝对路径。"""
        if self.rag_index_path.is_absolute():
            return self.rag_index_path
        return self.project_root / self.rag_index_path

    def resolved_output_styles_dir(self) -> Path:
        """返回项目级 OutputStyle 目录绝对路径。"""
        if self.output_styles_dir.is_absolute():
            return self.output_styles_dir
        return self.project_root / self.output_styles_dir
