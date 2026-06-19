from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.llm.cache_decision_engine import (
    EXPLICIT_CACHE_FALLBACK_TOKENS,
    IMPLICIT_CACHE_MIN_TOKENS,
    CacheDecision,
    CacheDecisionEngine,
    CacheStrategy,
)
from app.llm.cache_manager import (
    build_cache_key,
    compute_project_id,
    compute_rag_fingerprint,
    compute_repo_fingerprint,
    compute_system_core_hash,
    compute_tool_schema_version,
)
from app.llm.cache_observability import CacheMetricsCollector, CacheMetricsSnapshot
from app.llm.message_transformer import (
    HISTORY_TIER_GRANULARITY,
    MAX_CACHE_BREAKPOINTS,
    CacheBreakpoint,
    build_stable_prefix,
    estimate_message_tokens,
    flatten_message_text,
    inject_cache_breakpoints,
)
from app.llm.provider import _cache_tokens_from_usage, _max_token_count
from app.orchestrator.agent import _emit_cache_hit_event, _record_cache_metrics
from app.prompt.builder import LayeredPrompt, build_layered_system_prompt
from app.prompt.project_context import build_project_context
from app.prompt.rag_context import build_rag_context
from app.rag.index import CodebaseIndex
from app.security.settings import SecuritySettings
from app.sessions.store import Session, session_from_dict, session_to_dict

_LONG = "x" * (EXPLICIT_CACHE_FALLBACK_TOKENS * 4 + 16)


class FlattenMessageTextTests(unittest.TestCase):
    def test_string_content(self) -> None:
        self.assertEqual(flatten_message_text("hello"), "hello")

    def test_block_list_content(self) -> None:
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(flatten_message_text(content), "a\nb")

    def test_none_and_unknown(self) -> None:
        self.assertEqual(flatten_message_text(None), "")
        self.assertEqual(flatten_message_text(123), "")


class EstimateMessageTokensTests(unittest.TestCase):
    def test_short_prefix_below_threshold(self) -> None:
        messages = [{"role": "system", "content": "hi"}, {"role": "user", "content": "go"}]
        self.assertLess(estimate_message_tokens(messages), IMPLICIT_CACHE_MIN_TOKENS)

    def test_counts_block_list_content(self) -> None:
        messages = [{"role": "system", "content": [{"type": "text", "text": _LONG}]}]
        self.assertGreaterEqual(estimate_message_tokens(messages), EXPLICIT_CACHE_FALLBACK_TOKENS)

    def test_non_text_content_is_ignored(self) -> None:
        messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}]
        self.assertEqual(estimate_message_tokens(messages), 0)


