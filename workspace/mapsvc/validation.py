"""候选版本验证：相邻瓦片接缝检查 + 跨区路线连通性检查。

接缝检查：
- 相邻瓦片（bbox 共边）边界端口按坐标容差配对；
- 未配对端口 -> dangling 错误；
- 已配对端口检查：车道数匹配（A.out == B.in）、宽度一致、限速连续性（告警）。

跨区路线检查：
- 在"候选合成图"（候选编辑 + 未涉及区域的当前发布版本）上，
  验证每对相邻区域的跨界端口对有向可达；
- 支持调用方追加命名 OD 检查点。
"""
from __future__ import annotations

from collections import deque

from .models import Candidate, TileData
from .routing import (
    PORT_PAIR_TOLERANCE_M, adjacent_pairs, build_merged_map, pair_ports,
)
from .store import Store

SEAM_WIDTH_TOLERANCE_M = 0.5
SPEED_CONTINUITY_WARN_KPH = 30.0


class Issue:
    ERROR = "ERROR"
    WARNING = "WARNING"

    @staticmethod
    def make(severity: str, code: str, message: str, **ctx) -> dict:
        return {"severity": severity, "code": code, "message": message, "context": ctx}


def validate_candidate(store: Store, cand: Candidate,
                       extra_od_pairs: list = None) -> dict:
    """完整验证候选版本，返回报告 {ok, issues, seam, cross_region}。"""
    issues: list = []
    edits = {e.tile_id: e for e in cand.edits}

    # ---- 结构检查：版本号必须是当前发布版本 +1（线性历史，防止过期基线）
    for e in cand.edits:
        latest = store.latest_tile_version(e.tile_id)
        expect = (latest or 0) + 1
        if e.version != expect:
            issues.append(Issue.make(
                Issue.ERROR, "VERSION_SKEW",
                f"瓦片 {e.tile_id} 版本 {e.version} 不是当前版本 "
                f"{latest or 0} 的下一个版本 {expect}",
                tile_id=e.tile_id))
        known_regions = {r["region_id"] for r in store.list_regions()}
        if e.region_id not in known_regions:
            issues.append(Issue.make(
                Issue.ERROR, "UNKNOWN_REGION",
                f"瓦片 {e.tile_id} 属于未知区域 {e.region_id}",
                tile_id=e.tile_id, region_id=e.region_id))

    # ---- 组装候选视图：编辑瓦片 + 同区域未编辑瓦片的最新发布版本
    view = _candidate_view(store, cand)

    # ---- 接缝检查
    seam_report = _check_seams(view, issues)

    # ---- 跨区路线检查（仅当接缝无 ERROR 时才有意义，但仍执行以给出完整报告）
    cross_report = _check_cross_region(store, cand, view, issues, extra_od_pairs)

    ok = not any(i["severity"] == Issue.ERROR for i in issues)
    return {
        "ok": ok,
        "issues": issues,
        "seam": seam_report,
        "cross_region": cross_report,
        "stats": {
            "tiles_in_view": len(view),
            "edited_tiles": len(edits),
            "effective_regions": list(cand.effective_regions),
        },
    }


def _candidate_view(store: Store, cand: Candidate) -> dict:
    """候选视图：tile_id -> TileData（编辑版本优先，否则当前发布版本）。"""
    view = {}
    for region in store.list_regions():
        gen = region["generation"]
        if gen > 0:
            rec = store.get_generation(region["region_id"], gen)
            for tile_id, ver in rec.tile_versions.items():
                view[tile_id] = store.get_tile(tile_id, ver)
    for e in cand.edits:
        view[e.tile_id] = e
    return view


