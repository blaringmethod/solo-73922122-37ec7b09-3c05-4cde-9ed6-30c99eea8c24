"""复访矩阵自测：登记、缺测/未检出区分、先登记后上报、调换与删除更正、
整组原子性、批次唯一归属、随写联动只更新受影响格。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def obs(obs_id, county="云和县", species="中华秋沙鸭", team="甲队", plot="样地A"):
    return {"obs_id": obs_id, "county": county, "species": species,
            "team": team, "plot": plot}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("DEMO_SEED", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def submit(client, batch_no, observations):
    return client.post("/batches", json={"batch_no": batch_no, "observations": observations})


def register(client, cells, county="云和县"):
    return client.post("/revisits", json={"county": county, "cells": cells})


def matrix(client, plots, rounds, county="云和县", species="中华秋沙鸭"):
    params = [("county", county), ("species", species)]
    params += [("plots", p) for p in plots]
    params += [("rounds", r) for r in rounds]
    return client.get("/revisits/matrix", params=params)


def cell_value(body, plot, round_):
    for c in body["cells"]:
        if c["plot"] == plot and c["round"] == round_:
            return c["value"]
    raise AssertionError(f"格 {plot}/{round_} 不在矩阵响应中")


# ------------------------------------------------------------ 登记与矩阵取值


def test_register_and_matrix_values(client):
    submit(client, "B1", [obs("O1", plot="样地A"), obs("O2", plot="样地B", species="麻雀")])
    r = register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地B", "round": "R1", "batches": ["B1"]},  # 同批覆盖多个样地
    ])
    assert r.status_code == 200, r.text
    assert all(c["state"] == "ready" for c in r.json()["cells"])

    body = matrix(client, ["样地A", "样地B", "样地C"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") == 1   # B1 在样地A 有秋沙鸭
    assert cell_value(body, "样地B", "R1") == 0   # B1 在样地B 只有麻雀
    assert cell_value(body, "样地C", "R1") is None  # 未登记 → 缺测
    reason = {c["plot"]: c["reason"] for c in body["cells"]}
    assert reason["样地C"] == "unregistered"
    # 矩阵行=样地、列=轮次
    assert body["matrix"] == [[1], [0], [None]]


def test_empty_batch_list_means_surveyed_zero(client):
    register(client, [{"plot": "样地A", "round": "R1", "batches": []}])
    body = matrix(client, ["样地A"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") == 0  # 查过但没有观测记录 ≠ 缺测


def test_register_before_submit_then_batch_arrives(client):
    # 先登记后上报：批次未到 → 缺测（pending）
    r = register(client, [{"plot": "样地A", "round": "R1", "batches": ["B9"]}])
    assert r.json()["cells"][0]["state"] == "pending"
    body = matrix(client, ["样地A"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") is None
    assert body["cells"][0]["reason"] == "pending"
    assert body["cells"][0]["pending_batches"] == ["B9"]

    # 上报后同格就绪，且响应只列出受影响的格
    r = submit(client, "B9", [obs("O1", plot="样地A")])
    assert r.json()["revisit_changes"] == [
        {"county": "云和县", "plot": "样地A", "round": "R1",
         "old_state": "pending", "new_state": "ready"}
    ]
    assert cell_value(matrix(client, ["样地A"], ["R1"]).json(), "样地A", "R1") == 1


def test_withdraw_marks_cells_missing(client):
    submit(client, "B1", [obs("O1", plot="样地A")])
    submit(client, "B2", [obs("O2", plot="样地B")])
    register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地B", "round": "R1", "batches": ["B2"]},
    ])
    r = client.post("/batches/B1/withdraw")
    assert r.status_code == 200
    # 只更新受影响的格：样地B 的格不在变更列表里
    assert r.json()["revisit_changes"] == [
        {"county": "云和县", "plot": "样地A", "round": "R1",
         "old_state": "ready", "new_state": "withdrawn"}
    ]
    body = matrix(client, ["样地A", "样地B"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") is None  # 有撤回 → 缺测
    assert cell_value(body, "样地B", "R1") == 1     # 未受影响
    wa = [c for c in body["cells"] if c["plot"] == "样地A"][0]
    assert wa["reason"] == "withdrawn"
    assert wa["withdrawn_batches"] == ["B1"]


def test_cell_with_multiple_batches_requires_all_arrived(client):
    submit(client, "B1", [obs("O1", plot="样地A", species="麻雀")])
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1", "B2"]}])
    body = matrix(client, ["样地A"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") is None  # B2 未到齐 → 缺测
    submit(client, "B2", [obs("O2", plot="样地A")])
    body = matrix(client, ["样地A"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") == 1  # 两批到齐，合并观测判定


# ------------------------------------------------------------ 登记校验与幂等


def test_batch_belongs_to_one_round_per_plot(client):
    # 同一请求内同批号归同一样地两轮 → 422
    r = register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地A", "round": "R2", "batches": ["B1"]},
    ])
    assert r.status_code == 422
    # 跨请求：第二轮登记撞上已登记的同批号 → 409
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    r = register(client, [{"plot": "样地A", "round": "R2", "batches": ["B1"]}])
    assert r.status_code == 409
    # 同批号归不同样地 → 允许
    r = register(client, [{"plot": "样地B", "round": "R2", "batches": ["B1"]}])
    assert r.status_code == 200


def test_reregister_replay_and_conflict(client):
    cells = [{"plot": "样地A", "round": "R1", "batches": ["B1"]}]
    r1 = register(client, cells)
    assert r1.json()["cells"][0]["result"] == "registered"
    r2 = register(client, cells)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["cells"][0]["result"] == "replayed"
    # 同格不同批号清单 → 409，须走更正
    r3 = register(client, [{"plot": "样地A", "round": "R1", "batches": ["B2"]}])
    assert r3.status_code == 409
    # 冲突后原格不变
    cells_now = client.get("/revisits", params={"county": "云和县"}).json()
    assert len(cells_now) == 1
    assert cells_now[0]["batches"] == ["B1"]


def test_register_group_atomic_on_conflict(client):
    # 第二格与已登记状态冲突 → 整组拒收，第一格也不落库
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    r = register(client, [
        {"plot": "样地B", "round": "R1", "batches": ["B2"]},
        {"plot": "样地A", "round": "R1", "batches": ["B9"]},
    ])
    assert r.status_code == 409
    cells_now = client.get("/revisits").json()
    assert {(c["plot"], c["round"]) for c in cells_now} == {("样地A", "R1")}


def test_register_validation_errors(client):
    assert register(client, []).status_code == 422                      # 空清单
    dup = [{"plot": "样地A", "round": "R1", "batches": []}] * 2
    assert register(client, dup).status_code == 422                    # 格重复
    r = register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1", "B1"]}])
    assert r.status_code == 422                                        # 格内批号重复


# ------------------------------------------------------------ 更正：调换与删除


def test_correction_move_batch_to_another_round(client):
    submit(client, "B1", [obs("O1", plot="样地A")])
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    r = client.post("/revisits/corrections", json={
        "county": "云和县",
        "moves": [{"batch_no": "B1", "from_plot": "样地A", "from_round": "R1",
                   "to_plot": "样地A", "to_round": "R2"}],
        "deletes": [],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied_moves"][0]["to"] == {"plot": "样地A", "round": "R2"}
    assert body["created_cells"] == [{"plot": "样地A", "round": "R2"}]  # 目标格自动新建
    # 来源格被腾空（空清单=查过无观测 → 0），目标格就绪 → 1
    m = matrix(client, ["样地A"], ["R1", "R2"]).json()
    assert cell_value(m, "样地A", "R1") == 0
    assert cell_value(m, "样地A", "R2") == 1


def test_correction_swap_two_batches(client):
    submit(client, "B1", [obs("O1", plot="样地A")])
    submit(client, "B2", [obs("O2", plot="样地A", species="麻雀")])
    register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地A", "round": "R2", "batches": ["B2"]},
    ])
    # 两条方向相反的调换 = 对调两格批次，整组生效
    r = client.post("/revisits/corrections", json={
        "county": "云和县",
        "moves": [
            {"batch_no": "B1", "from_plot": "样地A", "from_round": "R1",
             "to_plot": "样地A", "to_round": "R2"},
            {"batch_no": "B2", "from_plot": "样地A", "from_round": "R2",
             "to_plot": "样地A", "to_round": "R1"},
        ],
        "deletes": [],
    })
    assert r.status_code == 200, r.text
    m = matrix(client, ["样地A"], ["R1", "R2"]).json()
    assert cell_value(m, "样地A", "R1") == 0  # 现在挂 B2（只有麻雀）
    assert cell_value(m, "样地A", "R2") == 1  # 现在挂 B1（有秋沙鸭）


def test_correction_delete_cell(client):
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    r = client.post("/revisits/corrections", json={
        "county": "云和县",
        "moves": [],
        "deletes": [{"plot": "样地A", "round": "R1"}],
    })
    assert r.status_code == 200
    assert r.json()["applied_deletes"][0]["removed_batches"] == ["B1"]
    body = matrix(client, ["样地A"], ["R1"]).json()
    assert cell_value(body, "样地A", "R1") is None
    assert body["cells"][0]["reason"] == "unregistered"


def test_correction_group_atomic_on_failure(client):
    submit(client, "B1", [obs("O1", plot="样地A")])
    register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地B", "round": "R1", "batches": []},
    ])
    before = matrix(client, ["样地A", "样地B"], ["R1"]).json()
    # 一条合法删除 + 一条非法调换（批号不在来源格）→ 整组失败
    r = client.post("/revisits/corrections", json={
        "county": "云和县",
        "moves": [{"batch_no": "B9", "from_plot": "样地A", "from_round": "R1",
                   "to_plot": "样地A", "to_round": "R2"}],
        "deletes": [{"plot": "样地B", "round": "R1"}],
    })
    assert r.status_code == 422
    # 删除也未生效，矩阵保持原状态
    after = matrix(client, ["样地A", "样地B"], ["R1"]).json()
    assert after == before
    cells_now = client.get("/revisits").json()
    assert {(c["plot"], c["round"]) for c in cells_now} == {("样地A", "R1"), ("样地B", "R1")}


def test_correction_move_conflict_with_existing_round(client):
    # B1 已归 样地A/R1，把 B1 从 样地B 调往 样地A/R2 → 409，整组回滚
    register(client, [
        {"plot": "样地A", "round": "R1", "batches": ["B1"]},
        {"plot": "样地B", "round": "R1", "batches": ["B1"]},
    ])
    r = client.post("/revisits/corrections", json={
        "county": "云和县",
        "moves": [{"batch_no": "B1", "from_plot": "样地B", "from_round": "R1",
                   "to_plot": "样地A", "to_round": "R2"}],
        "deletes": [],
    })
    assert r.status_code == 409
    cells_now = client.get("/revisits").json()
    assert len(cells_now) == 2  # 目标格未新建


def test_correction_not_found_and_validation(client):
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    # 删除未登记的格 → 404
    r = client.post("/revisits/corrections", json={
        "county": "云和县", "moves": [],
        "deletes": [{"plot": "样地Z", "round": "R1"}]})
    assert r.status_code == 404
    # 来源格未登记 → 404
    r = client.post("/revisits/corrections", json={
        "county": "云和县", "deletes": [],
        "moves": [{"batch_no": "B1", "from_plot": "样地Z", "from_round": "R1",
                   "to_plot": "样地A", "to_round": "R2"}]})
    assert r.status_code == 404
    # 目标与来源相同 → 422
    r = client.post("/revisits/corrections", json={
        "county": "云和县", "deletes": [],
        "moves": [{"batch_no": "B1", "from_plot": "样地A", "from_round": "R1",
                   "to_plot": "样地A", "to_round": "R1"}]})
    assert r.status_code == 422
    # 空更正组 → 422
    r = client.post("/revisits/corrections",
                    json={"county": "云和县", "moves": [], "deletes": []})
    assert r.status_code == 422


def test_correction_move_to_pending_batch_refreshes_state(client):
    # 调换后目标格引用未上报批次 → 目标格缺测；批次到齐后 → 就绪
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    r = client.post("/revisits/corrections", json={
        "county": "云和县", "deletes": [],
        "moves": [{"batch_no": "B1", "from_plot": "样地A", "from_round": "R1",
                   "to_plot": "样地B", "to_round": "R1"}]})
    assert r.status_code == 200
    changes = {(c["plot"], c["new_state"]) for c in r.json()["revisit_changes"]}
    assert changes == {("样地B", "pending"), ("样地A", "ready")}
    submit(client, "B1", [obs("O1", plot="样地B")])
    assert cell_value(matrix(client, ["样地B"], ["R1"]).json(), "样地B", "R1") == 1


# ------------------------------------------------------------ 矩阵查询行为


def test_matrix_is_read_only(client):
    submit(client, "B1", [obs("O1", plot="样地A")])
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    first = matrix(client, ["样地A"], ["R1"]).json()
    matrix(client, ["样地A"], ["R1"])
    matrix(client, ["样地A"], ["R1"], species="麻雀")
    assert matrix(client, ["样地A"], ["R1"]).json() == first  # 查询不改状态
    assert len(client.get("/revisits").json()) == 1


def test_matrix_species_and_county_scoping(client):
    submit(client, "B1", [
        obs("O1", plot="样地A", species="中华秋沙鸭"),
        obs("O2", plot="样地A", species="麻雀", county="景宁县"),
    ])
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    # 同格不同鸟种：秋沙鸭 1，麻雀 0（景宁县的麻雀不计入云和县）
    assert cell_value(matrix(client, ["样地A"], ["R1"]).json(), "样地A", "R1") == 1
    assert cell_value(matrix(client, ["样地A"], ["R1"], species="麻雀").json(),
                      "样地A", "R1") == 0
    # 同批号登记到景宁县的格：该县的麻雀计入
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}], county="景宁县")
    assert cell_value(matrix(client, ["样地A"], ["R1"], county="景宁县",
                             species="麻雀").json(), "样地A", "R1") == 1


def test_matrix_requires_plot_and_round_lists(client):
    r = client.get("/revisits/matrix", params={"county": "云和县", "species": "麻雀"})
    assert r.status_code == 422


def test_registry_flow_unaffected_by_revisits(client):
    # 复访登记不影响名录签发；撤批同时驱动名录复核与复访格更新
    submit(client, "B1", [
        obs("O1", team="甲队", plot="样地A"),
        obs("O2", team="乙队", plot="样地B"),
        obs("O3", team="丙队", plot="样地C"),
    ])
    register(client, [{"plot": "样地A", "round": "R1", "batches": ["B1"]}])
    assert len(client.get("/registry").json()) == 1
    r = client.post("/batches/B1/withdraw")
    assert r.json()["registry_changes"][0]["action"] == "delisted"
    assert r.json()["revisit_changes"][0]["new_state"] == "withdrawn"
    assert client.get("/registry").json() == []