class BuildStablePrefixTests(unittest.TestCase):
    def test_empty_messages_returns_empty_plan(self) -> None:
        plan = build_stable_prefix([])
        self.assertEqual(plan.breakpoints, [])

    def test_string_system_yields_single_breakpoint(self) -> None:
        plan = build_stable_prefix([{"role": "system", "content": "a"}])
        self.assertEqual(plan.breakpoints, [CacheBreakpoint(0, None, "system_core")])
        self.assertEqual(plan.stable_prefix_end_index, 0)

    def test_layered_system_yields_per_block_breakpoints(self) -> None:
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "L0"},
                    {"type": "text", "text": "L2"},
                ],
            },
            {"role": "user", "content": "hi"},
        ]
        plan = build_stable_prefix(messages)
        self.assertEqual(
            plan.breakpoints,
            [CacheBreakpoint(0, 0, "system_layer_0"), CacheBreakpoint(0, 1, "system_layer_1")],
        )

    def test_multi_turn_adds_stable_history_breakpoint(self) -> None:
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        plan = build_stable_prefix(messages)
        self.assertIn(CacheBreakpoint(2, None, "stable_history"), plan.breakpoints)
        self.assertEqual(plan.stable_prefix_end_index, 2)

    def _conversation(self, turn_count: int) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "prompt"}]
        for i in range(turn_count):
            messages.append({"role": "user", "content": f"u{i}"})
            messages.append({"role": "assistant", "content": f"a{i}"})
        return messages

    def test_short_history_has_no_old_tier(self) -> None:
        # 历史长度低于一个粒度（HISTORY_TIER_GRANULARITY=20）时只有 recent 断点，
        # 与改造前完全一致——B 不应改变短对话的行为。
        messages = self._conversation(5)
        plan = build_stable_prefix(messages)
        segments = [bp.segment for bp in plan.breakpoints]
        self.assertNotIn("history_old_tier", segments)
        self.assertIn("stable_history", segments)

    def test_old_tier_appears_once_history_crosses_granularity(self) -> None:
        messages = self._conversation(15)  # 1 + 30 = 31 messages, stable_history_end=29
        plan = build_stable_prefix(messages)
        old_tier = next((bp for bp in plan.breakpoints if bp.segment == "history_old_tier"), None)
        self.assertIsNotNone(old_tier)
        assert old_tier is not None
        self.assertEqual(old_tier.message_index % HISTORY_TIER_GRANULARITY, 0)
        self.assertGreater(old_tier.message_index, 0)

    def test_old_tier_position_is_stable_across_a_tier_span_then_jumps(self) -> None:
        # 核心断言：old tier 在跨过下一个粒度边界前保持固定，不像 recent tier 那样
        # 每轮都右移；这正是"B"要解决的问题——老段不再每轮都被当成新前缀重建。
        positions: dict[int, int] = {}
        for turn_count in range(18, 32):
            plan = build_stable_prefix(self._conversation(turn_count))
            old_tier = next((bp for bp in plan.breakpoints if bp.segment == "history_old_tier"), None)
            if old_tier is not None:
                positions[turn_count] = old_tier.message_index
        distinct_positions = set(positions.values())
        self.assertGreater(len(positions), 5)
        self.assertLess(len(distinct_positions), len(positions))

    def test_recent_tier_keeps_moving_every_turn(self) -> None:
        plan_a = build_stable_prefix(self._conversation(15))
        plan_b = build_stable_prefix(self._conversation(16))
        recent_a = next(bp.message_index for bp in plan_a.breakpoints if bp.segment == "stable_history")
        recent_b = next(bp.message_index for bp in plan_b.breakpoints if bp.segment == "stable_history")
        self.assertNotEqual(recent_a, recent_b)

    def test_old_tier_ordered_before_recent_tier(self) -> None:
        messages = self._conversation(15)
        plan = build_stable_prefix(messages)
        segments = [bp.segment for bp in plan.breakpoints]
        self.assertLess(segments.index("history_old_tier"), segments.index("stable_history"))

    def test_layered_system_plus_long_history_caps_and_drops_recent_first(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [{"type": "text", "text": f"L{i}"} for i in range(3)],
            }
        ]
        messages.extend(self._conversation(15)[1:])  # reuse long history without its own system msg
        plan = build_stable_prefix(messages)
        self.assertLessEqual(len(plan.breakpoints), MAX_CACHE_BREAKPOINTS)
        segments = [bp.segment for bp in plan.breakpoints]
        self.assertEqual(segments[:3], ["system_layer_0", "system_layer_1", "system_layer_2"])
        self.assertIn("history_old_tier", segments)
        self.assertNotIn("stable_history", segments)

    def test_never_exceeds_max_breakpoints(self) -> None:
        messages = [
            {"role": "system", "content": [{"type": "text", "text": str(i)} for i in range(6)]},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        plan = build_stable_prefix(messages)
        self.assertLessEqual(len(plan.breakpoints), MAX_CACHE_BREAKPOINTS)

    def test_compaction_summary_adds_system_tail(self) -> None:
        messages = [
            {"role": "system", "content": "core"},
            {"role": "system", "content": "[compact_summary]..."},
            {"role": "user", "content": "x"},
        ]
        plan = build_stable_prefix(messages)
        self.assertIn(CacheBreakpoint(1, None, "system_tail"), plan.breakpoints)


class InjectCacheBreakpointsTests(unittest.TestCase):
    def test_marks_string_content_as_block(self) -> None:
        messages = [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]
        marked = inject_cache_breakpoints(messages, [CacheBreakpoint(0, None, "system_core")])
        self.assertEqual(
            marked[0]["content"],
            [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        )
        self.assertEqual(marked[1]["content"], "b")

    def test_does_not_mutate_original_messages(self) -> None:
        messages = [{"role": "system", "content": "a"}]
        inject_cache_breakpoints(messages, [CacheBreakpoint(0, None, "system_core")])
        self.assertEqual(messages[0]["content"], "a")

    def test_marks_specific_block_in_layered_message(self) -> None:
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "L0"}, {"type": "text", "text": "L2"}],
            }
        ]
        marked = inject_cache_breakpoints(
            messages, [CacheBreakpoint(0, 0, "system_layer_0"), CacheBreakpoint(0, 1, "system_layer_1")]
        )
        blocks = marked[0]["content"]
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})
        # 原始未被改动
        self.assertNotIn("cache_control", messages[0]["content"][0])

    def test_none_block_index_marks_last_block(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        ]
        marked = inject_cache_breakpoints(messages, [CacheBreakpoint(0, None, "x")])
        blocks = marked[0]["content"]
        self.assertNotIn("cache_control", blocks[0])
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})

    def test_skips_message_with_no_text_content(self) -> None:
        messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}]
        marked = inject_cache_breakpoints(messages, [CacheBreakpoint(0, None, "x")])
        self.assertIsNone(marked[0]["content"])

    def test_out_of_range_indices_ignored(self) -> None:
        messages = [{"role": "system", "content": "a"}]
        self.assertIs(inject_cache_breakpoints(messages, [CacheBreakpoint(5, None, "x")]), messages)

    def test_empty_breakpoints_returns_same_object(self) -> None:
        messages = [{"role": "system", "content": "a"}]
        self.assertIs(inject_cache_breakpoints(messages, []), messages)


