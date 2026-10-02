"""核心服务：发布引擎、紧急补丁、路线快照。

关键不变量：
1. 发布按区域代际原子完成——任一区域失败，整体回滚，车辆侧永远只看到完整代际；
2. 快照固定 {region: generation} + 瓦片版本集合，创建后不可变；
   途中更新只能"整体切换"到新快照，禁止新旧拓扑拼接；
3. 补丁仅允许限制类操作，必须与基线代际兼容；撤销干净回到基线；
4. 任何时刻车辆的有效版本 = 基线代际 + 生效补丁，单一且可解释；
   离线追赶直接给出单一目标版本，不追增量序列。
"""
from __future__ import annotations

import json
from typing import Optional

from .models import (
    Candidate, CandidateStatus, GenerationRecord, Patch, PatchOp, PatchStatus,
    RESTRICTIVE_OPS, Snapshot, SnapshotStatus, TileData, new_id, now,
)
from .routing import RouteError, build_merged_map, shortest_path
from .store import Store
from .validation import validate_candidate


class ServiceError(Exception):
    pass


class MapService:
    """门面：组合发布、补丁、快照三大子服务。"""

    def __init__(self, store: Store):
        self.store = store
        self.publish_engine = PublishEngine(store)
        self.patches = PatchService(store, self)
        self.snapshots = SnapshotService(store, self)

    # ------------------------------------------------------------ 有效版本

    def effective_version(self) -> dict:
        """当前有效版本：{region: generation} + 生效补丁 id 列表。"""
        base = {r["region_id"]: r["generation"]
                for r in self.store.list_regions() if r["generation"] > 0}
        patches = [p.patch_id for p in self.store.list_patches(status=PatchStatus.ACTIVE)]
        return {"base": base, "patches": patches}

    def active_patch_objs(self) -> list:
        return self.store.list_patches(status=PatchStatus.ACTIVE)

    def explain_snapshot(self, snap: Snapshot) -> dict:
        """单一可解释版本说明：基线 + 补丁 + 固定瓦片 + 解释链。"""
        return {
            "snapshot_id": snap.snapshot_id,
            "vehicle_id": snap.vehicle_id,
            "status": snap.status,
            "reason": snap.reason,
            "base": dict(snap.base),
            "tiles": dict(snap.tiles),
            "patches": [
                {"patch_id": p.patch_id, "title": p.title,
                 "ops": [o.to_dict() for o in p.ops]}
                for p in (self._safe_get_patch(pid) for pid in snap.patches)
                if p is not None
            ],
            "route": {
                "src_xy": snap.route.get("src_xy"),
                "dst_xy": snap.route.get("dst_xy"),
                "length_m": snap.route.get("length_m"),
                "tiles_used": snap.route.get("tiles_used"),
                "nodes": snap.route.get("nodes"),
                "edges": snap.route.get("edges"),
            },
            "lineage": list(snap.lineage),
            "created_at": snap.created_at,
        }

    def _safe_get_patch(self, patch_id: str) -> Optional[Patch]:
        try:
            return self.store.get_patch(patch_id)
        except KeyError:
            return None


# ---------------------------------------------------------------- 发布引擎

