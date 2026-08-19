"""派生只由后端运行时上下文控制的 CodeAct 身份。"""

from __future__ import annotations

import hashlib


def task_execution_id(session_id: str, session_epoch: str, owner_frame_id: str) -> str:
    """为一个稳定 owner frame 派生不可由模型覆盖的任务执行标识。"""
    material = "\0".join((session_id, session_epoch, owner_frame_id))
    return f"exec-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def codeact_call_id(execution_id: str, tool_call_id: str) -> str:
    """把模型响应内的调用标识绑定到可信执行标识以形成全局唯一键。"""
    material = "\0".join((execution_id, tool_call_id))
    return f"call-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"
