"""Server 工具集合：本服务直接执行、限定工程根、只读的工具。"""

from __future__ import annotations

from app.tools.server_tools.grep_code import register_grep_code_tool
from app.tools.server_tools.list_files import register_list_files_tool
from app.tools.server_tools.load_skill import register_load_skill_tool
from app.tools.server_tools.read_file import register_read_file_tool
from app.tools.server_tools.search_codebase import register_search_codebase_tool
from app.tools.server_tools.search_tools import register_search_tools_tool


def register_server_tools() -> None:
    """注册本包提供的全部 server 工具。"""
    register_list_files_tool()
    register_read_file_tool()
    register_grep_code_tool()
    register_load_skill_tool()
    register_search_tools_tool()
    register_search_codebase_tool()


def register_all() -> None:
    """向后兼容旧入口名。"""
    register_server_tools()
