"""统一 CodeAct 执行边界。

本包承载任务隔离、网关、工作区证据、Editor 观察和审计等与具体 agent
无关的执行能力。旧 front tool 不得绕过这些组件执行项目写入或宿主命令。
"""

from app.codeact.contracts import CodeActRequest, CodeActResult, CodeActToolName

__all__ = ["CodeActRequest", "CodeActResult", "CodeActToolName"]
