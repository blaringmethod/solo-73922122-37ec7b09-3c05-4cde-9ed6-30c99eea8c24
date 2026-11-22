# 县域鸟类分布名录供数 API

FastAPI + SQLite 实现的县域鸟类分布名录服务：接收带批号的观测清单，满足
“同县同种三笔有效观测、三支不同调查队、三处不同样地”即入册并签发证据号；
下游可按证据号追溯原三笔的批号、观测号及证据有效性。

另提供**复访矩阵 API**：供占域分析取数，严格区分**未检出（0）**与**缺测（null）**。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务启动后打开 <http://localhost:8000/docs>（Swagger UI）。
首次启动且库为空时自动写入演示数据（`DEMO_SEED=1`，见 `app/seed.py`）：

| 内容 | 结果 |
|---|---|
| 批次 `LY-2026-041`：云和县·中华秋沙鸭 3 笔（三队三样地）；景宁畲族自治县·黄腹角雉 3 笔 | 入册 `EV-000001`、`EV-000002` |
| 批次 `LY-2026-042`：云和县·中华秋沙鸭 备选 3 笔；云和县·白颈长尾雉 3 笔（两笔同样地） | 秋沙鸭保留原证据号；白颈长尾雉不入册 |
| 复访登记（云和县 5 格）：浮云溪/规溪/长汀 第1轮 ← `LY-2026-041`；浮云溪 第2轮 ← `LY-2026-042`；长汀 第2轮 ← `LY-2026-043` | 前 4 格就绪；长汀第2轮的批次未上报 → 缺测（先登记后上报） |

### 在 /docs 演示复访矩阵、调换批次与撤批

1. **取矩阵**：`GET /revisits/matrix`，参数
   `county=云和县`、`species=中华秋沙鸭`、
   `plots=浮云溪样地&plots=规溪样地&plots=长汀样地`、`rounds=第1轮&rounds=第2轮`，
   得到 `matrix: [[1,0],[1,null],[1,null]]`——同页可见检出 1、未检出 0、
   缺测 null（规溪第2轮 `unregistered` 未登记；长汀第2轮 `pending` 批次未到齐）。
2. **调换批次**：`POST /revisits/corrections`，把 `LY-2026-042` 从
   浮云溪样地/第2轮 调到 规溪样地/第2轮：

   ```json
   {"county": "云和县",
    "moves": [{"batch_no": "LY-2026-042", "from_plot": "浮云溪样地", "from_round": "第2轮",
               "to_plot": "规溪样地", "to_round": "第2轮"}],
    "deletes": []}
   ```

   目标格自动新建（`created_cells`），源格腾空（空清单=查过但没有观测记录 → 0）。
   再取矩阵变为 `[[1,0],[1,0],[1,null]]`。两条方向相反的 move 即对调两格批次。
3. **撤批**：`POST /batches/LY-2026-041/withdraw`，一次事务里同时完成：
   - 名录：云和县·中华秋沙鸭 `replaced`（`EV-000001`→`EV-000003`）；
     景宁畲族自治县·黄腹角雉 `delisted`；
   - 复访格：引用该批的第1轮三格 `ready → withdrawn`（响应 `revisit_changes`
     只列受影响的格）。再取矩阵，第1轮整列变缺测。
4. **追溯**：`GET /evidence/EV-000001` 仍可查，`status=invalid`，
   `replaced_by=EV-000003`。
5. **幂等**：重复撤批、同批号原样重报、同内容重复登记均为无操作；
   同批号异内容重报、同格异清单登记返回 **409**。

等价的 curl 命令：

