from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import AppSettings
from app.llm.cache_manager import compute_rag_fingerprint
from app.prompt.context_builder import ContextBuilder, ContextLayer
from app.rag.build_manager import RagIndexBuildManager
from app.rag.embedding_client import EmbeddingClient, EmbeddingConfig
from app.rag.engine.asset_index import AssetIndex, classify_asset
from app.rag.engine.scene_graph_index import SceneGraphIndex
from app.rag.engine.signal_graph_index import SignalGraphIndex
from app.rag.graph_fusion import GraphFusion
from app.rag.graph_reranker import GraphAwareReranker
from app.rag.hybrid import HybridRetriever
from app.rag.index import CodebaseIndex
from app.rag.models import SearchResult
from app.rag.query_router import QueryRouter, QueryType
from app.rag.symbol_index import SymbolIndex
from app.security.settings import SecuritySettings


class EarsTests(unittest.TestCase):
    def test_router_modalities(self) -> None:
        router = QueryRouter()
        self.assertEqual(router.route("Camera follow node").type, QueryType.SCENE)
        self.assertEqual(router.route("signal connect 到哪里").type, QueryType.SIGNAL)
        self.assertEqual(router.route("enemy texture 在哪").type, QueryType.ASSET)
        self.assertEqual(router.route("movement behavior implementation").type, QueryType.CODE)

    def test_symbol_scene_signal_and_asset_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text(
                "signal health_changed\nconst MAX_HEALTH = 100\nfunc jump():\n    pass\n",
                encoding="utf-8",
            )
            (root / "enemy.png").write_bytes(b"png")
            (root / "player.tscn").write_text(
                '[gd_scene load_steps=3 format=3]\n'
                '[ext_resource type="Script" path="res://player.gd" id="1"]\n'
                '[ext_resource type="Texture2D" path="res://enemy.png" id="2"]\n'
                '[node name="Player" type="CharacterBody2D"]\nscript = ExtResource("1")\n'
                '[node name="Camera2D" type="Camera2D" parent="."]\n'
                '[connection signal="health_changed" from="Player" to="Camera2D" method="_on_health_changed"]\n',
                encoding="utf-8",
            )
            symbol = SymbolIndex(root / "symbols.json")
            symbol.build(root, [root / "player.gd"])
            self.assertEqual(symbol.search("jump")[0].graph_meta["symbol_type"], "function")
            self.assertIn(symbol.symbols[0]["backend"], {"tree-sitter", "regex"})

            scene = SceneGraphIndex(root / "scene.json")
            scene.build(root, [root / "player.tscn"])
            self.assertTrue(scene.find_node("Camera2D"))
            self.assertTrue(scene.trace_path("Player/Camera2D"))
            self.assertTrue(scene.find_script_nodes("player.gd"))

            signal = SignalGraphIndex(root / "signal.json")
            signal.build(root, [root / "player.gd", root / "player.tscn"])
            self.assertTrue(signal.find_signal("health_changed"))
            self.assertTrue(signal.find_emitters("_on_health_changed"))

            asset = AssetIndex(root / "asset.json")
            asset.build(root)
            self.assertEqual(classify_asset("enemy.png"), "image")
            self.assertIn("player.tscn", asset.assets["enemy.png"]["used_by"])

    def test_incremental_and_fingerprint_query_awareness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "player.gd"
            file.write_text("func jump():\n    pass\n", encoding="utf-8")
            index = CodebaseIndex(SecuritySettings(project_root=root))
            first = index.build(incremental=True)
            second = index.build(incremental=True)
            self.assertEqual(first["changed_files"], 1)
            self.assertEqual(second["changed_files"], 0)
            self.assertNotEqual(
                compute_rag_fingerprint(index.path, "jump"),
                compute_rag_fingerprint(index.path, "shoot"),
            )

    def test_embedding_and_graph_expansion(self) -> None:
        def encoder(texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), 1.0] for text in texts]
        client = EmbeddingClient(EmbeddingConfig(), encoder=encoder)
        self.assertEqual(len(client.embed(["a", "bb"])), 2)
        seed = SearchResult("a", "seed", "kw", 1.0, "a.gd")
        from app.rag.models import GraphEdge, GraphNode

        graph = GraphFusion(
            {"a": GraphNode("a", "script", "a"), "b": GraphNode("b", "asset", "b", "b.png", "texture")},
            [GraphEdge("a", "b", "asset_ref")],
        )
        expanded = graph.expand([seed])
        self.assertEqual(expanded[1].graph_meta["graph_distance"], 1)

    def test_four_context_layers(self) -> None:
        with self.assertLogs("app.prompt.context_builder", level="DEBUG") as captured:
            context = ContextBuilder().build(
                stable_prefix="stable", structure_context="structure",
                dynamic_context="dynamic", query="private query",
            )
        sections = context.sections()
        self.assertEqual([section.layer for section in sections], list(ContextLayer))
        self.assertEqual([section.cacheable for section in sections], [True, True, False, False])
        output = "\n".join(captured.output)
        self.assertIn("query_length=13", output)
        self.assertNotIn("private query", output)

    def test_graph_aware_reranker_and_metrics(self) -> None:
        far = SearchResult(
            "far", "far", "kw", 0.9, "far.gd", graph_meta={"graph_distance": 4}
        )
        near = SearchResult(
            "near", "near", "kw", 0.8, "near.gd", graph_meta={"graph_distance": 1}
        )
        reranker = GraphAwareReranker(alpha=0.5, beta=0.5, top_k=2)
        ranked = reranker.rerank("query", [far, near])
        self.assertEqual(ranked[0].id, "near")

        retriever = HybridRetriever(
            {"kw": lambda _query, _limit: [far, near]},
            reranker=reranker,
            final_limit=2,
        )
        with self.assertLogs("app.rag", level="DEBUG") as captured:
            retriever.search("private implementation query")
        self.assertEqual(retriever.last_metrics["counts"], {"kw": 2})
        self.assertIn("rerank_ms", retriever.last_metrics)
        output = "\n".join(captured.output)
        self.assertIn("RAG query routed", output)
        self.assertIn("Graph-aware rerank complete", output)
        self.assertIn("EARS retrieval", output)
        self.assertNotIn("private implementation query", output)

    def test_graph_traversal_smoke_performance(self) -> None:
        from app.rag.models import GraphEdge, GraphNode

        nodes = {str(index): GraphNode(str(index), "scene_node", str(index)) for index in range(100)}
        edges = [GraphEdge(str(index), str(index + 1), "child") for index in range(99)]
        graph = GraphFusion(nodes, edges, max_depth=2, max_neighbors=5)
        started = time.perf_counter()
        graph.expand([SearchResult("0", "root", "scene_graph", 1.0, "scene.tscn")])
        self.assertLess((time.perf_counter() - started) * 1000, 20)

    def test_tree_sitter_failure_falls_back_to_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fallback.gd"
            source.write_text("func fallback_symbol():\n    pass\n", encoding="utf-8")
            with patch("app.rag.symbol_index._tree_sitter_language", return_value=None):
                symbols = SymbolIndex(root / "symbols.json")
                symbols.build(root, [source])
            self.assertEqual(symbols.symbols[0]["name"], "fallback_symbol")
            self.assertEqual(symbols.symbols[0]["backend"], "regex")

    def test_signal_graph_incremental_skips_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            emitter = root / "player.gd"
            emitter.write_text("signal health_changed\n", encoding="utf-8")
            receiver = root / "hud.gd"
            receiver.write_text(
                "func _ready():\n    player.health_changed.connect(self._on_health_changed)\n",
                encoding="utf-8",
            )
            index = SignalGraphIndex(root / "signal.json")
            first = index.build(root, [emitter, receiver], incremental=True)
            self.assertEqual(first, 2)
            self.assertEqual(index.file_signals["player.gd"][0]["name"], "health_changed")
            # Bare "self.method" connect() targets must resolve to the method, not the "self" qualifier.
            self.assertEqual(index.file_signals["hud.gd"][0]["connections"][0]["method"], "_on_health_changed")

            second_index = SignalGraphIndex(root / "signal.json")
            with patch.object(SignalGraphIndex, "_parse_file") as parse_mock:
                second_index.build(root, [emitter, receiver], incremental=True)
                parse_mock.assert_not_called()

            receiver.write_text(
                "func _ready():\n    player.health_changed.connect(self._on_changed_v2)\n",
                encoding="utf-8",
            )
            third_index = SignalGraphIndex(root / "signal.json")
            with patch.object(SignalGraphIndex, "_parse_file", wraps=third_index._parse_file) as parse_mock:
                third_index.build(root, [emitter, receiver], incremental=True)
                parse_mock.assert_called_once()

    def test_rag_pipeline_end_to_end_performance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(20):
                (root / f"script_{i}.gd").write_text(
                    f"func behavior_{i}():\n    pass\n", encoding="utf-8"
                )
            index = CodebaseIndex(SecuritySettings(project_root=root))
            index.build(incremental=True)
            started = time.perf_counter()
            index.hybrid_search("behavior implementation", max_results=4)
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.assertLess(elapsed_ms, 500)

    def test_embedding_search_latency(self) -> None:
        from app.rag.embedding_index import EmbeddingIndex

        def encoder(texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), 1.0, 0.5] for text in texts]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.json"
            client = EmbeddingClient(EmbeddingConfig(), encoder=encoder)
            chunks = [
                {"path": f"f{i}.gd", "start_line": 1, "end_line": 2, "snippet": f"snippet {i}"}
                for i in range(50)
            ]
            index = EmbeddingIndex(path, client)
            index.build(chunks)
            started = time.perf_counter()
            index.search("snippet", limit=10)
            self.assertLess((time.perf_counter() - started) * 1000, 50)

    def test_scene_graph_parse_performance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ['[gd_scene load_steps=2 format=3]', '[node name="Root" type="Node2D"]']
            for i in range(200):
                lines.append(f'[node name="Child{i}" type="Node2D" parent="Root"]')
            scene_file = root / "large.tscn"
            scene_file.write_text("\n".join(lines), encoding="utf-8")
            index = SceneGraphIndex(root / "scene.json")
            started = time.perf_counter()
            index.build(root, [scene_file], incremental=False)
            self.assertLess((time.perf_counter() - started) * 1000, 200)

    def test_index_lifecycle_logs_are_structured_and_query_safe(self) -> None:
        from app.rag.embedding_index import EmbeddingIndex

        def encoder(texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), 1.0] for text in texts]

        with tempfile.TemporaryDirectory() as tmp:
            index = EmbeddingIndex(
                Path(tmp) / "embeddings.json",
                EmbeddingClient(EmbeddingConfig(), encoder=encoder),
            )
            chunks = [
                {
                    "path": "player.gd",
                    "start_line": 1,
                    "end_line": 2,
                    "snippet": "secret_query_text",
                }
            ]
            with self.assertLogs("app.rag.embedding_index", level="DEBUG") as captured:
                index.build(chunks)
                index.search("secret_query_text")
            output = "\n".join(captured.output)
            self.assertIn("Embedding index build complete", output)
            self.assertIn("Embedding search complete", output)
            self.assertIn("query_length=17", output)
            self.assertNotIn("secret_query_text", output)

    def test_frontend_exposes_all_rag_settings(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        migrations = (repo / "ai_agent_frontend/addons/ai_agent/config/config_migrations.gd").read_text(encoding="utf-8")
        manager = (repo / "ai_agent_frontend/addons/ai_agent/service/service_manager.gd").read_text(encoding="utf-8")
        fields = (
            "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_ENDPOINT",
            "EMBEDDING_API_KEY", "EMBEDDING_TIMEOUT_S", "EMBEDDING_RETRIES",
            "RERANK_MODEL", "RERANK_TIMEOUT_S", "RAG_QUERY_ROUTER_ENABLED",
            "RAG_TOKEN_BUDGET", "GRAPH_MAX_DEPTH", "GRAPH_MAX_NEIGHBORS",
            "ASSET_UNDERSTANDING_ENABLED", "ASSET_UNDERSTANDING_MODEL",
            "ASSET_UNDERSTANDING_ENDPOINT", "ASSET_UNDERSTANDING_API_KEY",
            "ASSET_UNDERSTANDING_TIMEOUT_S", "ASSET_UNDERSTANDING_MAX_TOKENS",
        )
        for field in fields:
            self.assertIn("AI_AGENT_" + field, manager)
            self.assertIn("ai_agent/" + field.lower(), migrations)

    def test_frontend_routes_slash_commands_to_command_api(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        panel = (
            repo / "ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd"
        ).read_text(encoding="utf-8")
        manager = (
            repo / "ai_agent_frontend/addons/ai_agent/service/service_manager.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("func _try_run_slash_command", panel)
        self.assertIn("_http_client.run_command(command_name, args)", panel)
        self.assertIn("命令参数必须是 JSON 对象", panel)
        self.assertIn('"AI_AGENT_RAG_AUTO_BUILD_ENABLED"', manager)

    def test_build_manager_creates_all_configured_indexes(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            (root / "player.tscn").write_text(
                '[node name="Player" type="Node2D"]\n', encoding="utf-8"
            )
            (root / "sprite.png").write_bytes(b"png")
            settings = AppSettings(
                project_root=root,
                asset_understanding_enabled=True,
                asset_understanding_model="",
                asset_understanding_endpoint="",
            )
            manager = RagIndexBuildManager(settings, SecuritySettings(project_root=root))
            result = asyncio.run(manager.build(reason="test"))
            self.assertEqual(result["trigger"], "test")
            for name in (
                "rag_index.json",
                "rag_embeddings.json",
                "rag_symbols.json",
                "scene_graph.json",
                "signal_graph.json",
                "asset_index.json",
            ):
                self.assertTrue((root / ".ai_agent_service" / name).exists(), name)

    def test_build_manager_watches_create_modify_and_delete(self) -> None:
        import asyncio
        import json

        async def scenario(root: Path) -> None:
            source = root / "player.gd"
            source.write_text("func jump():\n    pass\n", encoding="utf-8")
            settings = AppSettings(project_root=root)
            manager = RagIndexBuildManager(settings, SecuritySettings(project_root=root))
            await manager.build(reason="test_initial")

            watcher = asyncio.create_task(manager.watch(poll_interval_s=0.1, debounce_s=0.05))
            try:
                await asyncio.sleep(0.15)
                source.write_text("func dash():\n    pass\n", encoding="utf-8")

                async def wait_for_text(needle: str, *, present: bool) -> None:
                    index_path = root / ".ai_agent_service" / "rag_index.json"
                    for _ in range(40):
                        payload = json.loads(index_path.read_text(encoding="utf-8"))
                        snippets = "\n".join(str(item.get("snippet", "")) for item in payload["chunks"])
                        if (needle in snippets) is present:
                            return
                        await asyncio.sleep(0.05)
                    self.fail(f"index did not reach expected state for {needle!r}")

                await wait_for_text("dash", present=True)
                source.unlink()
                await wait_for_text("dash", present=False)
            finally:
                watcher.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await watcher

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