def _check_seams(view: dict, issues: list) -> dict:
    tiles = list(view.values())
    pairs_checked = 0
    ports_paired = 0
    details = []

    for tile_a, tile_b in adjacent_pairs(tiles):
        pairs_checked += 1
        pairs, dangling_a, dangling_b = pair_ports(tile_a, tile_b)
        ports_paired += len(pairs)

        for p in dangling_a:
            issues.append(Issue.make(
                Issue.ERROR, "SEAM_DANGLING_PORT",
                f"瓦片 {tile_a.tile_id} 端口 {p.port_key} "
                f"在相邻瓦片 {tile_b.tile_id} 上找不到配对（{PORT_PAIR_TOLERANCE_M}m 容差）",
                tile_id=tile_a.tile_id, neighbor=tile_b.tile_id,
                port_key=p.port_key, x=p.x, y=p.y))
        for p in dangling_b:
            issues.append(Issue.make(
                Issue.ERROR, "SEAM_DANGLING_PORT",
                f"瓦片 {tile_b.tile_id} 端口 {p.port_key} "
                f"在相邻瓦片 {tile_a.tile_id} 上找不到配对（{PORT_PAIR_TOLERANCE_M}m 容差）",
                tile_id=tile_b.tile_id, neighbor=tile_a.tile_id,
                port_key=p.port_key, x=p.x, y=p.y))

        for pa, pb in pairs:
            offset = ((pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2) ** 0.5
            if pa.out_lanes != pb.in_lanes or pb.out_lanes != pa.in_lanes:
                issues.append(Issue.make(
                    Issue.ERROR, "SEAM_LANE_MISMATCH",
                    f"接缝 {tile_a.tile_id}/{tile_b.tile_id} 端口 {pa.port_key} "
                    f"车道数不匹配: A(out={pa.out_lanes},in={pa.in_lanes}) "
                    f"B(out={pb.out_lanes},in={pb.in_lanes})",
                    tile_a=tile_a.tile_id, tile_b=tile_b.tile_id,
                    port_key=pa.port_key))
            if abs(pa.width_m - pb.width_m) > SEAM_WIDTH_TOLERANCE_M:
                issues.append(Issue.make(
                    Issue.ERROR, "SEAM_WIDTH_MISMATCH",
                    f"接缝端口 {pa.port_key} 车道宽度差 "
                    f"{abs(pa.width_m - pb.width_m):.2f}m 超限",
                    tile_a=tile_a.tile_id, tile_b=tile_b.tile_id,
                    port_key=pa.port_key))
            if abs(pa.speed_limit_kph - pb.speed_limit_kph) > SPEED_CONTINUITY_WARN_KPH:
                issues.append(Issue.make(
                    Issue.WARNING, "SEAM_SPEED_JUMP",
                    f"接缝端口 {pa.port_key} 限速突变 "
                    f"{pa.speed_limit_kph} -> {pb.speed_limit_kph} kph",
                    tile_a=tile_a.tile_id, tile_b=tile_b.tile_id,
                    port_key=pa.port_key))
            details.append({
                "tiles": [tile_a.tile_id, tile_b.tile_id],
                "port_key": pa.port_key, "offset_m": round(offset, 3),
            })

    return {
        "adjacent_pairs_checked": pairs_checked,
        "ports_paired": ports_paired,
        "pairs": details,
    }


def _check_cross_region(store: Store, cand: Candidate, view: dict,
                        issues: list, extra_od_pairs: list = None) -> dict:
    """在候选合成图上验证跨区连通性。

    构造临时"候选基线"：生效区域用视图推导的代际映射不可行（尚未发布），
    因此直接在视图瓦片集合上构图，绕过代际记录。
    """
    from .routing import MergedMap, _add_cross_edge, _dist  # 复用构图原语

    mm = MergedMap(base={"__candidate__": -1})
    mm.tiles = dict(view)
    for tile in view.values():
        for n in tile.nodes:
            mm.nodes[(tile.tile_id, n.node_id)] = (n.x, n.y)
        for e in tile.edges:
            src = mm.nodes[(tile.tile_id, e.src)]
            dst = mm.nodes[(tile.tile_id, e.dst)]
            ref = f"{tile.tile_id}:{e.edge_id}"
            mm.adj.setdefault((tile.tile_id, e.src), []).append(
                ((tile.tile_id, e.dst), _dist(src, dst) / max(e.speed_limit_kph, 1.0), ref))
            mm.edge_refs[ref] = {"tile_id": tile.tile_id, "edge_id": e.edge_id,
                                 "kind": "internal",
                                 "length_m": _dist(src, dst)}
    for tile_a, tile_b in adjacent_pairs(list(view.values())):
        pairs, _, _ = pair_ports(tile_a, tile_b)
        for pa, pb in pairs:
            _add_cross_edge(mm, tile_a, pa, tile_b, pb)
            _add_cross_edge(mm, tile_b, pb, tile_a, pa)

    # 收集跨区端口对并做有向可达性检查
    region_of = {t.tile_id: t.region_id for t in view.values()}
    checks = []
    for tile_a, tile_b in adjacent_pairs(list(view.values())):
        if region_of[tile_a.tile_id] == region_of[tile_b.tile_id]:
            continue  # 只关心跨区接缝
        pairs, _, _ = pair_ports(tile_a, tile_b)
        for pa, pb in pairs:
            if pa.out_lanes > 0 and pb.in_lanes > 0:
                checks.append({
                    "name": f"{tile_a.tile_id}->{tile_b.tile_id}:{pa.port_key}",
                    "src": (tile_a.tile_id, pa.node_id),
                    "dst": (tile_b.tile_id, pb.node_id),
                    "regions": [region_of[tile_a.tile_id], region_of[tile_b.tile_id]],
                })
            if pb.out_lanes > 0 and pa.in_lanes > 0:
                checks.append({
                    "name": f"{tile_b.tile_id}->{tile_a.tile_id}:{pb.port_key}",
                    "src": (tile_b.tile_id, pb.node_id),
                    "dst": (tile_a.tile_id, pa.node_id),
                    "regions": [region_of[tile_b.tile_id], region_of[tile_a.tile_id]],
                })

    # 区域锚点检查（核心）：相邻区域的锚点节点必须双向可达。
    # 端口对检查只验证跨界边本身存在，锚点检查验证"进入该区域后
    # 能深入区域内部"，才是真正的跨区路线验证。
    anchors = _region_anchors(mm, view)
    adjacent_regions = set()
    for tile_a, tile_b in adjacent_pairs(list(view.values())):
        ra, rb = region_of[tile_a.tile_id], region_of[tile_b.tile_id]
        if ra != rb:
            adjacent_regions.add((ra, rb))
            adjacent_regions.add((rb, ra))
    for ra, rb in sorted(adjacent_regions):
        if ra not in anchors or rb not in anchors:
            continue
        checks.append({
            "name": f"anchor:{ra}->{rb}",
            "src": anchors[ra], "dst": anchors[rb],
            "regions": [ra, rb], "anchor": True,
        })

    results = []
    for chk in checks:
        ok = _reachable(mm, chk["src"], chk["dst"])
        results.append({"name": chk["name"], "regions": chk["regions"], "ok": ok})
        if not ok:
            issues.append(Issue.make(
                Issue.ERROR, "CROSS_REGION_UNREACHABLE",
                f"跨区路线检查失败: {chk['name']} "
                f"({chk['regions'][0]} -> {chk['regions'][1]}) 不可达",
                check=chk["name"], regions=chk["regions"]))

    # 追加命名 OD 检查
    for od in (extra_od_pairs or []):
        src_key = _nearest_in_view(mm, od["src"])
        dst_key = _nearest_in_view(mm, od["dst"])
        ok = src_key is not None and dst_key is not None and _reachable(mm, src_key, dst_key)
        results.append({"name": od.get("name", "od"), "ok": ok, "custom": True})
        if not ok:
            issues.append(Issue.make(
                Issue.ERROR, "OD_UNREACHABLE",
                f"命名路线检查失败: {od.get('name')}", check=od.get("name")))

    return {"checks": results, "passed": sum(1 for r in results if r["ok"]),
            "total": len(results)}


def _region_anchors(mm, view: dict) -> dict:
    """每区域的锚点节点：离区域几何中心最近的车道节点。"""
    boxes: dict = {}
    for t in view.values():
        b = boxes.setdefault(t.region_id, [1e18, 1e18, -1e18, -1e18])
        b[0] = min(b[0], t.bbox[0]); b[1] = min(b[1], t.bbox[1])
        b[2] = max(b[2], t.bbox[2]); b[3] = max(b[3], t.bbox[3])
    anchors = {}
    for region_id, (x0, y0, x1, y1) in boxes.items():
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best, best_d = None, float("inf")
        for key, (nx, ny) in mm.nodes.items():
            tile_id = key[0]
            if view[tile_id].region_id != region_id:
                continue
            d = (nx - cx) ** 2 + (ny - cy) ** 2
            if d < best_d:
                best, best_d = key, d
        if best is not None:
            anchors[region_id] = best
    return anchors


def _reachable(mm, src_key, dst_key) -> bool:
    if src_key not in mm.nodes or dst_key not in mm.nodes:
        return False
    seen = {src_key}
    q = deque([src_key])
    while q:
        u = q.popleft()
        if u == dst_key:
            return True
        for v, _cost, _ref in mm.adj.get(u, []):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return False


def _nearest_in_view(mm, xy):
    best, best_d = None, float("inf")
    for key, (nx, ny) in mm.nodes.items():
        d = (nx - xy[0]) ** 2 + (ny - xy[1]) ** 2
        if d < best_d:
            best, best_d = key, d
    return best
