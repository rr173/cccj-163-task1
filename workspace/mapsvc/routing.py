"""有效地图合成与车道级路由。

有效地图 = 基线代际（每区域一份不可变瓦片集合）+ 生效补丁（仅限制类操作）。
跨瓦片连接由边界端口配对生成虚拟边；路由在合成图上做 Dijkstra。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .models import Patch, TileData
from .store import Store

PORT_PAIR_TOLERANCE_M = 2.0     # 边界端口配对坐标容差


@dataclass
class MergedMap:
    """合成图：节点键为 (tile_id, node_id)，跨界边由端口配对生成。"""
    base: dict                                   # {region_id: generation}
    tiles: dict = field(default_factory=dict)    # tile_id -> TileData
    nodes: dict = field(default_factory=dict)    # (tile,node) -> (x,y)
    adj: dict = field(default_factory=dict)      # (tile,node) -> [(nbr, cost, edge_ref)]
    edge_refs: dict = field(default_factory=dict)  # edge_ref -> 描述（含来源瓦片版本）
    closed_edges: set = field(default_factory=set)  # 被补丁关闭的 (tile_id, edge_id)
    seam_gaps: list = field(default_factory=list)   # 未配对端口（验证用）

    def edge_count(self) -> int:
        return sum(len(v) for v in self.adj.values())


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _shared_boundary(a_bbox: tuple, b_bbox: tuple, tol: float = 1e-6):
    """两相邻 bbox 的共享边界。返回 (方向, 常量坐标, 下界, 上界) 或 None。"""
    aminx, aminy, amaxx, amaxy = a_bbox
    bminx, bminy, bmaxx, bmaxy = b_bbox
    if abs(amaxx - bminx) < tol:
        return ("v", amaxx, max(aminy, bminy), min(amaxy, bmaxy))
    if abs(bmaxx - aminx) < tol:
        return ("v", bmaxx, max(aminy, bminy), min(amaxy, bmaxy))
    if abs(amaxy - bminy) < tol:
        return ("h", amaxy, max(aminx, bminx), min(amaxx, bmaxx))
    if abs(bmaxy - aminy) < tol:
        return ("h", bmaxy, max(aminx, bminx), min(amaxx, bmaxx))
    return None


def pair_ports(tile_a: TileData, tile_b: TileData,
               tol: float = PORT_PAIR_TOLERANCE_M) -> tuple:
    """贪心最近邻配对两个瓦片在共享边界上的端口。

    只考虑落在共享边界带内的端口（其余端口属于其他接缝）。
    返回 (pairs, dangling_a, dangling_b)。
    """
    boundary = _shared_boundary(tile_a.bbox, tile_b.bbox)
    if boundary is None:
        return [], list(tile_a.ports), list(tile_b.ports)
    orient, c, lo, hi = boundary

    def on_seam(p) -> bool:
        if orient == "v":
            return abs(p.x - c) <= tol and lo - tol <= p.y <= hi + tol
        return abs(p.y - c) <= tol and lo - tol <= p.x <= hi + tol

    ports_a = [p for p in tile_a.ports if on_seam(p)]
    ports_b = [p for p in tile_b.ports if on_seam(p)]

    pairs, used_b, dangling_a = [], set(), []
    indexed_b = list(enumerate(ports_b))
    for pa in ports_a:
        best, best_d = None, tol
        for idx, pb in indexed_b:
            if idx in used_b:
                continue
            d = math.hypot(pa.x - pb.x, pa.y - pb.y)
            if d <= best_d:
                best, best_d = idx, d
        if best is None:
            dangling_a.append(pa)
        else:
            used_b.add(best)
            pairs.append((pa, ports_b[best]))
    dangling_b = [pb for idx, pb in indexed_b if idx not in used_b]
    return pairs, dangling_a, dangling_b


def _bboxes_adjacent(a: tuple, b: tuple, tol: float = 1e-6) -> bool:
    """两 bbox 是否共享一段边界（边相接且重叠长度>0）。"""
    aminx, aminy, amaxx, amaxy = a
    bminx, bminy, bmaxx, bmaxy = b
    touch_x = (abs(amaxx - bminx) < tol or abs(bmaxx - aminx) < tol)
    overlap_y = min(amaxy, bmaxy) - max(aminy, bminy) > tol
    touch_y = (abs(amaxy - bminy) < tol or abs(bmaxy - aminy) < tol)
    overlap_x = min(amaxx, bmaxx) - max(aminx, bminx) > tol
    return (touch_x and overlap_y) or (touch_y and overlap_x)


def adjacent_pairs(tiles: list) -> list:
    """返回 [(tile_a, tile_b)] 相邻瓦片对。"""
    out = []
    for i in range(len(tiles)):
        for j in range(i + 1, len(tiles)):
            if _bboxes_adjacent(tiles[i].bbox, tiles[j].bbox):
                out.append((tiles[i], tiles[j]))
    return out


def build_merged_map(store: Store, base: dict, patches: list = None) -> MergedMap:
    """按基线代际 + 补丁合成有效地图。

    base: {region_id: generation}，每区域解析到该代际记录的瓦片版本集合。
    """
    mm = MergedMap(base=dict(base))
    tile_versions = {}
    for region_id, gen in base.items():
        rec = store.get_generation(region_id, gen)
        tile_versions.update(rec.tile_versions)

    tiles = {}
    for tile_id, ver in tile_versions.items():
        tiles[tile_id] = store.get_tile(tile_id, ver)
    mm.tiles = tiles

    # 1) 装载节点与内部有向边
    for tile in tiles.values():
        for n in tile.nodes:
            mm.nodes[(tile.tile_id, n.node_id)] = (n.x, n.y)
        for e in tile.edges:
            src = mm.nodes[(tile.tile_id, e.src)]
            dst = mm.nodes[(tile.tile_id, e.dst)]
            length = _dist(src, dst)
            cost = length / max(e.speed_limit_kph, 1.0)   # 时间代价（相对值）
            ref = f"{tile.tile_id}:{e.edge_id}"
            mm.adj.setdefault((tile.tile_id, e.src), []).append(
                ((tile.tile_id, e.dst), cost, ref))
            mm.edge_refs[ref] = {
                "tile_id": tile.tile_id, "tile_version": tile.version,
                "edge_id": e.edge_id, "kind": "internal",
                "lanes": e.lanes, "speed_limit_kph": e.speed_limit_kph,
                "length_m": length,
            }

    # 2) 端口配对生成跨界虚拟边（双向按 out/in 配对）
    for tile_a, tile_b in adjacent_pairs(list(tiles.values())):
        pairs, dangling_a, dangling_b = pair_ports(tile_a, tile_b)
        for pa, pb in pairs:
            # A -> B 方向：A.out_lanes 与 B.in_lanes 需一致（验证阶段保证）
            _add_cross_edge(mm, tile_a, pa, tile_b, pb)
            _add_cross_edge(mm, tile_b, pb, tile_a, pa)
        for p in dangling_a:
            mm.seam_gaps.append({"tile_id": tile_a.tile_id, "port": p.to_dict()})
        for p in dangling_b:
            mm.seam_gaps.append({"tile_id": tile_b.tile_id, "port": p.to_dict()})

    # 3) 应用补丁（仅限制类）：关闭边 / 减车道 / 限速
    for patch in (patches or []):
        for op in patch.ops:
            _apply_op(mm, op)

    return mm


def _add_cross_edge(mm: MergedMap, ta: TileData, pa, tb: TileData, pb) -> None:
    """A 端口节点 -> B 端口节点 的跨界虚拟边（方向由 out/in 决定）。"""
    if pa.out_lanes <= 0 or pb.in_lanes <= 0:
        return
    key_a = (ta.tile_id, pa.node_id)
    key_b = (tb.tile_id, pb.node_id)
    if key_a not in mm.nodes or key_b not in mm.nodes:
        return
    length = max(_dist(mm.nodes[key_a], mm.nodes[key_b]), 0.1)
    speed = min(pa.speed_limit_kph, pb.speed_limit_kph)
    ref = f"x:{ta.tile_id}:{pa.port_key}->{tb.tile_id}:{pb.port_key}"
    mm.adj.setdefault(key_a, []).append((key_b, length / max(speed, 1.0), ref))
    mm.edge_refs[ref] = {
        "tile_id": None, "kind": "cross",
        "from_tile": ta.tile_id, "to_tile": tb.tile_id,
        "lanes": min(pa.out_lanes, pb.in_lanes),
        "speed_limit_kph": speed, "length_m": length,
    }


def op_fully_closes(edge, op) -> bool:
    """该操作是否使边完全不可通行（CLOSE_LANE 未封全部车道时仍可通行）。"""
    if op.kind == "CLOSE_EDGE":
        return True
    if op.kind == "CLOSE_LANE":
        mask = int(op.params.get("lane_mask", 0))
        remaining = edge.lanes - bin(mask & ((1 << edge.lanes) - 1)).count("1")
        return remaining <= 0
    return False


def _apply_op(mm: MergedMap, op) -> None:
    tile = mm.tiles.get(op.tile_id)
    if tile is None:
        return
    emap = tile.edge_map()
    edge = emap.get(op.edge_id)
    if edge is None:
        return
    targets = [edge]
    if op.kind == "CLOSE_EDGE" and op.params.get("both_directions", True):
        # 封路默认封双向：找几何反向边
        for other in emap.values():
            if other.src == edge.dst and other.dst == edge.src and other.kind == edge.kind:
                targets.append(other)
    for e in targets:
        ref = f"{tile.tile_id}:{e.edge_id}"
        if op.kind == "CLOSE_EDGE" or op_fully_closes(e, op):
            _remove_edge(mm, (tile.tile_id, e.src), ref)
            mm.closed_edges.add((tile.tile_id, e.edge_id))
        elif op.kind == "CLOSE_LANE":
            # 部分车道封闭：降容，代价按剩余车道比例上升
            mask = int(op.params.get("lane_mask", 0))
            remaining = e.lanes - bin(mask & ((1 << e.lanes) - 1)).count("1")
            factor = e.lanes / max(remaining, 1)
            _recost_edge(mm, (tile.tile_id, e.src), ref,
                         e.speed_limit_kph / factor)
        elif op.kind == "SPEED_CAP":
            cap = float(op.params.get("speed_kph", 10))
            _recost_edge(mm, (tile.tile_id, e.src), ref, cap)


def _remove_edge(mm: MergedMap, src_key, ref: str) -> None:
    mm.adj[src_key] = [t for t in mm.adj.get(src_key, []) if t[2] != ref]


def _recost_edge(mm: MergedMap, src_key, ref: str, speed_kph: float) -> None:
    info = mm.edge_refs.get(ref)
    if not info:
        return
    new_cost = info["length_m"] / max(speed_kph, 1.0)
    mm.adj[src_key] = [
        (nbr, new_cost if r == ref else cost, r)
        for (nbr, cost, r) in mm.adj.get(src_key, [])
    ]


# ---------------------------------------------------------------- 路由

def _nearest_node(mm: MergedMap, x: float, y: float):
    best, best_d = None, float("inf")
    for key, (nx, ny) in mm.nodes.items():
        d = math.hypot(nx - x, ny - y)
        if d < best_d:
            best, best_d = key, d
    return best


def shortest_path(mm: MergedMap, src_xy: tuple, dst_xy: tuple) -> dict:
    """Dijkstra 最短路。返回路径详情或抛出 RouteError。"""
    src = _nearest_node(mm, *src_xy)
    dst = _nearest_node(mm, *dst_xy)
    if src is None or dst is None:
        raise RouteError("地图为空，无法定位起终点")

    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    visited = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u == dst:
            break
        for v, cost, ref in mm.adj.get(u, []):
            nd = d + cost
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (u, ref)
                heapq.heappush(pq, (nd, v))

    if dst not in dist:
        raise RouteError(f"目标不可达: {src_xy} -> {dst_xy}")

    # 回溯路径
    nodes_path, edge_path = [dst], []
    cur = dst
    while cur != src:
        u, ref = prev[cur]
        edge_path.append(ref)
        nodes_path.append(u)
        cur = u
    nodes_path.reverse()
    edge_path.reverse()

    tiles_used = sorted({k[0] for k in nodes_path})
    length_m = sum(mm.edge_refs[r]["length_m"] for r in edge_path)
    return {
        "src_xy": list(src_xy), "dst_xy": list(dst_xy),
        "nodes": [[t, n] for (t, n) in nodes_path],
        "edges": edge_path,
        "tiles_used": tiles_used,
        "length_m": round(length_m, 2),
        "cost": round(dist[dst], 4),
    }


class RouteError(Exception):
    pass
