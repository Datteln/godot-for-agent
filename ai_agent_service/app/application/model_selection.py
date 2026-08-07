"""Application-level model and thinking-budget selection policy."""

from __future__ import annotations

from app.agents.types import EffortLevel
from app.config import AppSettings


def model_for_effort(settings: AppSettings, effort: EffortLevel) -> str | None:
    """按 effort 档位解析配置中的模型覆盖名称。

    Args:
        settings: 应用配置对象，包含各 effort 档位的模型字段。
        effort: 请求的 effort 档位。

    Returns:
        对应档位的模型名称；未配置或未知档位时为 None。
    """
    return {
        "quick": settings.llm_quick_model,
        "standard": settings.llm_standard_model,
        "deep": settings.llm_deep_model,
        "verify": settings.llm_verify_model,
        "advisor": settings.llm_advisor_model,
    }.get(effort)


def thinking_budget_for_effort(settings: AppSettings, effort: EffortLevel) -> int | None:
    """按 effort 档位解析配置中的 thinking token 预算。

    Args:
        settings: 应用配置对象，包含各 effort 档位的 thinking 预算字段。
        effort: 请求的 effort 档位。

    Returns:
        对应档位的 thinking 预算；未配置或未知档位时为 None。
    """
    return {
        "quick": settings.llm_thinking_budget_quick,
        "standard": settings.llm_thinking_budget_standard,
        "deep": settings.llm_thinking_budget_deep,
        "verify": settings.llm_thinking_budget_verify,
        "advisor": settings.llm_thinking_budget_advisor,
    }.get(effort)
