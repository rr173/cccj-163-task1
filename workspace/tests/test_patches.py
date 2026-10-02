"""紧急补丁：兼容性、限制类约束、撤销、安全过渡。"""
import pytest

from mapsvc import PatchOp, ServiceError


def close_op(tile_id="t00", edge_id="e_n00_n10"):
    return PatchOp(kind="CLOSE_EDGE", tile_id=tile_id, edge_id=edge_id,
                   params={"both_directions": True})


def test_create_closure_patch(svc):
    res = svc.patches.create_patch("事故封路", [close_op()], priority=200)
    assert res["patch"]["status"] == "ACTIVE"
    assert res["patch"]["base"] == {"south": 1, "north": 1}


def test_non_restrictive_op_rejected(svc):
    op = PatchOp(kind="ADD_EDGE", tile_id="t00", edge_id="e_new")
    with pytest.raises(ServiceError, match="限制类"):
        svc.patches.create_patch("非法补丁", [op])


def test_unknown_edge_rejected(svc):
    op = PatchOp(kind="CLOSE_EDGE", tile_id="t00", edge_id="e_ghost")
    with pytest.raises(ServiceError, match="不存在"):
        svc.patches.create_patch("坏引用", [op])


def test_tile_outside_base_rejected(svc):
    op = PatchOp(kind="CLOSE_EDGE", tile_id="t99", edge_id="e_n00_n10")
    with pytest.raises(ServiceError, match="不在补丁基线"):
        svc.patches.create_patch("越界", [op])


def test_lane_mask_overflow_rejected(svc):
    op = PatchOp(kind="CLOSE_LANE", tile_id="t00", edge_id="e_n00_n10",
                 params={"lane_mask": 0b10})   # 该边只有 1 条车道
    with pytest.raises(ServiceError, match="车道掩码"):
        svc.patches.create_patch("掩码越界", [op])


def test_patch_changes_routing(svc):
    from mapsvc.routing import build_merged_map, shortest_path, RouteError
    ver = svc.effective_version()
    # 封掉 t00 内全部南北向边后，(10,10)->(10,90) 直连被切断
    ops = [PatchOp(kind="CLOSE_EDGE", tile_id="t00", edge_id=e,
                   params={"both_directions": True})
           for e in ("e_n00_n01", "e_n01_n02")]
    svc.patches.create_patch("封南北", ops)
    patches = svc.active_patch_objs()
    mm = build_merged_map(svc.store, ver["base"], patches)
    # x=10 最近节点是 n00 列；直行边被封后需绕行，代价变大
    route = shortest_path(mm, (10, 10), (10, 90))
    assert route["length_m"] > 100.0   # 直行 100m 变为绕行


def test_revoke_restores_and_rebases(svc):
    snap = svc.snapshots.request_route("V1", [10, 10], [190, 190])
    # 关闭路线实际经过的第一条边，确保补丁影响到该快照
    tile_id, edge_id = snap.route["edges"][0].split(":")
    res = svc.patches.create_patch(
        "临时封路", [close_op(tile_id, edge_id)], priority=200)
    pid = res["patch"]["patch_id"]
    assert res["transitions"] and res["transitions"][0]["ok"]

    out = svc.patches.revoke_patch(pid)
    assert out["status"] == "REVOKED"
    # 撤销后有效版本不含任何补丁
    assert svc.effective_version()["patches"] == []
    # 受影响快照被整体重定基线（单一版本，无拼接）
    assert out["rebased_snapshots"]
    cur = [s for s in svc.store.list_snapshots(vehicle_id="V1")
           if s.status == "ACTIVE"]
    assert len(cur) == 1
    assert cur[0].patches == []


def test_revoke_twice_rejected(svc):
    res = svc.patches.create_patch("p", [close_op()])
    pid = res["patch"]["patch_id"]
    svc.patches.revoke_patch(pid)
    with pytest.raises(ServiceError):
        svc.patches.revoke_patch(pid)