class PublishEngine:
    def __init__(self, store: Store):
        self.store = store

    def create_candidate(self, name: str, edits: list,
                         effective_regions: Optional[list] = None) -> Candidate:
        if not edits:
            raise ServiceError("候选版本至少包含一个瓦片编辑")
        regions = effective_regions or sorted({e.region_id for e in edits})
        cand = Candidate(
            candidate_id=new_id("cand"), name=name, edits=edits,
            effective_regions=list(regions),
        )
        self.store.put_candidate(cand)
        return cand

    def validate(self, candidate_id: str, extra_od_pairs: Optional[list] = None) -> dict:
        cand = self.store.get_candidate(candidate_id)
        report = validate_candidate(self.store, cand, extra_od_pairs)
        cand.validation = report
        if report["ok"]:
            cand.status = CandidateStatus.VALIDATED
        self.store.put_candidate(cand)
        return report

    def publish(self, candidate_id: str) -> dict:
        """按区域代际原子发布。失败整体回滚，已发布代际不变。"""
        cand = self.store.get_candidate(candidate_id)
        if cand.status == CandidateStatus.PUBLISHED:
            raise ServiceError("候选版本已发布")
        if cand.validation is None:
            raise ServiceError("候选版本尚未验证")
        if not cand.validation.get("ok"):
            cand.status = CandidateStatus.FAILED
            cand.error = "验证未通过，禁止发布"
            self.store.put_candidate(cand)
            raise ServiceError(cand.error)

        # 按区域分组编辑
        edits_by_region: dict = {}
        for e in cand.edits:
            edits_by_region.setdefault(e.region_id, []).append(e)

        published = []
        try:
            with self.store._lock:
                cur = self.store.conn.cursor()
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    for region_id, tiles in edits_by_region.items():
                        row = cur.execute(
                            "SELECT generation FROM regions WHERE region_id=?",
                            (region_id,)).fetchone()
                        if row is None:
                            raise ServiceError(f"未知区域: {region_id}")
                        new_gen = row["generation"] + 1

                        # 瓦片落库（同事务）
                        self.store.put_tiles_tx(cur, tiles)

                        # 新代际的完整瓦片集合 = 上一代际 + 本次编辑覆盖
                        tile_versions = {}
                        if row["generation"] > 0:
                            prev = self.store.get_generation(region_id, row["generation"])
                            tile_versions.update(prev.tile_versions)
                        for t in tiles:
                            tile_versions[t.tile_id] = t.version

                        rec = GenerationRecord(
                            region_id=region_id, generation=new_gen,
                            candidate_id=cand.candidate_id,
                            tile_versions=tile_versions,
                        )
                        cur.execute(
                            "INSERT INTO generations(region_id, generation, data) "
                            "VALUES (?,?,?)",
                            (region_id, new_gen,
                             json.dumps(rec.to_dict())))
                        cur.execute(
                            "UPDATE regions SET generation=? WHERE region_id=?",
                            (new_gen, region_id))
                        published.append({"region_id": region_id,
                                          "generation": new_gen})
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
        except Exception as exc:
            # 任何失败（含存储故障）都已回滚：线上代际不变，单一版本保持
            cand.status = CandidateStatus.FAILED
            cand.error = f"发布失败（已回滚，线上代际不变）: {exc}"
            self.store.put_candidate(cand)
            raise ServiceError(cand.error) from exc

        cand.status = CandidateStatus.PUBLISHED
        cand.published_at = now()
        self.store.put_candidate(cand)
        return {"candidate_id": cand.candidate_id, "published": published}


# ---------------------------------------------------------------- 补丁服务