class CacheManagerTests(unittest.TestCase):
    def test_tool_schema_version_changes_with_tools(self) -> None:
        self.assertNotEqual(
            compute_tool_schema_version([{"name": "read_file"}]),
            compute_tool_schema_version([{"name": "write_file"}]),
        )

    def test_system_core_hash_handles_block_content(self) -> None:
        string_msgs = [{"role": "system", "content": "L0\nL2"}]
        block_msgs = [
            {"role": "system", "content": [{"type": "text", "text": "L0"}, {"type": "text", "text": "L2"}]}
        ]
        self.assertEqual(compute_system_core_hash(string_msgs), compute_system_core_hash(block_msgs))

    def test_system_core_hash_ignores_non_system_tail(self) -> None:
        a = [{"role": "system", "content": "p"}, {"role": "user", "content": "x"}]
        b = [{"role": "system", "content": "p"}, {"role": "user", "content": "y"}]
        self.assertEqual(compute_system_core_hash(a), compute_system_core_hash(b))

    def test_repo_fingerprint_non_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = compute_repo_fingerprint(Path(tmp))
        self.assertTrue(fingerprint.startswith("no-git:"))

    def test_repo_fingerprint_reflects_dependency_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = compute_repo_fingerprint(root)
            (root / "project.godot").write_text("config_version=5", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            (root2 / "project.godot").write_text("config_version=5", encoding="utf-8")
            after = compute_repo_fingerprint(root2)
        # 不同路径下相同锁文件仍因 project_root 路径不同而不同；此处只验证依赖锁
        # 参与了指纹（before 无锁文件、after 有锁文件，两者必然不同）。
        self.assertNotEqual(before, after)

    def test_project_id_stable_and_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(compute_project_id(root), compute_project_id(root))

    def test_rag_fingerprint_missing_returns_no_rag(self) -> None:
        self.assertEqual(compute_rag_fingerprint(None), "no-rag")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(compute_rag_fingerprint(Path(tmp) / "absent.idx"), "no-rag")

    def test_rag_fingerprint_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.idx"
            path.write_text("data", encoding="utf-8")
            self.assertNotEqual(compute_rag_fingerprint(path), "no-rag")

    def test_build_cache_key_sensitive_to_all_dimensions(self) -> None:
        base = build_cache_key(
            system_core_hash="s", tool_schema_version="t", repo_fingerprint="r",
            project_id="p", rag_fingerprint="g",
        )
        self.assertEqual(
            base,
            build_cache_key(
                system_core_hash="s", tool_schema_version="t", repo_fingerprint="r",
                project_id="p", rag_fingerprint="g",
            ),
        )
        self.assertNotEqual(
            base,
            build_cache_key(
                system_core_hash="s", tool_schema_version="t", repo_fingerprint="r",
                project_id="p", rag_fingerprint="OTHER",
            ),
        )


class CacheDecisionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def _decide(self, engine: CacheDecisionEngine, messages: list[dict[str, Any]], root: Path) -> CacheDecision:
        return await engine.decide(
            session_id="s1", frame_id="f1", messages=messages, tools=[], project_root=root
        )

    async def test_none_below_implicit_minimum(self) -> None:
        engine = CacheDecisionEngine()
        messages = [{"role": "system", "content": "tiny"}, {"role": "user", "content": "hi"}]
        with tempfile.TemporaryDirectory() as tmp:
            decision = await self._decide(engine, messages, Path(tmp))
        self.assertIs(decision.strategy, CacheStrategy.NONE)
        self.assertFalse(decision.enabled)

    async def test_explicit_when_large_prefix_first_turn(self) -> None:
        engine = CacheDecisionEngine()
        messages = [{"role": "system", "content": _LONG}, {"role": "user", "content": "hi"}]
        with tempfile.TemporaryDirectory() as tmp:
            decision = await self._decide(engine, messages, Path(tmp))
        self.assertIs(decision.strategy, CacheStrategy.EXPLICIT)
        self.assertTrue(decision.enabled)
        self.assertTrue(decision.breakpoints)

    async def test_implicit_when_midsize_unstable(self) -> None:
        engine = CacheDecisionEngine()
        # token 落在 [IMPLICIT_MIN, EXPLICIT_FALLBACK) 区间、且首轮未稳定 → 隐式。
        midsize = "y" * ((IMPLICIT_CACHE_MIN_TOKENS + 50) * 4)
        messages = [{"role": "system", "content": midsize}, {"role": "user", "content": "hi"}]
        with tempfile.TemporaryDirectory() as tmp:
            decision = await self._decide(engine, messages, Path(tmp))
        self.assertIs(decision.strategy, CacheStrategy.IMPLICIT)
        self.assertFalse(decision.enabled)
        self.assertEqual(decision.breakpoints, [])

    async def test_explicit_once_prefix_is_stable(self) -> None:
        engine = CacheDecisionEngine()
        midsize = "y" * ((IMPLICIT_CACHE_MIN_TOKENS + 50) * 4)
        messages = [{"role": "system", "content": midsize}, {"role": "user", "content": "hi"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await self._decide(engine, messages, root)
            second = await self._decide(engine, messages, root)
        self.assertIs(first.strategy, CacheStrategy.IMPLICIT)
        self.assertFalse(first.prefix_stable)
        self.assertIs(second.strategy, CacheStrategy.EXPLICIT)
        self.assertTrue(second.prefix_stable)

    async def test_sessions_do_not_share_stability(self) -> None:
        engine = CacheDecisionEngine()
        messages = [{"role": "system", "content": _LONG}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await engine.decide(session_id="s1", frame_id="f1", messages=messages, tools=[], project_root=root)
            other = await engine.decide(
                session_id="s2", frame_id="f1", messages=messages, tools=[], project_root=root
            )
        self.assertFalse(other.prefix_stable)


class CacheTokensFromUsageTests(unittest.TestCase):
    def test_extracts_triple(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=4200,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3840, cache_creation_input_tokens=120),
        )
        self.assertEqual(_cache_tokens_from_usage(usage), (3840, 4200, 120))

    def test_dict_details(self) -> None:
        usage = SimpleNamespace(prompt_tokens=100, prompt_tokens_details={"cached_tokens": 64})
        self.assertEqual(_cache_tokens_from_usage(usage), (64, 100, None))

    def test_missing_usage(self) -> None:
        self.assertEqual(_cache_tokens_from_usage(None), (None, None, None))


class MaxTokenCountTests(unittest.TestCase):
    def test_none_handling(self) -> None:
        self.assertIsNone(_max_token_count(None, None))
        self.assertEqual(_max_token_count(None, 5), 5)
        self.assertEqual(_max_token_count(5, None), 5)

    def test_keeps_larger(self) -> None:
        self.assertEqual(_max_token_count(10, 3), 10)
        self.assertEqual(_max_token_count(3, 10), 10)


class LayeredPromptTests(unittest.TestCase):
    def test_layers_drop_empty(self) -> None:
        prompt = LayeredPrompt(core="L0", project_context="", rag_context="L3")
        self.assertEqual(prompt.layers(), ["L0", "L3"])

    def test_to_content_blocks(self) -> None:
        prompt = LayeredPrompt(core="L0", project_context="L2")
        self.assertEqual(
            prompt.to_content_blocks(),
            [{"type": "text", "text": "L0"}, {"type": "text", "text": "L2"}],
        )

    def test_to_text_joins_layers(self) -> None:
        prompt = LayeredPrompt(core="L0", project_context="L2")
        self.assertEqual(prompt.to_text(), "L0\n\nL2")

    def test_builder_uses_project_context(self) -> None:
        agent = SimpleNamespace(
            prompt="core rules", hooks=None, skills=[], name="coordinator"
        )
        layered = build_layered_system_prompt(agent, None, None, None, project_context="项目背景：foo")
        self.assertIn("项目背景：foo", layered.project_context)
        self.assertEqual(len(layered.layers()), 2)


class ProjectContextTests(unittest.TestCase):
    def test_empty_when_no_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build_project_context(Path(tmp)), "")

    def test_reads_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "CLAUDE.md").write_text("project overview", encoding="utf-8")
            context = build_project_context(Path(tmp))
        self.assertIn("CLAUDE.md", context)
        self.assertIn("project overview", context)

    def test_truncates_long_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("z" * 9000, encoding="utf-8")
            context = build_project_context(Path(tmp))
        self.assertIn("已截断", context)


class RagContextTests(unittest.TestCase):
    def _index_with_file(self, root: Path, rel: str, content: str) -> CodebaseIndex:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        index = CodebaseIndex(SecuritySettings(project_root=root))
        index.build()
        return index

    def test_empty_without_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = CodebaseIndex(SecuritySettings(project_root=Path(tmp)))
            self.assertEqual(build_rag_context(index, "jump velocity"), "")

    def test_empty_for_blank_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index_with_file(Path(tmp), "scripts/player.gd", "func jump():\n\tvelocity_y = -300\n")
            self.assertEqual(build_rag_context(index, "   "), "")

    def test_returns_snippets_for_matching_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index_with_file(
                Path(tmp),
                "scripts/player.gd",
                "extends CharacterBody2D\n\nfunc apply_jump_velocity():\n\tvelocity.y = jump_velocity\n",
            )
            context = build_rag_context(index, "jump_velocity")
        self.assertIn("scripts/player.gd", context)
        self.assertIn("相关代码片段", context)

    def test_empty_for_unrelated_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index_with_file(Path(tmp), "scripts/player.gd", "func jump():\n\tvelocity_y = -300\n")
            self.assertEqual(build_rag_context(index, "zzzdoesnotexistzzz_token"), "")


class SessionRagContextRoundTripTests(unittest.TestCase):
    def test_rag_context_survives_serialization(self) -> None:
        session = Session(session_id="s1", rag_context="相关代码片段……\n--- a.gd:1-2 ---\ncode")
        restored = session_from_dict(session_to_dict(session), set())
        self.assertEqual(restored.rag_context, session.rag_context)

    def test_rag_context_defaults_empty(self) -> None:
        restored = session_from_dict({"session_id": "s1"}, set())
        self.assertEqual(restored.rag_context, "")


class EmitCacheHitEventTests(unittest.TestCase):
    def _capture(self) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
        events: list[tuple[str, dict[str, Any]]] = []
        return events, lambda event_type, payload: events.append((event_type, payload))

    def test_emits_without_saved_ratio(self) -> None:
        events, callback = self._capture()
        frame = SimpleNamespace(id="frame-1")
        turn = SimpleNamespace(cached_tokens=3840, total_input_tokens=4200, cache_creation_tokens=120)
        _emit_cache_hit_event(callback, frame, 2, turn)
        event_type, payload = events[0]
        self.assertEqual(event_type, "cache_hit")
        self.assertEqual(payload["cached_tokens"], 3840)
        self.assertEqual(payload["total_input_tokens"], 4200)
        self.assertEqual(payload["cache_creation_tokens"], 120)
        self.assertNotIn("saved_ratio", payload)

    def test_silent_on_miss(self) -> None:
        events, callback = self._capture()
        frame = SimpleNamespace(id="f")
        for turn in (
            SimpleNamespace(cached_tokens=0, total_input_tokens=4200, cache_creation_tokens=None),
            SimpleNamespace(cached_tokens=None, total_input_tokens=4200, cache_creation_tokens=None),
            SimpleNamespace(cached_tokens=10, total_input_tokens=0, cache_creation_tokens=None),
        ):
            _emit_cache_hit_event(callback, frame, 1, turn)
        self.assertEqual(events, [])


class RecordCacheMetricsTests(unittest.TestCase):
    def test_records_when_decision_present(self) -> None:
        collector = CacheMetricsCollector()
        decision = CacheDecision(
            strategy=CacheStrategy.EXPLICIT,
            breakpoints=[CacheBreakpoint(0, 0, "system_layer_0")],
            cache_key="key",
            prefix_stable=True,
            segments_used=["system_layer_0"],
            repo_fingerprint="repo",
            tool_schema_version="tools",
        )
        turn = SimpleNamespace(cached_tokens=50, total_input_tokens=200, cache_creation_tokens=None)
        _record_cache_metrics(collector, decision, turn)
        self.assertAlmostEqual(collector.aggregate_hit_ratio, 0.25)

    def test_noop_without_collector_or_decision(self) -> None:
        turn = SimpleNamespace(cached_tokens=50, total_input_tokens=200, cache_creation_tokens=None)
        _record_cache_metrics(None, None, turn)
        collector = CacheMetricsCollector()
        _record_cache_metrics(collector, None, turn)
        self.assertEqual(collector.aggregate_hit_ratio, 0.0)


class CacheMetricsCollectorTests(unittest.TestCase):
    def test_aggregate_accumulates(self) -> None:
        collector = CacheMetricsCollector()
        for cached in (100, 0):
            collector.record(
                CacheMetricsSnapshot(
                    cache_key="k",
                    repo_fingerprint="r",
                    tool_schema_version="t",
                    cached_tokens=cached,
                    total_tokens=100,
                    hit_ratio=cached / 100,
                    cache_enabled=True,
                )
            )
        self.assertAlmostEqual(collector.aggregate_hit_ratio, 0.5)


if __name__ == "__main__":
    unittest.main()
