"""路线快照：固定瓦片集合、禁止拼接、整体切换、离线追赶。"""
from mapsvc import PatchOp
from mapsvc.routing import build_merged_map, shortest_path
from conftest import edit_tile


def assert_snapshot_consistent(svc, snap):
    """不变量：快照固定的瓦片版本必须与其基线代际记录一致（无拼接）。"""
    for region_id, gen in snap.base.items():
        rec = svc.store.get_generation(region_id, gen)
        for tile_id, ver in snap.tiles.items():
            if tile_id in rec.tile_versions:
                assert rec.tile_versions[tile_id] == ver, (
                    f"快照 {snap.snapshot_id} 中 {tile_id}@v{ver} "
                    f"与基线 {region_id}@g{gen} 不一致")


def publish_edit(svc, tile_id, **kwargs):
    edit = edit_tile(svc, tile_id, **kwargs)
    cand = svc.publish_engine.create_candidate(f"{tile_id}-edit", [edit])
    svc.publish_engine.validate(cand.candidate_id)
    svc.publish_engine.publish(cand.candidate_id)


def test_snapshot_pins_tile_set(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    assert snap.base == {"south": 1, "north": 1}
    assert snap.tiles                      # 固定了所用瓦片集合
    assert snap.route["tiles_used"]
    assert set(snap.tiles) == set(snap.route["tiles_used"])
    assert_snapshot_consistent(svc, snap)


def test_publish_does_not_disturb_active_snapshot(svc):
    """途中发布：车辆快照保持不变，新旧拓扑绝不拼接。"""
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    publish_edit(svc, "t00", speed_limit_kph=30.0)
    assert svc.store.current_generation("south") == 2

    # 快照仍是单一旧版本，且其基线代际仍可完整解析
    kept = svc.store.get_snapshot(snap.snapshot_id)
    assert kept.status == "ACTIVE"
    assert kept.base == {"south": 1, "north": 1}
    assert kept.tiles.get("t00") == 1
    mm = build_merged_map(svc.store, kept.base, [])
    route = shortest_path(mm, (10, 10), (190, 190))
    assert route["edges"]                  # 旧版本上路线依然可用
    assert_snapshot_consistent(svc, kept)


def test_refresh_switches_whole_snapshot(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    publish_edit(svc, "t00", speed_limit_kph=30.0)

    new_snap = svc.snapshots.refresh(snap.snapshot_id, at_xy=[60, 10])
    old = svc.store.get_snapshot(snap.snapshot_id)
    assert old.status == "SUPERSEDED"
    assert new_snap.status == "ACTIVE"
    # 整体切换：新快照基线全部是最新代际
    assert new_snap.base == {"south": 2, "north": 1}
    assert new_snap.route["src_xy"] == [60, 10]
    assert new_snap.route["dst_xy"] == [190, 190]
    assert new_snap.lineage == [snap.snapshot_id]
    assert_snapshot_consistent(svc, new_snap)


def test_refresh_noop_when_nothing_changed(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    same = svc.snapshots.refresh(snap.snapshot_id, at_xy=[60, 10])
    assert same.snapshot_id == snap.snapshot_id


def test_patch_transition_for_affected_vehicle(svc):
    """高优先级补丁：受影响车辆获得同基线+补丁的整体过渡快照。"""
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    tile_id, edge_id = snap.route["edges"][0].split(":")
    res = svc.patches.create_patch(
        "紧急封路", [PatchOp(kind="CLOSE_EDGE", tile_id=tile_id,
                             edge_id=edge_id,
                             params={"both_directions": True})],
        priority=300)
    assert res["transitions"], "应生成过渡方案"
    tr = res["transitions"][0]
    assert tr["from_snapshot"] == snap.snapshot_id and tr["ok"]

    new_snap = svc.store.get_snapshot(tr["to_snapshot"])
    assert new_snap.reason == "patch_transition"
    assert new_snap.base == snap.base             # 同基线
    assert len(new_snap.patches) == 1             # 加补丁，单一可解释
    # 新路线避开被封的边
    assert f"{tile_id}:{edge_id}" not in new_snap.route["edges"]
    old = svc.store.get_snapshot(snap.snapshot_id)
    assert old.status == "SUPERSEDED"
    assert_snapshot_consistent(svc, new_snap)


def test_unaffected_vehicle_untouched_by_patch(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    used = set(snap.route["edges"])
    # 找一条不在路线上的边来封
    victim = next(
        e for e in svc.store.get_tile("t11", 1).edges
        if f"t11:{e.edge_id}" not in used)
    res = svc.patches.create_patch(
        "远处封路", [PatchOp(kind="CLOSE_EDGE", tile_id="t11",
                             edge_id=victim.edge_id)], priority=300)
    assert res["transitions"] == []
    assert svc.store.get_snapshot(snap.snapshot_id).status == "ACTIVE"


def test_catchup_gives_single_target_version(svc):
    """离线追赶：跳过中间所有变更，一次性切到当前有效版本。"""
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    # 车辆离线期间：一次发布 + 一个补丁（补丁随后撤销）
    publish_edit(svc, "t00", speed_limit_kph=30.0)
    publish_edit(svc, "t01", speed_limit_kph=45.0)
    res = svc.patches.create_patch(
        "离线期间封路", [PatchOp(kind="CLOSE_EDGE", tile_id="t10",
                                 edge_id="e_n00_n10")], priority=100)
    svc.patches.revoke_patch(res["patch"]["patch_id"])

    out = svc.snapshots.catchup("V1", snap.snapshot_id, at_xy=[60, 10])
    assert out["action"] == "switch"
    target = out["to"]
    # 单一目标版本 = 当前有效版本（不追中间态）
    assert target["base"] == svc.effective_version()["base"]
    assert target["patches"] == []
    assert target["reason"] == "catchup"
    assert target["lineage"] == [snap.snapshot_id]
    old = svc.store.get_snapshot(snap.snapshot_id)
    assert old.status == "SUPERSEDED"


def test_catchup_noop_when_current(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    out = svc.snapshots.catchup("V1", snap.snapshot_id, at_xy=[60, 10])
    assert out["action"] == "none"


def test_explain_snapshot_is_self_contained(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    ex = svc.explain_snapshot(snap)
    # 单一可解释版本：基线 + 瓦片 + 补丁 + 路线 + 解释链齐备
    assert ex["base"] == {"south": 1, "north": 1}
    assert ex["tiles"] and ex["route"]["length_m"] > 0
    assert ex["patches"] == []