class PatchService:
    def __init__(self, store: Store, svc: "MapService"):
        self.store = store
        self.svc = svc

    def create_patch(self, title: str, ops: list, priority: int = 100,
                     base: Optional[dict] = None,
                     auto_transition: bool = True) -> dict:
        """创建高优先级补丁（紧急封路等）。

        - 基线缺省为当前有效版本；
        - 兼容性校验失败则拒绝；
        - auto_transition: 立即为受影响车辆快照生成安全过渡方案。
        """
        base = base or self.svc.effective_version()["base"]
        patch = Patch(patch_id=new_id("patch"), title=title, priority=priority,
                      base=dict(base), ops=list(ops))
        problems = self.check_compatible(patch)
        if problems:
            raise ServiceError("补丁与基线不兼容: " + "; ".join(problems))
        self.store.put_patch(patch)

        transitions = []
        if auto_transition:
            transitions = self._apply_to_active_snapshots(patch)
        return {"patch": patch.to_dict(), "transitions": transitions}

    def check_compatible(self, patch: Patch) -> list:
        """兼容性：仅限制类操作；引用的瓦片/边必须存在于基线代际。"""
        problems = []
        # 解析基线瓦片集合
        tiles_at_base: dict = {}
        for region_id, gen in patch.base.items():
            try:
                rec = self.store.get_generation(region_id, gen)
            except KeyError:
                problems.append(f"基线代际不存在: {region_id}@g{gen}")
                continue
            tiles_at_base.update(rec.tile_versions)

        for op in patch.ops:
            if op.kind not in RESTRICTIVE_OPS:
                problems.append(
                    f"操作 {op.kind} 非限制类，补丁不得新增/改变拓扑")
                continue
            ver = tiles_at_base.get(op.tile_id)
            if ver is None:
                problems.append(f"瓦片 {op.tile_id} 不在补丁基线中")
                continue
            tile = self.store.get_tile(op.tile_id, ver)
            edge = tile.edge_map().get(op.edge_id)
            if edge is None:
                problems.append(
                    f"边 {op.edge_id} 不存在于 {op.tile_id}@v{ver}")
                continue
            if op.kind == "CLOSE_LANE":
                mask = op.params.get("lane_mask", 0)
                if mask >= (1 << edge.lanes):
                    problems.append(
                        f"车道掩码超出 {op.edge_id} 现有车道数 {edge.lanes}")
        return problems

    def revoke_patch(self, patch_id: str) -> dict:
        """撤销补丁：状态置 REVOKED，受影响快照整体重定基线（单一版本）。"""
        patch = self.store.get_patch(patch_id)
        if patch.status == PatchStatus.REVOKED:
            raise ServiceError("补丁已撤销")
        patch.status = PatchStatus.REVOKED
        patch.revoked_at = now()
        self.store.put_patch(patch)

        rebased = []
        for snap in self.store.list_snapshots(status=SnapshotStatus.ACTIVE):
            if patch_id in snap.patches:
                new_snap = self.svc.snapshots._rebase_snapshot(
                    snap, reason="patch_revoked")
                rebased.append({"vehicle_id": snap.vehicle_id,
                                "from": snap.snapshot_id,
                                "to": new_snap.snapshot_id if new_snap else None})
        return {"patch_id": patch_id, "status": patch.status,
                "rebased_snapshots": rebased}

    def _apply_to_active_snapshots(self, patch: Patch) -> list:
        """高优先级补丁生效：为路线受影响的活跃快照生成过渡快照。

        安全过渡语义：不修改原快照（不可拼接），而是生成同基线+新补丁的
        整体新快照，路线从车辆当前位置（上报位置或路线起点）重算。
        """
        transitions = []
        for snap in self.store.list_snapshots(status=SnapshotStatus.ACTIVE):
            if not self._snapshot_affected(snap, patch):
                continue
            new_snap = self.svc.snapshots._rebase_snapshot(
                snap, reason="patch_transition", extra_patch=patch.patch_id)
            transitions.append({
                "vehicle_id": snap.vehicle_id,
                "from_snapshot": snap.snapshot_id,
                "to_snapshot": new_snap.snapshot_id if new_snap else None,
                "ok": new_snap is not None,
            })
        return transitions

    def _snapshot_affected(self, snap: Snapshot, patch: Patch) -> bool:
        """快照路线是否经过补丁完全封闭的边（含双向封闭的反向边）。"""
        from .routing import op_fully_closes
        closed_refs = set()
        for op in patch.ops:
            if op.kind not in ("CLOSE_EDGE", "CLOSE_LANE"):
                continue
            ver = self._tile_version_at_base(patch, op.tile_id)
            if ver is None:
                continue
            tile = self.store.get_tile(op.tile_id, ver)
            edge = tile.edge_map().get(op.edge_id)
            if edge is None or not op_fully_closes(edge, op):
                continue
            closed_refs.add(f"{op.tile_id}:{op.edge_id}")
            if op.kind == "CLOSE_EDGE" and op.params.get("both_directions", True):
                for other in tile.edges:
                    if (other.src == edge.dst and other.dst == edge.src
                            and other.kind == edge.kind):
                        closed_refs.add(f"{op.tile_id}:{other.edge_id}")
        return any(ref in closed_refs for ref in snap.route.get("edges", []))

    def _tile_version_at_base(self, patch: Patch, tile_id: str):
        for region_id, gen in patch.base.items():
            try:
                rec = self.store.get_generation(region_id, gen)
            except KeyError:
                continue
            if tile_id in rec.tile_versions:
                return rec.tile_versions[tile_id]
        return None


# ---------------------------------------------------------------- 快照服务

