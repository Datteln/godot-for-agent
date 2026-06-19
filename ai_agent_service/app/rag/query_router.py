"""查询意图识别与检索通道路由。"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class QueryType(str, Enum):
    CODE = "CODE"
    SCENE = "SCENE"
    SIGNAL = "SIGNAL"
    ASSET = "ASSET"
    GENERAL = "GENERAL"


@dataclass(frozen=True)
class QueryRoute:
    type: QueryType
    intent: str
    confidence: float
    channels: frozenset[str]

    def to_dict(self) -> dict[str, object]:
        return {"type": self.type.value, "intent": self.intent, "confidence": self.confidence, "channels": sorted(self.channels)}


class QueryRouter:
    def __init__(
        self,
        enabled: bool = True,
        classifier: Callable[[str], tuple[QueryType, float]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.classifier = classifier or _classify_lightweight

    def route(self, query: str) -> QueryRoute:
        text = query.lower()
        if not self.enabled:
            return self._logged_route(
                query,
                QueryRoute(QueryType.GENERAL, "broad_search", 1.0, frozenset({"kw", "vec", "sym", "scene_graph", "signal_graph", "asset"})),
                "disabled",
            )
        rules = [
            (QueryType.SIGNAL, ("signal", "信号", "connect", "emit"), "trace_signal", {"kw", "sym", "signal_graph"}),
            (QueryType.ASSET, ("texture", "sprite", "audio", "sound", "贴图", "纹理", "音效", "资源"), "trace_asset_usage", {"asset", "scene_graph"}),
            (QueryType.SCENE, ("scene", "node", "camera", "场景", "节点", "挂载"), "trace_node_dependency", {"kw", "sym", "scene_graph"}),
            (QueryType.CODE, ("function", "class", "logic", "代码", "函数", "逻辑", ".gd", ".cs"), "find_code", {"kw", "vec", "sym"}),
        ]
        for kind, keywords, intent, channels in rules:
            if any(word in text for word in keywords):
                return self._logged_route(
                    query, QueryRoute(kind, intent, 0.9, frozenset(channels)), "rule"
                )
        kind, confidence = self.classifier(query)
        model_routes = {
            QueryType.CODE: ("find_code", {"kw", "vec", "sym"}),
            QueryType.SCENE: ("trace_node_dependency", {"kw", "sym", "scene_graph"}),
            QueryType.SIGNAL: ("trace_signal", {"kw", "sym", "signal_graph"}),
            QueryType.ASSET: ("trace_asset_usage", {"asset", "scene_graph"}),
        }
        if kind in model_routes and confidence >= 0.35:
            intent, channels = model_routes[kind]
            return self._logged_route(
                query, QueryRoute(kind, intent, confidence, frozenset(channels)), "classifier"
            )
        return self._logged_route(
            query,
            QueryRoute(QueryType.GENERAL, "broad_search", 0.5, frozenset({"kw", "vec", "sym", "scene_graph", "signal_graph", "asset"})),
            "fallback",
        )

    @staticmethod
    def _logged_route(query: str, route: QueryRoute, source: str) -> QueryRoute:
        logger.debug(
            "RAG query routed query_length=%d type=%s intent=%s confidence=%.3f "
            "channels=%s source=%s",
            len(query),
            route.type.value,
            route.intent,
            route.confidence,
            sorted(route.channels),
            source,
        )
        return route


_PROTOTYPES = {
    QueryType.CODE: "implementation behavior algorithm method variable script jump movement save login 代码 实现 行为 算法 方法 变量 脚本",
    QueryType.SCENE: "hierarchy parent child mounted camera player node scene tree 层级 父节点 子节点 挂载 场景树",
    QueryType.SIGNAL: "event callback emitter receiver notification connection 事件 回调 发射 接收 连接",
    QueryType.ASSET: "image sprite texture sound music animation resource used visual 图片 贴图 音频 音乐 动画 资源 使用",
}


def _features(text: str) -> Counter[str]:
    normalized = text.lower()
    words = re.findall(r"[a-z_]+|[\u4e00-\u9fff]", normalized)
    features: Counter[str] = Counter(words)
    compact = re.sub(r"\s+", "", normalized)
    features.update(f"#{compact[index:index + 2]}" for index in range(max(0, len(compact) - 1)))
    return features


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    norm = math.sqrt(sum(value * value for value in left.values()) * sum(value * value for value in right.values()))
    return dot / norm if norm else 0.0


def _classify_lightweight(query: str) -> tuple[QueryType, float]:
    """无网络、无模型下载的 hashed n-gram 原型分类器。"""
    query_features = _features(query)
    scores = {kind: _cosine(query_features, _features(prototype)) for kind, prototype in _PROTOTYPES.items()}
    kind, score = max(scores.items(), key=lambda item: (item[1], item[0].value))
    return (kind, min(0.8, score * 2.5)) if score > 0 else (QueryType.GENERAL, 0.0)
