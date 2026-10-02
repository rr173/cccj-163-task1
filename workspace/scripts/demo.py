#!/usr/bin/env python3
"""端到端演示：从建图、验证、发布，到车辆快照、紧急补丁、离线追赶。

运行： python3 scripts/demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mapsvc import MapService, PatchOp, Store
from mapsvc.gridmap import CITY_POS, CITY_TOWARD, make_city, make_grid_tile

REGION_OF = {"t00": "south", "t10": "south", "t01": "north", "t11": "north"}


def hr(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def show_version(svc, label):
    ver = svc.effective_version()
    print(f"  [{label}] 有效版本: 区域代际={ver['base']} 生效补丁={ver['patches']}")


def main():
    svc = MapService(Store(":memory:"))
    svc.store.register_region("south")
    svc.store.register_region("north")

    hr("1. 初始建图：2x2 瓦片城市（south/north 两区域），候选 -> 验证 -> 发布")
    tiles = make_city(REGION_OF)
    cand = svc.publish_engine.create_candidate("初始城市 v1", tiles)
    report = svc.publish_engine.validate(cand.candidate_id)
    print(f"  验证: ok={report['ok']} 接缝对={report['seam']['adjacent_pairs_checked']}"
          f" 配对端口={report['seam']['ports_paired']}"
          f" 跨区检查={report['cross_region']['passed']}/{report['cross_region']['total']}")
    svc.publish_engine.publish(cand.candidate_id)
    show_version(svc, "发布后")

    hr("2. 车辆 V1 请求路线 -> 固定路线快照（瓦片集合钉在 g1）")
    snap_v1 = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    print(f"  快照 {snap_v1.snapshot_id}")
    print(f"  路线: {snap_v1.route['length_m']}m 经过瓦片={snap_v1.route['tiles_used']}")
    print(f"  固定: base={snap_v1.base} tiles={snap_v1.tiles}")

    hr("3. 地图编辑：t00 限速调整 -> 候选 v2 -> 验证 -> 按区域代际发布")
    edit = make_grid_tile("t00", "south", *CITY_POS["t00"], version=2,
                          toward=CITY_TOWARD["t00"], speed_limit_kph=30.0)
    cand2 = svc.publish_engine.create_candidate("t00 限速 30", [edit])
    svc.publish_engine.validate(cand2.candidate_id)
    svc.publish_engine.publish(cand2.candidate_id)
    show_version(svc, "south 升到 g2")
    kept = svc.store.get_snapshot(snap_v1.snapshot_id)
    print(f"  V1 快照未受影响（禁止拼接）: base={kept.base} tiles={kept.tiles}")

    hr("4. 坏编辑被拦截：t10 端口错位 -> 验证失败 -> 禁止发布，线上版本不变")
    bad = make_grid_tile("t10", "south", *CITY_POS["t10"], version=2,
                         toward=CITY_TOWARD["t10"],
                         port_shift={"W:1": (0.0, 10.0)})
    cand3 = svc.publish_engine.create_candidate("错位编辑", [bad])
    report3 = svc.publish_engine.validate(cand3.candidate_id)
    print(f"  验证: ok={report3['ok']}")
    for issue in report3["issues"][:2]:
        print(f"    [{issue['severity']}] {issue['message']}")
    try:
        svc.publish_engine.publish(cand3.candidate_id)
    except Exception as exc:
        print(f"  发布被拒绝: {exc}")
    show_version(svc, "线上保持")

    hr("5. 紧急封路：高优先级补丁 -> 兼容性校验 -> 受影响车辆安全过渡")
    victim = snap_v1.route["edges"][0]
    tile_id, edge_id = victim.split(":")
    print(f"  封闭 {victim}（V1 路线第一段）")
    res = svc.patches.create_patch(
        "前方事故紧急封路",
        [PatchOp(kind="CLOSE_EDGE", tile_id=tile_id, edge_id=edge_id,
                 params={"both_directions": True})],
        priority=300)
    for tr in res["transitions"]:
        print(f"  车辆 {tr['vehicle_id']}: 快照 {tr['from_snapshot']} -> "
              f"{tr['to_snapshot']} (patch_transition)")
    new_snap = svc.store.get_snapshot(res["transitions"][0]["to_snapshot"])
    print(f"  过渡后路线避开封路段: {victim not in new_snap.route['edges']}")
    print(f"  单一可解释: base={new_snap.base} patches={new_snap.patches}")

    hr("6. 车辆 V2 离线后重连 -> 一次性追赶到单一目标版本")
    snap_v2 = svc.snapshots.request_route("V2", [10, 10], [190, 190])
    print(f"  V2 离线前快照: base={snap_v2.base} patches={snap_v2.patches}")
    # V2 离线期间：又一次发布 + 补丁撤销
    edit2 = make_grid_tile("t01", "north", *CITY_POS["t01"], version=2,
                           toward=CITY_TOWARD["t01"], speed_limit_kph=45.0)
    cand4 = svc.publish_engine.create_candidate("t01 限速 45", [edit2])
    svc.publish_engine.validate(cand4.candidate_id)
    svc.publish_engine.publish(cand4.candidate_id)
    svc.patches.revoke_patch(res["patch"]["patch_id"])
    show_version(svc, "V2 离线期间线上变为")
    out = svc.snapshots.catchup("V2", snap_v2.snapshot_id, at_xy=[60, 10])
    print(f"  追赶动作: {out['action']}")
    if out["action"] == "switch":
        print(f"  目标版本: base={out['to']['base']} patches={out['to']['patches']}"
              f" reason={out['to']['reason']}")
        print("  （不追中间增量，直接给出单一目标版本）")

    hr("7. 审查页数据快照")
    snaps = svc.store.list_snapshots()
    for s in snaps:
        print(f"  {s.vehicle_id} {s.snapshot_id} {s.status:10s}"
              f" reason={s.reason:16s} base={s.base} patches={s.patches}")
    print("\n演示完成。启动 API: uvicorn mapsvc.api:app --port 8000"
          " 后访问 /review 查看地图审查页。")


if __name__ == "__main__":
    main()
