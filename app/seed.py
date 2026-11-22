"""演示数据：DEMO_SEED=1 且数据库为空时写入，便于在 /docs 直接演示取数、
复访矩阵、调换批次与撤批。

演示剧情：
- LY-2026-041：云和县·中华秋沙鸭 三笔（三队三样地）→ 入册 EV-000001；
  景宁畲族自治县·黄腹角雉 三笔 → 入册 EV-000002。
- LY-2026-042：云和县·中华秋沙鸭 备选三笔（原证据仍有效，保留 EV-000001）；
  云和县·白颈长尾雉 三笔但两笔同样地 → 不满足条件，不入册。
- 复访登记（云和县）：浮云溪/规溪/长汀 第1轮各挂 LY-2026-041；浮云溪第2轮
  挂 LY-2026-042；长汀第2轮挂尚未上报的 LY-2026-043（先登记后上报 → 缺测）。
- 在 /docs 演示：
  1. GET /revisits/matrix 取云和县·中华秋沙鸭矩阵（1 / 0 / 缺测同页可见）；
  2. POST /revisits/corrections 把 LY-2026-042 从浮云溪第2轮调换到规溪第2轮；
  3. POST /batches/LY-2026-041/withdraw 撤批：名录换发/移出，同时
     第1轮三个复访格变为缺测（revisit_changes 可见）。
"""

from __future__ import annotations

from .db import Database
from . import logic

DEMO_BATCHES: list[dict] = [
    {
        "batch_no": "LY-2026-041",
        "observations": [
            {"obs_id": "OBS-001", "county": "云和县", "species": "中华秋沙鸭",
             "team": "丽水调查一队", "plot": "浮云溪样地"},
            {"obs_id": "OBS-002", "county": "云和县", "species": "中华秋沙鸭",
             "team": "云和调查队", "plot": "规溪样地"},
            {"obs_id": "OBS-003", "county": "云和县", "species": "中华秋沙鸭",
             "team": "高校联合队", "plot": "长汀样地"},
            {"obs_id": "OBS-004", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "丽水调查一队", "plot": "望东垟样地"},
            {"obs_id": "OBS-005", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "景宁调查队", "plot": "上山头样地"},
            {"obs_id": "OBS-006", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "高校联合队", "plot": "大漈样地"},
        ],
    },
    {
        "batch_no": "LY-2026-042",
        "observations": [
            {"obs_id": "OBS-101", "county": "云和县", "species": "中华秋沙鸭",
             "team": "野生动物保护协会", "plot": "梅源样地"},
            {"obs_id": "OBS-102", "county": "云和县", "species": "中华秋沙鸭",
             "team": "丽水调查二队", "plot": "石塘样地"},
            {"obs_id": "OBS-103", "county": "云和县", "species": "中华秋沙鸭",
             "team": "志愿者观测队", "plot": "崇头样地"},
            {"obs_id": "OBS-104", "county": "云和县", "species": "白颈长尾雉",
             "team": "丽水调查一队", "plot": "雾溪样地"},
            {"obs_id": "OBS-105", "county": "云和县", "species": "白颈长尾雉",
             "team": "云和调查队", "plot": "雾溪样地"},
            {"obs_id": "OBS-106", "county": "云和县", "species": "白颈长尾雉",
             "team": "高校联合队", "plot": "沙铺样地"},
        ],
    },
]

DEMO_REVISITS: dict = {
    "county": "云和县",
    "cells": [
        {"plot": "浮云溪样地", "round": "第1轮", "batches": ["LY-2026-041"]},
        {"plot": "浮云溪样地", "round": "第2轮", "batches": ["LY-2026-042"]},
        {"plot": "规溪样地", "round": "第1轮", "batches": ["LY-2026-041"]},
        {"plot": "长汀样地", "round": "第1轮", "batches": ["LY-2026-041"]},
        # 先登记后上报：LY-2026-043 尚未上报，该格为缺测（pending）
        {"plot": "长汀样地", "round": "第2轮", "batches": ["LY-2026-043"]},
    ],
}


def seed_if_empty(db: Database) -> dict:
    """数据库为空时写入演示批次与复访登记，返回写入摘要。"""
    with db.lock:
        count = db.conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
        if count:
            return {}
    seeded_batches: list[str] = []
    for batch in DEMO_BATCHES:
        logic.submit_batch(db, batch["batch_no"], batch["observations"])
        seeded_batches.append(batch["batch_no"])
    result = logic.register_revisits(db, DEMO_REVISITS["county"], DEMO_REVISITS["cells"])
    return {"batches": seeded_batches, "revisit_cells": len(result["cells"])}
