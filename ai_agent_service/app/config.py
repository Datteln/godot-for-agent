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
    llm_fallback_model: str | None = Field(
        default=None,
        description="主模型不可用时的降级模型名；为空表示不降级。",
    )
    llm_request_timeout_s: float = Field(
        default=60.0,
        description="单次 LLM 请求超时时间（秒）。",
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
    output_styles_dir: Path = Field(
        default_factory=lambda: Path(".ai_agent") / "output_styles",
        description="项目级 OutputStyle 目录；未信任工程默认不启用项目级样式。",
    )

    max_turns: int = Field(default=12, description="单次用户消息驱动的 agent 循环最大轮数。")

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
