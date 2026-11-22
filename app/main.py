"""FastAPI 入口：县域鸟类分布名录供数 API。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import logic, models, seed
from .db import Database

DESCRIPTION = """\
县域鸟类分布名录供数 API。

* **POST /batches** 上报一批观测（带批号）；同县同种凑齐三笔有效观测
  （三支不同调查队、三处不同样地）即入册并签发证据号。
* **POST /batches/{batch_no}/withdraw** 撤回整批；只复核该批涉及的县与鸟种组合，
  原证据失效时另找三笔换发新号，无替代则移出名录。
* **GET /registry** 当前名录（县、鸟种、证据号）。
* **GET /evidence/{evidence_no}** 按证据号追溯原三笔的批号、观测号及证据有效性；
  已失效的旧证据号仍可查询。

复访矩阵（供占域分析，区分未检出 0 与缺测 null）：

* **POST /revisits** 一次登记同一县的多个复访格（样地 + 轮次 + 该次复访的全部
  观测批号；空清单表示查过但没有观测记录）。可引用旧批次，也可先登记后上报。
* **POST /revisits/corrections** 一组更正：调换批次归属或删掉误登格，整组生效。
* **GET /revisits** 复访格列表（可按县过滤）。
* **GET /revisits/matrix** 按县、鸟种、样地列表、轮次列表取矩阵：
  未登记 / 批次未到齐 / 有撤回的格为缺测（null），就绪格有该鸟种记 1、否则记 0。
"""


def create_app() -> FastAPI:
    db_path = os.environ.get("DB_PATH", str(Path.cwd() / "registry.db"))
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if os.environ.get("DEMO_SEED") == "1":
            seeded = seed.seed_if_empty(db)
            if seeded:
                print(
                    f"[demo] 已写入演示批次: {', '.join(seeded['batches'])}；"
                    f"复访格 {seeded['revisit_cells']} 个"
                )
        yield
        db.close()

    app = FastAPI(
        title="县域鸟类分布名录供数 API",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db = db

    @app.exception_handler(logic.NotFoundError)
    async def not_found_handler(_: Request, exc: logic.NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(logic.ConflictError)
    async def conflict_handler(_: Request, exc: logic.ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(logic.ValidationError)
    async def validation_handler(_: Request, exc: logic.ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # ------------------------------------------------------------ 写接口

    @app.post(
        "/batches",
        response_model=models.BatchSubmitResponse,
        summary="上报一批观测清单",
    )
    def submit_batch(submission: models.BatchSubmission) -> dict:
        """接收带批号的观测清单。同批号原样重报幂等不改状态，异内容重报返回 409。"""
        return logic.submit_batch(
            db,
            submission.batch_no,
            [o.model_dump() for o in submission.observations],
        )

    @app.post(
        "/batches/{batch_no}/withdraw",
        response_model=models.BatchWithdrawResponse,
        summary="撤回整批观测",
    )
    def withdraw_batch(batch_no: str) -> dict:
        """撤回整批并复核受影响的（县, 鸟种）组合。重复撤回幂等不改状态。"""
        return logic.withdraw_batch(db, batch_no)

    # ------------------------------------------------------------ 供数接口

    @app.get(
        "/registry",
        response_model=list[models.RegistryEntry],
        summary="当前名录（县 × 鸟种 × 证据号）",
    )
    def get_registry() -> list[dict]:
        return logic.get_registry(db)

    @app.get(
        "/evidence/{evidence_no}",
        response_model=models.EvidenceDetail,
        summary="按证据号追溯原三笔观测",
    )
    def get_evidence(evidence_no: str) -> dict:
        """返回该证据对应三笔观测的批号、观测号，以及这组证据是否有效。"""
        return logic.get_evidence(db, evidence_no)

    @app.get(
        "/batches",
        response_model=list[models.BatchSummary],
        summary="批次列表",
    )
    def list_batches() -> list[dict]:
        return logic.list_batches(db)

    @app.get(
        "/batches/{batch_no}",
        response_model=models.BatchDetail,
        summary="批次明细",
    )
    def get_batch(batch_no: str) -> dict:
        return logic.get_batch(db, batch_no)

    # ------------------------------------------------------------ 复访矩阵

    @app.post(
        "/revisits",
        response_model=models.RevisitRegisterResponse,
        summary="一次登记同一县的多个复访格",
    )
    def register_revisits(registration: models.RevisitRegistration) -> dict:
        """每格 = 样地 + 轮次 + 该次复访的全部观测批号。空清单表示查过但没有
        观测记录。同一批次可覆盖多个样地，但同县同一样地只归一轮。登记可引用
        旧批次，也可先登记后上报（批次到齐前该格为缺测）。整组校验、整组生效。"""
        return logic.register_revisits(
            db,
            registration.county,
            [c.model_dump() for c in registration.cells],
        )

    @app.post(
        "/revisits/corrections",
        response_model=models.RevisitCorrectionResponse,
        summary="一组更正：调换批次归属 / 删掉误登格",
    )
    def correct_revisits(correction: models.RevisitCorrection) -> dict:
        """调换把批号从来源格移到目标格（目标格不存在则新建；两条方向相反的
        调换即对调两格批次）；删除整格移除误登。整组校验、整组生效，任一失败
        全部回滚。"""
        return logic.correct_revisits(
            db,
            correction.county,
            [m.model_dump() for m in correction.moves],
            [d.model_dump() for d in correction.deletes],
        )

    @app.get(
        "/revisits",
        response_model=list[models.RevisitCellOut],
        summary="复访格列表（可按县过滤）",
    )
    def list_revisits(county: str | None = None) -> list[dict]:
        return logic.list_revisit_cells(db, county)

    @app.get(
        "/revisits/matrix",
        response_model=models.RevisitMatrix,
        summary="复访矩阵：县 × 鸟种 × 样地列表 × 轮次列表",
    )
    def get_revisit_matrix(
        county: str,
        species: str,
        plots: list[str] = Query(min_length=1, description="样地列表（可重复传参）"),
        rounds: list[str] = Query(min_length=1, description="轮次列表（可重复传参）"),
    ) -> dict:
        """只读汇总。每格：1=检出，0=未检出（含空清单格），null=缺测
        （未登记 / 批次未到齐 / 有撤回）。各格来自同一已提交状态。"""
        return logic.get_revisit_matrix(db, county, species, plots, rounds)

    return app


app = create_app()
