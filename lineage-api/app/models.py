"""Pydantic request / response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, confloat

NodeKind = Literal["source", "derived", "report"]

# Non-negative finite execution cost (NaN / +/-infinity are rejected).
NonNegativeCost = confloat(ge=0, allow_inf_nan=False)


class NodeIn(BaseModel):
    name: str = Field(..., min_length=1, description="全局唯一字段名")
    kind: NodeKind
    # 候选输入组：组内字段全部可用时该组可用；任一组可用即可产出本列。
    # source 节点必须为空或省略。
    candidate_groups: list[list[str]] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    nodes: list[NodeIn] = Field(..., min_length=1)


class RegisterResponse(BaseModel):
    registered: int
    nodes: list[str]
    message: str


class ImpactRequest(BaseModel):
    retiring: list[str] = Field(
        ..., min_length=1, description="拟下线的字段名（任意类型节点）"
    )


class MissingDependency(BaseModel):
    name: str
    kind: NodeKind
    root_causes: list[str] = Field(
        description="导致该输入不可用的最深层退役字段"
    )


class FailedGroup(BaseModel):
    group_index: int
    inputs: list[str]
    missing: list[MissingDependency]


class FailedReportColumn(BaseModel):
    name: str
    failed_groups: list[FailedGroup]


class ImpactResponse(BaseModel):
    retiring: list[str]
    failed_report_columns: list[FailedReportColumn]
    unavailable_count: int
    unavailable_nodes: list[str] = Field(
        description="所有不可用节点（含派生列，便于递归排查）"
    )


class NodeOut(BaseModel):
    name: str
    kind: NodeKind
    candidate_groups: list[list[str]]


class GraphResponse(BaseModel):
    nodes: list[NodeOut]
    counts: dict[str, int]


# --------------------------------------------------------------------------- #
# Recalculation plan
# --------------------------------------------------------------------------- #


class ReplanRequest(BaseModel):
    targets: list[str] = Field(
        ..., min_length=1, description="需要重算的目标报表列（已登记节点名）"
    )
    costs: dict[str, NonNegativeCost] = Field(
        ...,
        description=(
            "相关非源列（derived / report）的执行成本，必须为非负有限数；"
            "源列无需执行，不允许出现在这里。"
        ),
    )


class PlannedColumn(BaseModel):
    name: str
    kind: NodeKind
    group_index: int = Field(description="该列选用的候选输入组下标（从 0 开始）")
    inputs: list[str] = Field(description="所选候选组内的输入字段")
    cost: float


class ReplanResponse(BaseModel):
    targets: list[str]
    total_cost: float
    columns: list[PlannedColumn] = Field(
        description="每个执行列选用的候选组（按列名排序）"
    )
    execution_order: list[str] = Field(
        description="满足依赖的执行顺序：被依赖者先执行"
    )
