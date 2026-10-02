"""领域模型：瓦片拓扑、候选版本、区域代际、补丁、路线快照。

设计要点：
- 瓦片内容不可变，(tile_id, version) 唯一标识一份瓦片数据；
- 区域代际 (region, generation) 单调递增，发布产物不可变；
- 车辆路线快照固定 {region: generation} 与瓦片版本集合，
  任何更新只能整体切换到新快照，禁止新旧拓扑拼接。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------- 瓦片拓扑

@dataclass
class Node:
    node_id: str
    x: float
    y: float

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Node":
        return Node(**d)


@dataclass
class Edge:
    """有向车道边。双向道路建模为两条反向边。"""
    edge_id: str
    src: str
    dst: str
    lanes: int = 1
    width_m: float = 3.5
    speed_limit_kph: float = 50.0
    kind: str = "road"                # road / highway / ramp
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Edge":
        return Edge(**d)


@dataclass
class BoundaryPort:
    """边界端口：瓦片与相邻瓦片的拓扑连接点（拓扑边界）。"""
    port_key: str          # 配对提示键，如 "main_st:E"
    x: float
    y: float
    node_id: str           # 瓦片内部连接节点
    out_lanes: int         # 离开本瓦片方向的车道数
    in_lanes: int          # 进入本瓦片方向的车道数
    width_m: float = 3.5
    speed_limit_kph: float = 50.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BoundaryPort":
        return BoundaryPort(**d)


@dataclass
class TileData:
    """一份不可变的瓦片数据：拓扑 + 车道规则 + 拓扑边界。"""
    tile_id: str
    region_id: str
    version: int
    bbox: tuple                          # (minx, miny, maxx, maxy)
    nodes: list = field(default_factory=list)        # list[Node]
    edges: list = field(default_factory=list)        # list[Edge]
    ports: list = field(default_factory=list)        # list[BoundaryPort]
    rules: dict = field(default_factory=dict)        # 车道规则 {edge_id: {...}}

    def to_dict(self) -> dict:
        return {
            "tile_id": self.tile_id,
            "region_id": self.region_id,
            "version": self.version,
            "bbox": list(self.bbox),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "ports": [p.to_dict() for p in self.ports],
            "rules": self.rules,
        }

    @staticmethod
    def from_dict(d: dict) -> "TileData":
        return TileData(
            tile_id=d["tile_id"],
            region_id=d["region_id"],
            version=int(d["version"]),
            bbox=tuple(d["bbox"]),
            nodes=[Node.from_dict(n) for n in d.get("nodes", [])],
            edges=[Edge.from_dict(e) for e in d.get("edges", [])],
            ports=[BoundaryPort.from_dict(p) for p in d.get("ports", [])],
            rules=d.get("rules", {}),
        )

    def node_map(self) -> dict:
        return {n.node_id: n for n in self.nodes}

    def edge_map(self) -> dict:
        return {e.edge_id: e for e in self.edges}


# ---------------------------------------------------------------- 候选版本

class CandidateStatus:
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


@dataclass
class Candidate:
    """候选版本：一组瓦片编辑 + 生效区域，待验证与发布。"""
    candidate_id: str
    name: str
    edits: list                          # list[TileData]
    effective_regions: list              # 生效区域（编辑涉及的区域集合）
    status: str = CandidateStatus.DRAFT
    validation: Optional[dict] = None    # 验证报告
    error: Optional[str] = None
    created_at: float = field(default_factory=now)
    published_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "edits": [e.to_dict() for e in self.edits],
            "effective_regions": list(self.effective_regions),
            "status": self.status,
            "validation": self.validation,
            "error": self.error,
            "created_at": self.created_at,
            "published_at": self.published_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        return Candidate(
            candidate_id=d["candidate_id"],
            name=d["name"],
            edits=[TileData.from_dict(e) for e in d["edits"]],
            effective_regions=list(d["effective_regions"]),
            status=d["status"],
            validation=d.get("validation"),
            error=d.get("error"),
            created_at=d["created_at"],
            published_at=d.get("published_at"),
        )


# ---------------------------------------------------------------- 区域代际

@dataclass
class GenerationRecord:
    """一次发布的产物：某区域某代际对应的完整瓦片版本集合（不可变）。"""
    region_id: str
    generation: int
    candidate_id: str
    tile_versions: dict                  # tile_id -> version
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "GenerationRecord":
        return GenerationRecord(**d)


# ---------------------------------------------------------------- 紧急补丁

class PatchStatus:
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


# 只允许"限制类"操作：补丁不得新增/改变拓扑，保证与基线兼容、可安全过渡
RESTRICTIVE_OPS = {"CLOSE_EDGE", "CLOSE_LANE", "SPEED_CAP"}


@dataclass
class PatchOp:
    kind: str                  # CLOSE_EDGE / CLOSE_LANE / SPEED_CAP
    tile_id: str
    edge_id: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PatchOp":
        return PatchOp(**d)


@dataclass
class Patch:
    """高优先级补丁（如紧急封路），附着于基线代际，可撤销。"""
    patch_id: str
    title: str
    priority: int                        # 数值越大优先级越高
    base: dict                           # 基线 {region_id: generation}
    ops: list                            # list[PatchOp]
    status: str = PatchStatus.ACTIVE
    created_at: float = field(default_factory=now)
    revoked_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "patch_id": self.patch_id,
            "title": self.title,
            "priority": self.priority,
            "base": dict(self.base),
            "ops": [o.to_dict() for o in self.ops],
            "status": self.status,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Patch":
        return Patch(
            patch_id=d["patch_id"],
            title=d["title"],
            priority=d["priority"],
            base=dict(d["base"]),
            ops=[PatchOp.from_dict(o) for o in d["ops"]],
            status=d["status"],
            created_at=d["created_at"],
            revoked_at=d.get("revoked_at"),
        )


# ---------------------------------------------------------------- 路线快照

class SnapshotStatus:
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"      # 已被整体替换（不是拼接，是切换）
    CLOSED = "CLOSED"              # 行程结束


@dataclass
class Snapshot:
    """车辆路线快照：固定瓦片集合 + 基线代际 + 生效补丁。

    不变量：base/tiles/patches 创建后不可变；更新只能产生新快照。
    """
    snapshot_id: str
    vehicle_id: str
    base: dict                     # {region_id: generation}
    tiles: dict                    # {tile_id: version} 固定的瓦片集合
    patches: list                  # 生效补丁 id（按优先级排序）
    route: dict                    # 路径详情
    status: str = SnapshotStatus.ACTIVE
    reason: str = "route_request"  # 产生原因：route_request/refresh/patch_transition/catchup
    lineage: list = field(default_factory=list)   # 解释链：来源快照
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Snapshot":
        return Snapshot(**d)
