## 转录断档恢复的有界状态机（fix-transcript-sync-recovery 任务 2.4）。
##
## 所有可见转录断档入口（传输序列缺口、服务端保留缺口、背压重同步、
## 投影器/渲染器拒绝、可见进度停滞）都收敛到这里，由它决定“从连续游标
## 续传”还是“原子水合权威快照”，并施加每回合有界预算，避免恢复死循环。
##
## 状态机：IDLE → RESUMING →（缺口仍未闭合）→ HYDRATING → IDLE。
## 恢复动作绝不重新提交聊天/审批/中断命令（任务 2.5）：本对象只输出
## "resume"/"hydrate" 决策，触发的都是订阅重连与只读历史请求。
extends RefCounted

const STATE_NAMES := ["idle", "resuming", "hydrating"]

enum State { IDLE, RESUMING, HYDRATING }

## 单回合内“先从连续游标续传”的最多尝试次数。
const RESUME_MAX_ATTEMPTS := 1
## 单回合内请求权威快照水合的最多次数（含探针重试）。
const HYDRATE_MAX_ATTEMPTS := 2

## 恢复回合开始（action 为 "resume" 或 "hydrate"；details 为脱敏诊断）。
signal recovery_started(action: String, details: Dictionary)
## 续传无法闭合缺口，升级为快照水合。
signal recovery_escalated(details: Dictionary)
## 恢复成功结束（detail 说明闭合方式）。
signal recovery_finished(detail: String)
## 预算耗尽：停止自动恢复，等待新的可见进度重置预算。
signal recovery_exhausted(details: Dictionary)

var state: int = State.IDLE
## 活跃轮次中判定可见停滞的阈值（秒）；可配置化以便按实测调整。
var stall_threshold_s := 20.0
## 当前回合的触发原因（脱敏标识）。
var reason := ""
## 最近一次服务端可见水位（由心跳/订阅确认携带）。
var server_visible_seq := 0

var _resume_attempts := 0
var _hydrate_attempts := 0
var _last_visible_progress_msec := 0
var _state_since_msec := 0
var _exhausted_reported := false


## 请求开始一次恢复。返回应执行的动作："resume"、"hydrate"，
## 或空字符串（已在恢复中/预算耗尽，调用方不做任何动作）。
##
## Args:
##   trigger: 触发源标识（如 "sequence_gap"、"visible_stall"）。
##   preferred: 首选动作；服务端类型化缺口/投影失败应直接 "hydrate"。
##   watermarks: 调用方当前脱敏水位快照，仅用于诊断日志。
func begin(trigger: String, preferred: String, watermarks: Dictionary = {}) -> String:
	var action := ""
	if state == State.HYDRATING:
		return ""
	if state == State.RESUMING:
		# 续传中再次断档 = 重放无法闭合缺口，升级为快照水合。
		action = "hydrate"
		reason = trigger
	else:
		reason = trigger
		action = preferred
	# 按动作类型施加有界预算。
	if action == "resume":
		if _resume_attempts >= RESUME_MAX_ATTEMPTS:
			action = "hydrate"
	if action == "hydrate":
		if _hydrate_attempts >= HYDRATE_MAX_ATTEMPTS:
			_report_exhausted(trigger, watermarks)
			return ""
	# 从续传升级到水合时清掉续传计数，保持预算语义清晰。
	if state == State.RESUMING and action == "hydrate":
		var details := _diagnostics(trigger, watermarks)
		state = State.HYDRATING
		_state_since_msec = Time.get_ticks_msec()
		_hydrate_attempts += 1
		recovery_escalated.emit(details)
		return "hydrate"
	state = State.RESUMING if action == "resume" else State.HYDRATING
	_state_since_msec = Time.get_ticks_msec()
	if action == "resume":
		_resume_attempts += 1
	else:
		_hydrate_attempts += 1
	_exhausted_reported = false
	recovery_started.emit(action, _diagnostics(trigger, watermarks))
	return action


