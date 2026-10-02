"""网格城市地图生成器：演示/测试/场景共用的瓦片构造工具。

每个瓦片含 3x3 路口节点；朝邻居方向的边界节点声明边界端口
（角点坐标去重，同一物理路口只声明一个端口）；
相邻瓦片的同坐标端口在验证/构图时配对，形成跨瓦片拓扑。
"""
from __future__ import annotations

from .models import BoundaryPort, Edge, Node, TileData

DEFAULT_SIZE_M = 100.0

# 2x2 城市每块瓦片的邻居方向
CITY_TOWARD = {
    "t00": ("E", "N"), "t10": ("W", "N"),
    "t01": ("E", "S"), "t11": ("W", "S"),
}
CITY_POS = {"t00": (0, 0), "t10": (1, 0), "t01": (0, 1), "t11": (1, 1)}


def make_grid_tile(tile_id: str, region_id: str, tx: int, ty: int,
                   version: int = 1, size: float = DEFAULT_SIZE_M,
                   speed_limit_kph: float = 50.0,
                   toward=("E", "W", "N", "S"),
                   lane_overrides: dict = None,
                   port_shift: dict = None,
                   port_lanes: dict = None,
                   edge_drops: set = None) -> TileData:
    """生成一个网格瓦片。

    lane_overrides: {edge_id: {"lanes": n, "speed_limit_kph": s}} 编辑用
    port_shift:     {port_key: (dx, dy)} 人为制造接缝错位（测试用）
    port_lanes:     {port_key: (out_lanes, in_lanes)} 人为制造车道不匹配
    edge_drops:     {edge_id} 移除指定边（模拟道路删除）
    """
    ox, oy = tx * size, ty * size
    half = size / 2.0
    nodes, edges, ports = [], [], []

    def nid(i: int, j: int) -> str:
        return f"n{i}{j}"

    for i in range(3):
        for j in range(3):
            nodes.append(Node(node_id=nid(i, j), x=ox + i * half, y=oy + j * half))

    def add_edge(a: str, b: str) -> None:
        eid = f"e_{a}_{b}"
        if edge_drops and eid in edge_drops:
            return
        ov = (lane_overrides or {}).get(eid, {})
        edges.append(Edge(
            edge_id=eid, src=a, dst=b,
            lanes=ov.get("lanes", 1),
            speed_limit_kph=ov.get("speed_limit_kph", speed_limit_kph),
        ))

    for i in range(2):
        for j in range(3):
            add_edge(nid(i, j), nid(i + 1, j))
            add_edge(nid(i + 1, j), nid(i, j))
    for i in range(3):
        for j in range(2):
            add_edge(nid(i, j), nid(i, j + 1))
            add_edge(nid(i, j + 1), nid(i, j))

    # 边界端口：只朝邻居方向声明；角点坐标合并为一个端口，
    # 键名用 "|" 连接各方向（如 "E:2|N:2"），配对时按坐标匹配
    declared = []
    if "E" in toward:
        declared += [(f"E:{j}", 2, j) for j in range(3)]
    if "W" in toward:
        declared += [(f"W:{j}", 0, j) for j in range(3)]
    if "N" in toward:
        declared += [(f"N:{i}", i, 2) for i in range(3)]
    if "S" in toward:
        declared += [(f"S:{i}", i, 0) for i in range(3)]

    by_coord: dict = {}
    for key, i, j in declared:
        coord = (round(ox + i * half, 6), round(oy + j * half, 6))
        by_coord.setdefault(coord, {"keys": [], "i": i, "j": j})
        by_coord[coord]["keys"].append(key)

    for (x, y), info in by_coord.items():
        key = "|".join(info["keys"])
        dx, dy = (port_shift or {}).get(key, (0.0, 0.0))
        out_l, in_l = (port_lanes or {}).get(key, (1, 1))
        ports.append(BoundaryPort(
            port_key=key, x=x + dx, y=y + dy,
            node_id=nid(info["i"], info["j"]), out_lanes=out_l, in_lanes=in_l,
            speed_limit_kph=speed_limit_kph))

    return TileData(
        tile_id=tile_id, region_id=region_id, version=version,
        bbox=(ox, oy, ox + size, oy + size),
        nodes=nodes, edges=edges, ports=ports,
        rules={},
    )


def make_city(region_of: dict, size: float = DEFAULT_SIZE_M) -> list:
    """生成 2x2 城市全部初始瓦片（v1）。region_of: tile_id -> region_id"""
    return [
        make_grid_tile(tid, region_of[tid], *CITY_POS[tid], version=1,
                       size=size, toward=CITY_TOWARD[tid])
        for tid in CITY_POS
    ]
