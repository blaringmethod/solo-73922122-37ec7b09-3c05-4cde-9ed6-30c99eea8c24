"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ObservationIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    obs_id: str = Field(min_length=1, max_length=64, description="观测号")
    county: str = Field(min_length=1, max_length=64, description="县")
    species: str = Field(min_length=1, max_length=64, description="鸟种")
    team: str = Field(min_length=1, max_length=64, description="调查队")
    plot: str = Field(min_length=1, max_length=64, description="样地")


class BatchSubmission(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64, description="批号")
    observations: list[ObservationIn] = Field(min_length=1, max_length=10000)


class RegistryChange(BaseModel):
    county: str
    species: str
    action: str
    evidence_no: str | None = None
    old_evidence_no: str | None = None


class BatchSubmitResponse(BaseModel):
    batch_no: str
    status: Literal["active", "withdrawn"]
    accepted: int
    replayed: bool
    registry_changes: list[dict[str, Any]]
    revisit_changes: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


class BatchWithdrawResponse(BaseModel):
    batch_no: str
    status: Literal["withdrawn"]
    replayed: bool
    registry_changes: list[dict[str, Any]]
    revisit_changes: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


class ObservationOut(BaseModel):
    obs_id: str
    county: str
    species: str
    team: str
    plot: str


class BatchDetail(BaseModel):
    batch_no: str
    status: Literal["active", "withdrawn"]
    received_at: str
    withdrawn_at: str | None
    observations: list[ObservationOut]


class BatchSummary(BaseModel):
    batch_no: str
    status: str
    received_at: str
    withdrawn_at: str | None
    observation_count: int


class RegistryEntry(BaseModel):
    county: str
    species: str
    evidence_no: str
    listed_at: str
    evidence_status: str


class EvidenceObservation(BaseModel):
    batch_no: str
    obs_id: str
    county: str
    species: str
    team: str
    plot: str
    batch_status: str


class EvidenceDetail(BaseModel):
    evidence_no: str
    county: str
    species: str
    status: Literal["valid", "invalid"]
    replaced_by: str | None
    created_at: str
    observations: list[EvidenceObservation]
    all_source_batches_active: bool


# ------------------------------------------------------------ 复访矩阵

CellState = Literal["ready", "pending", "withdrawn"]


class RevisitCellIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    plot: str = Field(min_length=1, max_length=64, description="样地")
    round: str = Field(min_length=1, max_length=64, description="轮次")
    batches: list[str] = Field(
        default_factory=list, max_length=1000,
        description="该次复访的全部观测批号；空清单表示查过但没有观测记录",
    )


class RevisitRegistration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    county: str = Field(min_length=1, max_length=64, description="县")
    cells: list[RevisitCellIn] = Field(min_length=1, max_length=10000)


class RevisitCellResult(BaseModel):
    plot: str
    round: str
    batches: list[str]
    state: CellState
    result: Literal["registered", "replayed"]


class RevisitRegisterResponse(BaseModel):
    county: str
    replayed: bool
    cells: list[RevisitCellResult]
    message: str | None = None


class RevisitCellRef(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    plot: str = Field(min_length=1, max_length=64, description="样地")
    round: str = Field(min_length=1, max_length=64, description="轮次")


class RevisitMove(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    batch_no: str = Field(min_length=1, max_length=64, description="批号")
    from_plot: str = Field(min_length=1, max_length=64, description="来源样地")
    from_round: str = Field(min_length=1, max_length=64, description="来源轮次")
    to_plot: str = Field(min_length=1, max_length=64, description="目标样地")
    to_round: str = Field(min_length=1, max_length=64, description="目标轮次")


class RevisitCorrection(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    county: str = Field(min_length=1, max_length=64, description="县")
    moves: list[RevisitMove] = Field(
        default_factory=list, max_length=10000,
        description="调换批次归属：把批号从来源格移到目标格（目标格不存在则新建）",
    )
    deletes: list[RevisitCellRef] = Field(
        default_factory=list, max_length=10000,
        description="删掉误登格（整格删除）",
    )


class RevisitCorrectionResponse(BaseModel):
    county: str
    applied_moves: list[dict[str, Any]]
    applied_deletes: list[dict[str, Any]]
    created_cells: list[dict[str, Any]]
    revisit_changes: list[dict[str, Any]]
    message: str | None = None


class RevisitCellOut(BaseModel):
    county: str
    plot: str
    round: str
    state: CellState
    batches: list[str]
    registered_at: str
    updated_at: str


class MatrixCell(BaseModel):
    plot: str
    round: str
    value: int | None = Field(description="1=检出，0=未检出，null=缺测")
    reason: Literal["unregistered", "pending", "withdrawn"] | None = None
    batches: list[str] | None = None
    pending_batches: list[str] = Field(default_factory=list)
    withdrawn_batches: list[str] = Field(default_factory=list)


class RevisitMatrix(BaseModel):
    county: str
    species: str
    plots: list[str]
    rounds: list[str]
    matrix: list[list[int | None]] = Field(description="行=样地、列=轮次的检出矩阵")
    cells: list[MatrixCell]