class SnapshotService:
    def __init__(self, store: Store, svc: "MapService"):
        self.store = store
        self.svc = svc

    def request_route(self, vehicle_id: str, src_xy: list, dst_xy: list) -> Snapshot:
        """车辆请求路线：在当前有效版本上算路并固定快照。"""
        ver = self.svc.effective_version()
        return self._create_snapshot(vehicle_id, tuple(src_xy), tuple(dst_xy),
                                     ver, reason="route_request")

    def refresh(self, snapshot_id: str, at_xy: list) -> Snapshot:
        """过渡点整体切换：从当前位置到原目的地，在最新有效版本上重算。

        旧快照置 SUPERSEDED——是整体替换而非逐瓦片拼接。
        """
        snap = self.store.get_snapshot(snapshot_id)
        if snap.status != SnapshotStatus.ACTIVE:
            raise ServiceError(f"快照 {snapshot_id} 状态 {snap.status}，不可切换")
        ver = self.svc.effective_version()
        if ver["base"] == snap.base and ver["patches"] == snap.patches:
            return snap  # 无变化，无需切换
        dst = tuple(snap.route["dst_xy"])
        new_snap = self._create_snapshot(
            snap.vehicle_id, tuple(at_xy), dst, ver,
            reason="refresh", lineage=[snap.snapshot_id])
        self._supersede(snap)
        return new_snap

    def catchup(self, vehicle_id: str, snapshot_id: str, at_xy: list) -> dict:
        """离线追赶：车辆重连后一次性给出单一目标版本。

        不追增量序列——直接对比当前有效版本，要么无需动作，要么整体切换。
        """
        snap = self.store.get_snapshot(snapshot_id)
        ver = self.svc.effective_version()
        if ver["base"] == snap.base and ver["patches"] == snap.patches:
            return {"action": "none",
                    "explain": self.svc.explain_snapshot(snap)}
        dst = tuple(snap.route["dst_xy"])
        try:
            new_snap = self._create_snapshot(
                vehicle_id, tuple(at_xy), dst, ver,
                reason="catchup", lineage=[snap.snapshot_id])
        except RouteError as exc:
            return {"action": "unreachable", "reason": str(exc),
                    "explain": self.svc.explain_snapshot(snap)}
        self._supersede(snap)
        return {"action": "switch",
                "from": self.svc.explain_snapshot(snap),
                "to": self.svc.explain_snapshot(new_snap)}

    def close(self, snapshot_id: str) -> Snapshot:
        snap = self.store.get_snapshot(snapshot_id)
        snap.status = SnapshotStatus.CLOSED
        self.store.put_snapshot(snap)
        return snap

    # ------------------------------------------------------------ 内部

    def _create_snapshot(self, vehicle_id: str, src_xy: tuple, dst_xy: tuple,
                         version: dict, reason: str,
                         lineage: Optional[list] = None) -> Snapshot:
        patches = [self.store.get_patch(pid) for pid in version["patches"]]
        mm = build_merged_map(self.store, version["base"], patches)
        route = shortest_path(mm, src_xy, dst_xy)

        # 固定瓦片集合：路线涉及瓦片在基线代际下的版本
        tiles_pinned = {}
        for region_id, gen in version["base"].items():
            rec = self.store.get_generation(region_id, gen)
            for tile_id, ver in rec.tile_versions.items():
                if tile_id in route["tiles_used"]:
                    tiles_pinned[tile_id] = ver

        snap = Snapshot(
            snapshot_id=new_id("snap"), vehicle_id=vehicle_id,
            base=dict(version["base"]), tiles=tiles_pinned,
            patches=list(version["patches"]), route=route,
            reason=reason, lineage=lineage or [],
        )
        self.store.put_snapshot(snap)
        # 不变量：每辆车同一时刻只有一个 ACTIVE 快照（单一可解释版本）
        for other in self.store.list_snapshots(vehicle_id=vehicle_id,
                                               status=SnapshotStatus.ACTIVE):
            if other.snapshot_id != snap.snapshot_id:
                self._supersede(other)
        return snap

    def _rebase_snapshot(self, snap: Snapshot, reason: str,
                         extra_patch: Optional[str] = None) -> Optional[Snapshot]:
        """整体重定基线：以当前有效版本为单一目标，从原起点重算路线。

        用于补丁生效/撤销时的安全过渡。目标永远是"当前有效版本"
        （基线代际 + 全部生效补丁），保证车辆侧单一可解释。
        失败（如不可达）返回 None，原快照保持 ACTIVE——
        车辆继续使用单一旧版本，绝不拼接。
        """
        ver = self.svc.effective_version()
        patches = list(ver["patches"])
        if extra_patch and extra_patch not in patches:
            patches.append(extra_patch)
        src = tuple(snap.route["src_xy"])
        dst = tuple(snap.route["dst_xy"])
        try:
            new_snap = self._create_snapshot(
                snap.vehicle_id, src, dst,
                {"base": ver["base"], "patches": patches},
                reason=reason, lineage=snap.lineage + [snap.snapshot_id])
        except RouteError:
            return None
        self._supersede(snap)
        return new_snap

    def _supersede(self, snap: Snapshot) -> None:
        snap.status = SnapshotStatus.SUPERSEDED
        self.store.put_snapshot(snap)
