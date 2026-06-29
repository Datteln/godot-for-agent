"""RAG 子系统状态与后续向量索引接入点。"""

from __future__ import annotations

import os

# 本地 HF 模型（bge-m3 / bge-reranker-v2-m3）一旦缓存好，huggingface_hub 每次加载仍会抢
# 缓存目录的 filelock 去做联网 revision 校验——多 worker/多线程并发时表现为日志里成片的
# "Lock not acquired ... waiting" 甚至 "Timeout on acquiring lock"。离线模式直接跳过这步
# 联网校验和对应的下载锁，模型按本地快照读取即可。用 setdefault：首次需要联网下载模型时，
# 运维显式设 HF_HUB_OFFLINE=0 即可覆盖。这里是包 __init__，先于任何 app.rag.* 子模块里的
# 惰性 sentence_transformers / huggingface_hub 导入执行，因此能保证设置在导入前生效。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
