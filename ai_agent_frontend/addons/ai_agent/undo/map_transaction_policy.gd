@tool
extends RefCounted

## 地图写事务的唯一边界策略。
##
## - 未携带 map_transaction_id 的写入是 single_tool，工具成功即提交。
## - planner-approved 写组必须携带稳定 id，并等待同 target/revision 的验证器提交。
## - 写组超过任一上限时整体回滚，绝不把半组静默拆成多个 Undo action。

const MODE_SINGLE_TOOL := "single_tool"
const MODE_APPROVED_WRITE_GROUP := "approved_write_group"

const MAX_TOOLS := 12
const MAX_DURATION_MS := 120_000
const MAX_SNAPSHOT_BYTES := 16 * 1024 * 1024
const MAX_JOURNAL_BYTES := 32 * 1024 * 1024
# 10k 是当前同步 recovery 在 250ms 策略内验证过的最大 fixture；
# 更大的 journal fail closed，避免首次写入在主线程阻塞数秒。
const MAX_RECOVERY_OPERATIONS := 10_000
const MAX_RECOVERY_LATENCY_MS := 250


static func mode(input: Dictionary) -> String:
	var transaction_id := str(input.get("map_transaction_id", "")).strip_edges()
	var requested_mode := str(input.get("map_transaction_mode", "")).strip_edges()
	if transaction_id != "" and requested_mode == MODE_APPROVED_WRITE_GROUP:
		return MODE_APPROVED_WRITE_GROUP
	return MODE_SINGLE_TOOL


static func validate_group_limits(
	started_at_ms: int,
	tool_count: int,
	snapshot_bytes: int
) -> Dictionary:
	var duration_ms := Time.get_ticks_msec() - started_at_ms
	if tool_count > MAX_TOOLS:
		return _limit_error(
			"map_transaction_tool_limit",
			"Map write group exceeded the %d-tool limit." % MAX_TOOLS,
			duration_ms,
			tool_count,
			snapshot_bytes
		)
	if duration_ms > MAX_DURATION_MS:
		return _limit_error(
			"map_transaction_duration_limit",
			"Map write group exceeded the %d ms duration limit." % MAX_DURATION_MS,
			duration_ms,
			tool_count,
			snapshot_bytes
		)
	if snapshot_bytes > MAX_SNAPSHOT_BYTES:
		return _limit_error(
			"map_transaction_snapshot_limit",
			"Map write group exceeded the %d-byte snapshot limit." % MAX_SNAPSHOT_BYTES,
			duration_ms,
			tool_count,
			snapshot_bytes
		)
	return {}


static func _limit_error(
	error_code: String,
	message: String,
	duration_ms: int,
	tool_count: int,
	snapshot_bytes: int
) -> Dictionary:
	return {
		"ok": false,
		"error_code": error_code,
		"message": message,
		"duration_ms": duration_ms,
		"tool_count": tool_count,
		"snapshot_bytes": snapshot_bytes,
		"limits": {
			"max_tools": MAX_TOOLS,
			"max_duration_ms": MAX_DURATION_MS,
			"max_snapshot_bytes": MAX_SNAPSHOT_BYTES,
		},
	}
