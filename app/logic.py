"""名录登记核心逻辑：批次接收/撤回、按（县, 鸟种）组合复核、证据签发与失效。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database

EVIDENCE_COUNTER = "evidence"


class NotFoundError(Exception):
    """资源不存在。"""


class ConflictError(Exception):
    """与已有状态冲突（如批号相同但内容不同）。"""


class ValidationError(Exception):
    """领域校验失败（如批内观测号重复）。"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def payload_hash(batch_no: str, observations: list[dict[str, Any]]) -> str:
    """批次内容的规范化哈希，用于识别“原样重报”与“异内容重报”。"""
    canon = json.dumps(
        {
            "batch_no": batch_no,
            "observations": sorted(observations, key=lambda o: o["obs_id"]),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 内部工具


def _next_evidence_no(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, 0) ON CONFLICT (name) DO NOTHING",
        (EVIDENCE_COUNTER,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (EVIDENCE_COUNTER,))
    value = conn.execute(
        "SELECT value FROM counters WHERE name = ?", (EVIDENCE_COUNTER,)
    ).fetchone()["value"]
    return f"EV-{value:06d}"


def _find_triple(
    conn: sqlite3.Connection, county: str, species: str
) -> list[sqlite3.Row] | None:
    """从有效批次中确定性选出三笔：调查队两两不同、样地两两不同。"""
    rows = conn.execute(
        """
        SELECT o.batch_no, o.obs_id, o.team, o.plot
        FROM observations o
        JOIN batches b ON b.batch_no = o.batch_no
        WHERE b.status = 'active' AND o.county = ? AND o.species = ?
        ORDER BY b.received_at, o.batch_no, o.obs_id
        """,
        (county, species),
    ).fetchall()
    used_teams: set[str] = set()
    used_plots: set[str] = set()
    picked: list[sqlite3.Row] = []
    for row in rows:
        if row["team"] in used_teams or row["plot"] in used_plots:
            continue
        picked.append(row)
        used_teams.add(row["team"])
        used_plots.add(row["plot"])
        if len(picked) == 3:
            return picked
    return None


def _issue_evidence(
    conn: sqlite3.Connection, county: str, species: str, triple: list[sqlite3.Row]
) -> str:
    evidence_no = _next_evidence_no(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO evidence (evidence_no, county, species, status, created_at)"
        " VALUES (?, ?, ?, 'valid', ?)",
        (evidence_no, county, species, now),
    )
    conn.executemany(
        "INSERT INTO evidence_obs (evidence_no, batch_no, obs_id) VALUES (?, ?, ?)",
        [(evidence_no, t["batch_no"], t["obs_id"]) for t in triple],
    )
    return evidence_no


def _revalidate_combo(
    conn: sqlite3.Connection, county: str, species: str
) -> list[dict[str, Any]]:
    """复核单个（县, 鸟种）组合。只在批次变动波及的组合上调用。"""
    changes: list[dict[str, Any]] = []
    base = {"county": county, "species": species}
    reg = conn.execute(
        "SELECT evidence_no FROM registry WHERE county = ? AND species = ?",
        (county, species),
    ).fetchone()

    if reg is not None:
        old_no = reg["evidence_no"]
        sources = conn.execute(
            """
            SELECT b.status AS batch_status
            FROM evidence_obs eo
            JOIN batches b ON b.batch_no = eo.batch_no
            WHERE eo.evidence_no = ?
            """,
            (old_no,),
        ).fetchall()
        if all(s["batch_status"] == "active" for s in sources):
            # 原三笔都有效 → 保留原证据号
            return [{**base, "action": "kept", "evidence_no": old_no}]

        conn.execute("UPDATE evidence SET status = 'invalid' WHERE evidence_no = ?", (old_no,))
        triple = _find_triple(conn, county, species)
        if triple is not None:
            new_no = _issue_evidence(conn, county, species, triple)
            conn.execute(
                "UPDATE registry SET evidence_no = ?, listed_at = ?"
                " WHERE county = ? AND species = ?",
                (new_no, utcnow(), county, species),
            )
            conn.execute(
                "UPDATE evidence SET replaced_by = ? WHERE evidence_no = ?",
                (new_no, old_no),
            )
            changes.append(
                {**base, "action": "replaced", "old_evidence_no": old_no, "evidence_no": new_no}
            )
        else:
            conn.execute(
                "DELETE FROM registry WHERE county = ? AND species = ?", (county, species)
            )
            changes.append({**base, "action": "delisted", "old_evidence_no": old_no})
        return changes

    triple = _find_triple(conn, county, species)
    if triple is not None:
        evidence_no = _issue_evidence(conn, county, species, triple)
        conn.execute(
            "INSERT INTO registry (county, species, evidence_no, listed_at) VALUES (?, ?, ?, ?)",
            (county, species, evidence_no, utcnow()),
        )
        changes.append({**base, "action": "listed", "evidence_no": evidence_no})
    else:
        changes.append({**base, "action": "none"})
    return changes


# ---------------------------------------------------------------- 写操作


def submit_batch(
    db: Database, batch_no: str, observations: list[dict[str, Any]]
) -> dict[str, Any]:
    if not observations:
        raise ValidationError("观测清单不能为空")
    obs_ids = [o["obs_id"] for o in observations]
    if len(set(obs_ids)) != len(obs_ids):
        raise ValidationError("同一批次内观测号重复")

    digest = payload_hash(batch_no, observations)
    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT payload_hash, status FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] == digest:
                    # 同批号原样重报：不改状态（已撤回的保持撤回）
                    conn.execute("COMMIT")
                    return {
                        "batch_no": batch_no,
                        "status": existing["status"],
                        "accepted": len(observations),
                        "replayed": True,
                        "registry_changes": [],
                        "revisit_changes": [],
                        "message": "同批号同内容重报，状态不变",
                    }
                raise ConflictError(f"批号 {batch_no} 已入账且内容不同，拒收")

            conn.execute(
                "INSERT INTO batches (batch_no, payload_hash, status, received_at)"
                " VALUES (?, ?, 'active', ?)",
                (batch_no, digest, utcnow()),
            )
            conn.executemany(
                "INSERT INTO observations (batch_no, obs_id, county, species, team, plot)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (batch_no, o["obs_id"], o["county"], o["species"], o["team"], o["plot"])
                    for o in observations
                ],
            )
            combos = sorted({(o["county"], o["species"]) for o in observations})
            changes: list[dict[str, Any]] = []
            for county, species in combos:
                changes.extend(_revalidate_combo(conn, county, species))
            # 只重算引用了本批次的复访格
            revisit_changes = _refresh_revisit_cells(
                conn, _cells_referencing_batch(conn, batch_no)
            )
            conn.execute("COMMIT")
            return {
                "batch_no": batch_no,
                "status": "active",
                "accepted": len(observations),
                "replayed": False,
                "registry_changes": changes,
                "revisit_changes": revisit_changes,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def withdraw_batch(db: Database, batch_no: str) -> dict[str, Any]:
    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"批号 {batch_no} 不存在")
            if row["status"] == "withdrawn":
                # 重复撤回：不改状态
                conn.execute("COMMIT")
                return {
                    "batch_no": batch_no,
                    "status": "withdrawn",
                    "replayed": True,
                    "registry_changes": [],
                    "revisit_changes": [],
                    "message": "该批次此前已撤回，状态不变",
                }

            conn.execute(
                "UPDATE batches SET status = 'withdrawn', withdrawn_at = ? WHERE batch_no = ?",
                (utcnow(), batch_no),
            )
            combos = conn.execute(
                "SELECT DISTINCT county, species FROM observations WHERE batch_no = ?"
                " ORDER BY county, species",
                (batch_no,),
            ).fetchall()
            changes: list[dict[str, Any]] = []
            for combo in combos:
                changes.extend(_revalidate_combo(conn, combo["county"], combo["species"]))
            # 只重算引用了本批次的复访格
            revisit_changes = _refresh_revisit_cells(
                conn, _cells_referencing_batch(conn, batch_no)
            )
            conn.execute("COMMIT")
            return {
                "batch_no": batch_no,
                "status": "withdrawn",
                "replayed": False,
                "registry_changes": changes,
                "revisit_changes": revisit_changes,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- 查询


def get_registry(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT r.county, r.species, r.evidence_no, r.listed_at, e.status AS evidence_status
        FROM registry r
        JOIN evidence e ON e.evidence_no = r.evidence_no
        ORDER BY r.county, r.species
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_evidence(db: Database, evidence_no: str) -> dict[str, Any]:
    ev = db.conn.execute(
        "SELECT * FROM evidence WHERE evidence_no = ?", (evidence_no,)
    ).fetchone()
    if ev is None:
        raise NotFoundError(f"证据号 {evidence_no} 不存在")
    obs = db.conn.execute(
        """
        SELECT eo.batch_no, eo.obs_id, o.county, o.species, o.team, o.plot,
               b.status AS batch_status
        FROM evidence_obs eo
        JOIN observations o ON o.batch_no = eo.batch_no AND o.obs_id = eo.obs_id
        JOIN batches b ON b.batch_no = eo.batch_no
        WHERE eo.evidence_no = ?
        ORDER BY eo.batch_no, eo.obs_id
        """,
        (evidence_no,),
    ).fetchall()
    return {
        "evidence_no": ev["evidence_no"],
        "county": ev["county"],
        "species": ev["species"],
        "status": ev["status"],
        "replaced_by": ev["replaced_by"],
        "created_at": ev["created_at"],
        "observations": [dict(o) for o in obs],
        "all_source_batches_active": all(o["batch_status"] == "active" for o in obs),
    }


def get_batch(db: Database, batch_no: str) -> dict[str, Any]:
    batch = db.conn.execute(
        "SELECT batch_no, status, received_at, withdrawn_at FROM batches WHERE batch_no = ?",
        (batch_no,),
    ).fetchone()
    if batch is None:
        raise NotFoundError(f"批号 {batch_no} 不存在")
    obs = db.conn.execute(
        "SELECT obs_id, county, species, team, plot FROM observations"
        " WHERE batch_no = ? ORDER BY obs_id",
        (batch_no,),
    ).fetchall()
    return {**dict(batch), "observations": [dict(o) for o in obs]}


def list_batches(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT b.batch_no, b.status, b.received_at, b.withdrawn_at,
               COUNT(o.obs_id) AS observation_count
        FROM batches b
        LEFT JOIN observations o ON o.batch_no = b.batch_no
        GROUP BY b.batch_no
        ORDER BY b.received_at, b.batch_no
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 复访矩阵
#
# 复访格 = (县, 样地, 轮次)，登记该次复访的全部观测批号。格的就绪状态
# （ready / pending / withdrawn）物化在 revisit_cells.state 上，只在上报、
# 撤批、登记、更正时按受影响范围重算；矩阵查询只读汇总，检出 0/1 按所列
# 批次在该县该样地的观测现算。

CellKey = tuple[str, str, str]  # (county, plot, round)


def _cell_batches(conn: sqlite3.Connection, county: str, plot: str, round_: str) -> list[str]:
    rows = conn.execute(
        "SELECT batch_no FROM revisit_cell_batches"
        " WHERE county = ? AND plot = ? AND round = ? ORDER BY batch_no",
        (county, plot, round_),
    ).fetchall()
    return [r["batch_no"] for r in rows]


def _batch_status_map(conn: sqlite3.Connection, batch_nos: list[str]) -> dict[str, str]:
    if not batch_nos:
        return {}
    placeholders = ",".join("?" for _ in batch_nos)
    rows = conn.execute(
        f"SELECT batch_no, status FROM batches WHERE batch_no IN ({placeholders})",
        batch_nos,
    ).fetchall()
    return {r["batch_no"]: r["status"] for r in rows}


def _state_for_batch_list(conn: sqlite3.Connection, batch_nos: list[str]) -> str:
    """空清单 = 查过但没有观测记录 → ready；有批次撤回 → withdrawn；
    有批次未上报 → pending；全部到齐且有效 → ready。"""
    if not batch_nos:
        return "ready"
    by_no = _batch_status_map(conn, batch_nos)
    if any(by_no.get(b) == "withdrawn" for b in batch_nos):
        return "withdrawn"
    if any(b not in by_no for b in batch_nos):
        return "pending"
    return "ready"


def _compute_cell_state(conn: sqlite3.Connection, county: str, plot: str, round_: str) -> str:
    return _state_for_batch_list(conn, _cell_batches(conn, county, plot, round_))


def _cells_referencing_batch(conn: sqlite3.Connection, batch_no: str) -> list[CellKey]:
    rows = conn.execute(
        "SELECT county, plot, round FROM revisit_cell_batches WHERE batch_no = ?",
        (batch_no,),
    ).fetchall()
    return [(r["county"], r["plot"], r["round"]) for r in rows]


def _refresh_revisit_cells(
    conn: sqlite3.Connection, keys: list[CellKey] | set[CellKey]
) -> list[dict[str, Any]]:
    """重算指定复访格的就绪状态（与调用方同事务）。只动传入的格，
    返回状态发生变化的格；已被删除的格自动跳过。"""
    changes: list[dict[str, Any]] = []
    for county, plot, round_ in sorted(set(keys)):
        row = conn.execute(
            "SELECT state FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
            (county, plot, round_),
        ).fetchone()
        if row is None:
            continue
        new_state = _compute_cell_state(conn, county, plot, round_)
        if new_state != row["state"]:
            conn.execute(
                "UPDATE revisit_cells SET state = ?, updated_at = ?"
                " WHERE county = ? AND plot = ? AND round = ?",
                (new_state, utcnow(), county, plot, round_),
            )
            changes.append(
                {
                    "county": county,
                    "plot": plot,
                    "round": round_,
                    "old_state": row["state"],
                    "new_state": new_state,
                }
            )
    return changes


def register_revisits(
    db: Database, county: str, cells: list[dict[str, Any]]
) -> dict[str, Any]:
    """一次登记同一县的多个复访格。整组校验、整组生效：
    任一格冲突则整组拒收。已登记且内容一致的格幂等跳过，
    内容不同的须走更正接口。登记可引用尚未上报的批号（先登记后上报）。"""
    if not cells:
        raise ValidationError("复访格清单不能为空")
    seen: set[tuple[str, str]] = set()
    round_of_plot_batch: dict[tuple[str, str], str] = {}
    for c in cells:
        key = (c["plot"], c["round"])
        if key in seen:
            raise ValidationError(f"同一登记内复访格重复：{c['plot']} / {c['round']}")
        seen.add(key)
        if len(set(c["batches"])) != len(c["batches"]):
            raise ValidationError(f"复访格 {c['plot']} / {c['round']} 内批号重复")
        for b in c["batches"]:
            owner = round_of_plot_batch.setdefault((c["plot"], b), c["round"])
            if owner != c["round"]:
                raise ValidationError(
                    f"批号 {b} 在同县同一样地 {c['plot']} 只归一轮"
                    f"（本次同时登记到 {owner} 和 {c['round']}）"
                )

    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 先整组校验，再落库
            for c in cells:
                existing = conn.execute(
                    "SELECT 1 FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, c["plot"], c["round"]),
                ).fetchone()
                if existing is not None:
                    current = _cell_batches(conn, county, c["plot"], c["round"])
                    if sorted(current) != sorted(c["batches"]):
                        raise ConflictError(
                            f"复访格 {c['plot']} / {c['round']} 已登记且批号清单不同，"
                            "请用更正接口调换批次或删除"
                        )
                for b in c["batches"]:
                    row = conn.execute(
                        "SELECT round FROM revisit_cell_batches"
                        " WHERE county = ? AND plot = ? AND batch_no = ?",
                        (county, c["plot"], b),
                    ).fetchone()
                    if row is not None and row["round"] != c["round"]:
                        raise ConflictError(
                            f"批号 {b} 已登记在 {c['plot']} 的 {row['round']}，"
                            "同县同一样地只归一轮"
                        )
            now = utcnow()
            results: list[dict[str, Any]] = []
            for c in cells:
                existing = conn.execute(
                    "SELECT state FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, c["plot"], c["round"]),
                ).fetchone()
                if existing is not None:
                    results.append(
                        {
                            "plot": c["plot"],
                            "round": c["round"],
                            "batches": _cell_batches(conn, county, c["plot"], c["round"]),
                            "state": existing["state"],
                            "result": "replayed",
                        }
                    )
                    continue
                state = _state_for_batch_list(conn, c["batches"])
                conn.execute(
                    "INSERT INTO revisit_cells (county, plot, round, state,"
                    " registered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (county, c["plot"], c["round"], state, now, now),
                )
                conn.executemany(
                    "INSERT INTO revisit_cell_batches (county, plot, round, batch_no)"
                    " VALUES (?, ?, ?, ?)",
                    [(county, c["plot"], c["round"], b) for b in c["batches"]],
                )
                results.append(
                    {
                        "plot": c["plot"],
                        "round": c["round"],
                        "batches": sorted(c["batches"]),
                        "state": state,
                        "result": "registered",
                    }
                )
            conn.execute("COMMIT")
            replayed = all(r["result"] == "replayed" for r in results)
            return {
                "county": county,
                "replayed": replayed,
                "cells": results,
                "message": "全部复访格此前已登记且内容一致，状态不变" if replayed else None,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def correct_revisits(
    db: Database,
    county: str,
    moves: list[dict[str, Any]],
    deletes: list[dict[str, Any]],
) -> dict[str, Any]:
    """一组更正：调换批次归属（move）或删掉误登格（delete），整组生效。
    任一条不合法则整组回滚，保留原状态。调换目标格不存在时新建。"""
    if not moves and not deletes:
        raise ValidationError("更正组不能为空：至少给一条调换或删除")
    deleted_keys: set[tuple[str, str]] = set()
    for d in deletes:
        key = (d["plot"], d["round"])
        if key in deleted_keys:
            raise ValidationError(f"更正组内重复删除同一复访格：{d['plot']} / {d['round']}")
        deleted_keys.add(key)
    for m in moves:
        if (m["from_plot"], m["from_round"]) == (m["to_plot"], m["to_round"]):
            raise ValidationError(f"批号 {m['batch_no']} 的调换目标与来源相同")
        if (m["from_plot"], m["from_round"]) in deleted_keys:
            raise ValidationError(
                f"调换来源 {m['from_plot']} / {m['from_round']} 同时被本组删除，含义矛盾"
            )
        if (m["to_plot"], m["to_round"]) in deleted_keys:
            raise ValidationError(
                f"调换目标 {m['to_plot']} / {m['to_round']} 同时被本组删除，含义矛盾"
            )

    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 先整组校验，再落库
            for d in deletes:
                row = conn.execute(
                    "SELECT 1 FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, d["plot"], d["round"]),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"复访格 {d['plot']} / {d['round']} 未登记")
            for m in moves:
                src = conn.execute(
                    "SELECT 1 FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, m["from_plot"], m["from_round"]),
                ).fetchone()
                if src is None:
                    raise NotFoundError(
                        f"来源复访格 {m['from_plot']} / {m['from_round']} 未登记"
                    )
                if m["batch_no"] not in _cell_batches(
                    conn, county, m["from_plot"], m["from_round"]
                ):
                    raise ValidationError(
                        f"批号 {m['batch_no']} 不在来源格 {m['from_plot']} / {m['from_round']}"
                    )
                row = conn.execute(
                    "SELECT round FROM revisit_cell_batches"
                    " WHERE county = ? AND plot = ? AND batch_no = ?",
                    (county, m["to_plot"], m["batch_no"]),
                ).fetchone()
                if row is not None:
                    if (m["to_plot"], row["round"]) == (m["from_plot"], m["from_round"]):
                        pass  # 同样地内调换轮次，源行随后删除
                    elif row["round"] == m["to_round"]:
                        raise ValidationError(
                            f"批号 {m['batch_no']} 已在目标格 {m['to_plot']} / {m['to_round']}"
                        )
                    else:
                        raise ConflictError(
                            f"批号 {m['batch_no']} 已归 {m['to_plot']} 的 {row['round']}，"
                            "同县同一样地只归一轮"
                        )

            now = utcnow()
            applied_deletes: list[dict[str, Any]] = []
            for d in deletes:
                removed = _cell_batches(conn, county, d["plot"], d["round"])
                conn.execute(
                    "DELETE FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, d["plot"], d["round"]),
                )
                applied_deletes.append(
                    {"plot": d["plot"], "round": d["round"], "removed_batches": removed}
                )

            applied_moves: list[dict[str, Any]] = []
            created_cells: list[dict[str, Any]] = []
            touched: set[CellKey] = set()
            for m in moves:
                conn.execute(
                    "DELETE FROM revisit_cell_batches"
                    " WHERE county = ? AND plot = ? AND round = ? AND batch_no = ?",
                    (county, m["from_plot"], m["from_round"], m["batch_no"]),
                )
                conn.execute(
                    "UPDATE revisit_cells SET updated_at = ?"
                    " WHERE county = ? AND plot = ? AND round = ?",
                    (now, county, m["from_plot"], m["from_round"]),
                )
                target = conn.execute(
                    "SELECT 1 FROM revisit_cells WHERE county = ? AND plot = ? AND round = ?",
                    (county, m["to_plot"], m["to_round"]),
                ).fetchone()
                if target is None:
                    conn.execute(
                        "INSERT INTO revisit_cells (county, plot, round, state,"
                        " registered_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?)",
                        (county, m["to_plot"], m["to_round"], now, now),
                    )
                    created_cells.append({"plot": m["to_plot"], "round": m["to_round"]})
                else:
                    conn.execute(
                        "UPDATE revisit_cells SET updated_at = ?"
                        " WHERE county = ? AND plot = ? AND round = ?",
                        (now, county, m["to_plot"], m["to_round"]),
                    )
                conn.execute(
                    "INSERT INTO revisit_cell_batches (county, plot, round, batch_no)"
                    " VALUES (?, ?, ?, ?)",
                    (county, m["to_plot"], m["to_round"], m["batch_no"]),
                )
                applied_moves.append(
                    {
                        "batch_no": m["batch_no"],
                        "from": {"plot": m["from_plot"], "round": m["from_round"]},
                        "to": {"plot": m["to_plot"], "round": m["to_round"]},
                    }
                )
                touched.add((county, m["from_plot"], m["from_round"]))
                touched.add((county, m["to_plot"], m["to_round"]))

            # 只重算本组触及的格（被删的格自动跳过）
            revisit_changes = _refresh_revisit_cells(conn, touched)
            conn.execute("COMMIT")
            return {
                "county": county,
                "applied_moves": applied_moves,
                "applied_deletes": applied_deletes,
                "created_cells": created_cells,
                "revisit_changes": revisit_changes,
            }
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise ConflictError(f"更正违反归属约束，整组未生效：{exc}") from exc
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def list_revisit_cells(db: Database, county: str | None = None) -> list[dict[str, Any]]:
    with db.lock:
        conn = db.conn
        sql = (
            "SELECT county, plot, round, state, registered_at, updated_at"
            " FROM revisit_cells"
        )
        args: tuple = ()
        if county:
            sql += " WHERE county = ?"
            args = (county,)
        sql += " ORDER BY county, plot, round"
        rows = conn.execute(sql, args).fetchall()
        return [
            {
                **dict(r),
                "batches": _cell_batches(conn, r["county"], r["plot"], r["round"]),
            }
            for r in rows
        ]


def get_revisit_matrix(
    db: Database, county: str, species: str, plots: list[str], rounds: list[str]
) -> dict[str, Any]:
    """按县、鸟种、样地列表、轮次列表取复访矩阵。

    每格取值：未登记 / 所列批次未到齐 / 有撤回 → 缺测（null）；
    就绪格按所列批次在该县该样地的观测，有该鸟种记 1，否则记 0。
    整个查询在写锁内一次完成，各格来自同一已提交状态。"""
    plots = list(dict.fromkeys(plots))
    rounds = list(dict.fromkeys(rounds))
    with db.lock:
        conn = db.conn
        cells_out: list[dict[str, Any]] = []
        for plot in plots:
            for round_ in rounds:
                cell = conn.execute(
                    "SELECT state FROM revisit_cells"
                    " WHERE county = ? AND plot = ? AND round = ?",
                    (county, plot, round_),
                ).fetchone()
                if cell is None:
                    cells_out.append(
                        {
                            "plot": plot,
                            "round": round_,
                            "value": None,
                            "reason": "unregistered",
                            "batches": None,
                            "pending_batches": [],
                            "withdrawn_batches": [],
                        }
                    )
                    continue
                batches = _cell_batches(conn, county, plot, round_)
                if cell["state"] != "ready":
                    by_no = _batch_status_map(conn, batches)
                    cells_out.append(
                        {
                            "plot": plot,
                            "round": round_,
                            "value": None,
                            "reason": cell["state"],
                            "batches": batches,
                            "pending_batches": [b for b in batches if b not in by_no],
                            "withdrawn_batches": [
                                b for b in batches if by_no.get(b) == "withdrawn"
                            ],
                        }
                    )
                    continue
                value = 0
                if batches:
                    placeholders = ",".join("?" for _ in batches)
                    hit = conn.execute(
                        "SELECT 1 FROM observations o"
                        " JOIN batches b ON b.batch_no = o.batch_no"
                        f" WHERE o.batch_no IN ({placeholders})"
                        " AND b.status = 'active'"
                        " AND o.county = ? AND o.plot = ? AND o.species = ? LIMIT 1",
                        (*batches, county, plot, species),
                    ).fetchone()
                    value = 1 if hit is not None else 0
                cells_out.append(
                    {
                        "plot": plot,
                        "round": round_,
                        "value": value,
                        "reason": None,
                        "batches": batches,
                        "pending_batches": [],
                        "withdrawn_batches": [],
                    }
                )
        matrix = [
            [cells_out[i * len(rounds) + j]["value"] for j in range(len(rounds))]
            for i in range(len(plots))
        ]
        return {
            "county": county,
            "species": species,
            "plots": plots,
            "rounds": rounds,
            "matrix": matrix,
            "cells": cells_out,
        }