## 快照水合在超时后仍未完成时的有界重试（任务 2.4）：只有处于 HYDRATING
## 且仍有水合预算时返回 "hydrate"，避免探针失败造成的永久 HYDRATING 死路。
func retry_hydration(trigger: String, watermarks: Dictionary = {}) -> String:
	if state != State.HYDRATING:
		return ""
	if _hydrate_attempts >= HYDRATE_MAX_ATTEMPTS:
		_report_exhausted(trigger, watermarks)
		return ""
	_hydrate_attempts += 1
	_state_since_msec = Time.get_ticks_msec()
	recovery_started.emit("hydrate", _diagnostics("retry_" + trigger, watermarks))
	return "hydrate"


## 当前状态已持续的毫秒数（供水合超时判断）。
func time_in_state_msec(now_msec: int) -> int:
	return maxi(0, now_msec - _state_since_msec)


## 恢复成功：重置状态与预算。调用方在缺口闭合（续传重放生效或快照水合完成）后调用。
func finish(detail: String) -> void:
	if state == State.IDLE:
		return
	state = State.IDLE
	_state_since_msec = Time.get_ticks_msec()
	_resume_attempts = 0
	_hydrate_attempts = 0
	reason = ""
	recovery_finished.emit(detail)


## 会话切换/重置时清空全部恢复状态。
func reset() -> void:
	state = State.IDLE
	_state_since_msec = Time.get_ticks_msec()
	reason = ""
	_resume_attempts = 0
	_hydrate_attempts = 0
	server_visible_seq = 0
	_last_visible_progress_msec = Time.get_ticks_msec()
	_exhausted_reported = false


## 客户端可见进度推进（接收/投影/渲染任一水位前进）：刷新停滞计时，
## 并把服务端水位同步到本地。
##
## 若当前处于 RESUMING：重放已经送达了新的可见条目 = 缺口被闭合，
## 立即结束本回合并重置预算；HYDRATING 的结束由快照应用方显式调用
## `finish` 表达（水合完成前不会有实时补丁被接受）。
func notify_visible_progress(server_seq: int = -1) -> void:
	_last_visible_progress_msec = Time.get_ticks_msec()
	if server_seq >= 0:
		server_visible_seq = maxi(server_visible_seq, server_seq)
	if state == State.RESUMING:
		state = State.IDLE
		_state_since_msec = Time.get_ticks_msec()
		_resume_attempts = 0
		_hydrate_attempts = 0
		reason = ""
		_exhausted_reported = false
		recovery_finished.emit("resume_closed_gap")
	elif state == State.IDLE:
		_resume_attempts = 0
		_hydrate_attempts = 0
		_exhausted_reported = false


## 服务端心跳/订阅确认带来的可见水位（只含序号，无正文）。
func notify_server_progress(visible_seq: int) -> void:
	server_visible_seq = maxi(server_visible_seq, visible_seq)


## 服务端可见水位是否领先于客户端已连续接收的游标。
func server_ahead_of(client_contiguous_seq: int) -> bool:
	return server_visible_seq > client_contiguous_seq


## 是否已超过停滞阈值没有任何可见进度。
func stalled(now_msec: int) -> bool:
	if _last_visible_progress_msec == 0:
		return false
	return now_msec - _last_visible_progress_msec >= int(stall_threshold_s * 1000.0)


## 供日志使用的状态名。
func state_name() -> String:
	return STATE_NAMES[clampi(state, 0, STATE_NAMES.size() - 1)]


func _diagnostics(trigger: String, watermarks: Dictionary) -> Dictionary:
	# 脱敏诊断：只含标识符、序号、计数与原因，绝不含条目正文。
	var details := {
		"trigger": trigger,
		"reason": reason,
		"recovery_state": state_name(),
		"resume_attempts": _resume_attempts,
		"hydrate_attempts": _hydrate_attempts,
		"server_visible_seq": server_visible_seq,
	}
	for key in watermarks:
		details[str(key)] = watermarks[key]
	return details


func _report_exhausted(trigger: String, watermarks: Dictionary) -> void:
	if _exhausted_reported:
		return
	_exhausted_reported = true
	recovery_exhausted.emit(_diagnostics(trigger, watermarks))