```bash
curl -G http://localhost:8000/revisits/matrix \
  --data-urlencode "county=云和县" --data-urlencode "species=中华秋沙鸭" \
  --data-urlencode "plots=浮云溪样地" --data-urlencode "plots=规溪样地" \
  --data-urlencode "rounds=第1轮" --data-urlencode "rounds=第2轮"

curl -X POST http://localhost:8000/revisits/corrections \
  -H 'Content-Type: application/json' \
  -d '{"county":"云和县","moves":[{"batch_no":"LY-2026-042","from_plot":"浮云溪样地","from_round":"第2轮","to_plot":"规溪样地","to_round":"第2轮"}],"deletes":[]}'

curl -X POST http://localhost:8000/batches/LY-2026-041/withdraw
curl http://localhost:8000/registry
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 上报一批观测（批号 + 观测清单：观测号/县/鸟种/调查队/样地） |
| POST | `/batches/{batch_no}/withdraw` | 撤回整批，复核受影响的（县, 鸟种）组合与复访格 |
| GET | `/registry` | 当前名录：县 × 鸟种 × 证据号 |
| GET | `/evidence/{evidence_no}` | 证据追溯：原三笔的批号、观测号、有效性 |
| GET | `/batches` / `/batches/{batch_no}` | 批次列表 / 明细 |
| POST | `/revisits` | 一次登记同一县的多个复访格（样地+轮次+批号清单） |
| POST | `/revisits/corrections` | 一组更正：调换批次归属 / 删掉误登格，整组生效 |
| GET | `/revisits` | 复访格列表（可按县过滤），含就绪状态与批号清单 |
| GET | `/revisits/matrix` | 复访矩阵：县 × 鸟种 × 样地列表 × 轮次列表 |

## 复访矩阵规则

- **复访格** =（县, 样地, 轮次），登记该次复访的**全部观测批号**；
  空清单表示查过但没有观测记录（是有效的 0，不是缺测）。
- **批次归属**：同一批次可覆盖多个样地（出现在多格），但同县同一样地
  只归一轮（数据库主键直接约束）；登记可引用旧批次，也可先登记后上报。
- **矩阵取值**（按县、鸟种、样地列表、轮次列表查询）：
  - `null`（缺测）：格未登记（`unregistered`）、所列批次未到齐（`pending`）、
    或有批次已撤回（`withdrawn`）；响应附未到齐/已撤回批号清单；
  - `1`：就绪格所列批次在该县该样地有该鸟种观测；
  - `0`：就绪格无该鸟种观测（含空清单格）。
- **就绪状态物化**：每格的 `ready/pending/withdrawn` 存在库里，上报、撤批、
  登记、更正时在同一事务内**只重算受影响的格**（写接口响应带
  `revisit_changes`）；矩阵查询只读汇总，整个查询在写锁内完成，
  各格来自同一已提交状态。
- **更正**：`moves` 调换批次归属（目标格不存在则新建；同样地内换轮次、
  跨样地挪动、两格对调均可），`deletes` 整格删除误登。整组先校验后落库，
  任一失败全部回滚、保留原状态。

## 名录规则实现要点

- **入册**：同县同种在有效批次中选出三笔，调查队两两不同且样地两两不同
  （按上报时间确定性贪心选取，见 `app/logic.py:_find_triple`）。
- **复核范围**：批次上报/撤回只重算该批涉及的（县, 鸟种）组合。
- **保留原号**：复核时原三笔仍全部有效 → 保留原证据号，不换发。
- **失效换发**：原证据任一来源批次被撤回 → 旧证据号标 `invalid`；
  找到替代三笔则签发新号（`replaced_by` 串联新旧号），找不到则移出名录。
  旧证据号永久可查。
- **幂等**：批次内容做规范化 SHA-256 哈希。同批号同内容重报、重复撤回、
  同内容重复登记均为无操作；同批号异内容重报、同格异清单登记返回 409。
- **事务一致**：所有写操作在进程内写锁 + `BEGIN IMMEDIATE` 事务中完成，
  批次、观测、名录、证据、复访格状态同事务提交；任一步失败整体回滚，
  保留原状态。

## 本地开发与测试

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 36 个用例
.venv/bin/uvicorn app.main:app --reload   # DEMO_SEED=1 可带演示数据
```

## 已知限制

- 单进程单写者（SQLite + 进程内锁）；多副本水平扩展需换服务端数据库
  （如 PostgreSQL），事务与幂等逻辑可直接平移。
- 证据号在单库内单调递增，不回收。
- 复访格的轮次为自由文本（如 `第1轮`），矩阵按查询给定的轮次列表顺序出列。
- 未做认证鉴权，内网供数场景假设。
