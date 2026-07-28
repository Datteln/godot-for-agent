@tool
extends RefCounted

# 纯格式化工具：把后端下发的诊断报告、命令输出、记忆条目和扩展信息
# 转成聊天面板展示用的中文字符串。不持有状态，不修改任何数据。


static func doctor_report(report: Dictionary) -> String:
	var capabilities: Dictionary = report.get("capabilities", {}) if report.get("capabilities", {}) is Dictionary else {}
	var lsp: Dictionary = capabilities.get("lsp", {}) if capabilities.get("lsp", {}) is Dictionary else {}
	var mcp: Dictionary = capabilities.get("mcp", {}) if capabilities.get("mcp", {}) is Dictionary else {}
	var rag: Dictionary = capabilities.get("rag", {}) if capabilities.get("rag", {}) is Dictionary else {}
	var lines: Array[String] = ["诊断报告", "", "基础状态"]
	lines.append("• Python：%s" % str(report.get("python_version", "未知")))
	lines.append("• 模型：%s" % str(report.get("llm_model", "未配置")))
	lines.append("• LLM 地址：%s" % status(bool(report.get("llm_base_url_configured", false))))
	lines.append("• 鉴权：%s" % status(bool(report.get("auth_enabled", false))))
	lines.append("• 权限模式：%s" % str(report.get("permission_mode", "未知")))
	lines.append("• 受信任项目：%s" % status(bool(report.get("trusted_project", false))))
	lines.append("• 项目目录：%s" % str(report.get("project_root", "未知")))
	lines.append("• 会话目录：%s" % str(report.get("session_store_dir", "未知")))
	lines.append_array(["", "能力"])
	lines.append("• LSP：%s；模式：%s；服务：%s" % [
		status(bool(lsp.get("enabled", false))), str(lsp.get("mode", "未知")), str(lsp.get("lsp_server", "未知"))
	])
	lines.append("  诊断来源：%s" % value_list(lsp.get("diagnostics_sources", [])))
	lines.append("  回退工具：%s" % value_list(lsp.get("fallbacks", [])))
	lines.append("• MCP：%s；模式：%s；权限：%s" % [
		status(bool(mcp.get("enabled", false))), str(mcp.get("mode", "未知")), str(mcp.get("permission_mode_when_enabled", "未知"))
	])
	lines.append("  入口：%s" % str(mcp.get("entrypoint", "未配置")))
	lines.append("• RAG：%s；模式：%s；策略：%s" % [
		status(bool(rag.get("enabled", false))), str(rag.get("mode", "未知")), str(rag.get("strategy", "未知"))
	])
	lines.append("  主索引：%s（%s）" % [
		"已创建" if bool(rag.get("index_exists", false)) else "未创建", str(rag.get("index_path", "未知"))
	])
	var sub_indexes: Dictionary = rag.get("sub_indexes", {}) if rag.get("sub_indexes", {}) is Dictionary else {}
	for index_name in sub_indexes.keys():
		var index_info: Dictionary = sub_indexes[index_name] if sub_indexes[index_name] is Dictionary else {}
		lines.append("  %s：%s（%s）" % [
			str(index_name), "已创建" if bool(index_info.get("exists", false)) else "未创建", str(index_info.get("path", "未知"))
		])
	lines.append_array(["", "启用域", value_list(report.get("enabled_domains", []))])
	var tools: Array = report.get("registered_tools", []) if report.get("registered_tools", []) is Array else []
	lines.append_array(["", "已注册工具（%d）" % tools.size(), value_list(tools)])
	lines.append_array(["", "输出风格"])
	var styles: Array = report.get("output_styles", []) if report.get("output_styles", []) is Array else []
	if styles.is_empty():
		lines.append("• 无")
	for style in styles:
		if style is Dictionary:
			lines.append("• %s：%s%s" % [
				str(style.get("name", "未命名")), str(style.get("description", "")), "" if bool(style.get("enabled", true)) else "（已禁用）"
			])
	lines.append_array(["", "技能"])
	var skills: Array = report.get("skills", []) if report.get("skills", []) is Array else []
	if skills.is_empty():
		lines.append("• 无")
	for skill in skills:
		if skill is Dictionary:
			lines.append("• %s：%s" % [str(skill.get("name", "未命名")), str(skill.get("description", ""))])
			lines.append("  工具：%s" % value_list(skill.get("effective_tools", [])))
	lines.append_array(["", "警告"])
	var warnings: Array = report.get("warnings", []) if report.get("warnings", []) is Array else []
	if warnings.is_empty():
		lines.append("• 无")
	else:
		for warning in warnings:
			lines.append("• %s" % str(warning))
	return "\n".join(lines)


static func status(enabled: bool) -> String:
	return "已启用" if enabled else "未启用"


static func value_list(values) -> String:
	if not (values is Array) or values.is_empty():
		return "无"
	var items := PackedStringArray()
	for value in values:
		items.append(str(value))
	return "、".join(items)


static func looks_like_command_list(values: Array) -> bool:
	if values.is_empty():
		return true
	for value in values:
		if not (value is Dictionary) or not value.has("name") or not value.has("description"):
			return false
	return true


static func commands_report(commands: Array) -> String:
	var lines: Array[String] = ["命令", "", "共 %d 个可用命令" % commands.size()]
	if commands.is_empty():
		lines.append("• 无")
		return "\n".join(lines)
	for command in commands:
		if not (command is Dictionary):
			continue
		lines.append("")
		lines.append("• %s" % str(command.get("name", "未命名")))
		lines.append("  %s" % str(command.get("description", "无说明")))
		var schema: Dictionary = command.get("args_schema", {}) if command.get("args_schema", {}) is Dictionary else {}
		var properties: Dictionary = schema.get("properties", {}) if schema.get("properties", {}) is Dictionary else {}
		if properties.is_empty():
			lines.append("  参数：无")
		else:
			var required: Array = schema.get("required", []) if schema.get("required", []) is Array else []
			var args: Array[String] = []
			for arg_name in properties.keys():
				var info: Dictionary = properties[arg_name] if properties[arg_name] is Dictionary else {}
				var label := str(arg_name) + "：" + str(info.get("type", "任意"))
				if required.has(arg_name):
					label += "，必填"
				elif info.has("default"):
					label += "，默认 %s" % str(info.get("default"))
				args.append(label)
			lines.append("  参数：%s" % "；".join(PackedStringArray(args)))
	return "\n".join(lines)


static func command_response(response: Dictionary) -> String:
	var text := str(response.get("text", "")).strip_edges()
	var result = response.get("result", null)
	if not (result is Dictionary):
		return text
	if result.has("python_version"):
		return doctor_report(result)
	if result.has("files") and result.has("chunks") and result.has("changed_files"):
		return rebuild_index_result(result)
	if result.has("compacted_frames") and result.has("removed_messages"):
		return compact_result(result)
	var formatted := plain_value("命令结果", result)
	return formatted if text.is_empty() else text + "\n\n" + formatted


static func rebuild_index_result(result: Dictionary) -> String:
	var lines: Array[String] = ["RAG 索引构建完成", ""]
	lines.append("• 本次处理文件：%d" % int(result.get("files", 0)))
	lines.append("• 索引片段：%d" % int(result.get("chunks", 0)))
	lines.append("• 发生变化的文件：%d" % int(result.get("changed_files", 0)))
	if result.has("vectors"):
		lines.append("• 向量数量：%d" % int(result.get("vectors", 0)))
	if result.has("symbols"):
		lines.append("• 符号数量：%d" % int(result.get("symbols", 0)))
	if result.has("assets"):
		lines.append("• 资源数量：%d" % int(result.get("assets", 0)))
	lines.append("• 文件数量是否超限：%s" % ("是" if bool(result.get("truncated_files", false)) else "否"))
	return "\n".join(lines)


static func compact_result(result: Dictionary) -> String:
	var lines: Array[String] = ["会话上下文压缩完成", ""]
	lines.append("• 压缩帧数：%d" % int(result.get("compacted_frames", 0)))
	lines.append("• 移除消息：%d" % int(result.get("removed_messages", 0)))
	lines.append("• 截断超长消息：%d" % int(result.get("truncated_messages", 0)))
	lines.append("• 待处理任务：%s" % ("已保留" if result.get("pending_turn_id", null) != null else "无"))
	return "\n".join(lines)


static func memory_report(response: Dictionary) -> String:
	var items: Array = response.get("items", []) if response.get("items", []) is Array else []
	var lines: Array[String] = ["记忆", "", "状态：%s" % ("成功" if bool(response.get("ok", true)) else "失败")]
	var response_text := str(response.get("text", "")).strip_edges()
	if response_text != "":
		lines.append("消息：%s" % response_text)
	lines.append("条目：%d" % items.size())
	if items.is_empty():
		lines.append("• 无")
		return "\n".join(lines)
	for index in range(items.size()):
		var item = items[index]
		if not (item is Dictionary):
			continue
		lines.append("")
		lines.append("%d. %s" % [index + 1, str(item.get("text", ""))])
		lines.append("   ID：%s" % str(item.get("id", "未知")))
		lines.append("   范围：%s；标签：%s" % [str(item.get("scope", "未知")), value_list(item.get("tags", []))])
		var updated_at := int(float(item.get("updated_at", 0.0)))
		if updated_at > 0:
			lines.append("   更新时间：%s" % Time.get_datetime_string_from_unix_time(updated_at, true))
	return "\n".join(lines)


static func extensions_report(payload: Dictionary) -> String:
	var skills: Array = payload.get("skills", []) if payload.get("skills", []) is Array else []
	var lines: Array[String] = ["扩展", "", "技能：%d" % skills.size()]
	if skills.is_empty():
		lines.append("• 无")
	for skill in skills:
		if not (skill is Dictionary):
			continue
		lines.append("")
		lines.append("• %s%s" % [
			str(skill.get("name", "未命名")), "" if bool(skill.get("enabled", true)) else "（已禁用）"
		])
		lines.append("  %s" % str(skill.get("description", "无说明")))
		var qualified_name := str(skill.get("qualified_name", "")).strip_edges()
		if qualified_name != "":
			lines.append("  标识：%s；来源：%s" % [qualified_name, str(skill.get("source", "未知"))])
		lines.append("  工具：%s" % value_list(skill.get("effective_tools", [])))
		var when_to_use := str(skill.get("when_to_use", "")).strip_edges()
		if when_to_use != "":
			lines.append("  使用时机：%s" % when_to_use)
	var warnings: Array = payload.get("warnings", []) if payload.get("warnings", []) is Array else []
	lines.append_array(["", "警告"])
	if warnings.is_empty():
		lines.append("• 无")
	else:
		for warning in warnings:
			lines.append("• %s" % str(warning))
	return "\n".join(lines)


static func plain_value(title: String, value) -> String:
	if value == null:
		return title + "\n\n无"
	if value is Array:
		return title + "\n\n" + value_list(value)
	if value is Dictionary:
		var lines: Array[String] = [title, ""]
		for key in value.keys():
			lines.append("• %s：%s" % [str(key), str(value[key])])
		return "\n".join(lines)
	return title + "\n\n" + str(value)
