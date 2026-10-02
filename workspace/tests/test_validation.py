"""接缝验证与跨区路线验证。"""
from conftest import edit_tile


def test_seam_ok_on_bootstrap(svc):
    cand = svc.store.list_candidates()[0]
    assert cand.validation["ok"]
    assert cand.validation["seam"]["adjacent_pairs_checked"] == 4
    errors = [i for i in cand.validation["issues"] if i["severity"] == "ERROR"]
    assert errors == []


def test_seam_port_offset_rejected(svc):
    # t10 的 W:1 端口偏移 10m（容差 2m）-> 两侧 dangling
    bad = edit_tile(svc, "t10", port_shift={"W:1": (0.0, 10.0)})
    cand = svc.publish_engine.create_candidate("bad-offset", [bad])
    report = svc.publish_engine.validate(cand.candidate_id)
    assert not report["ok"]
    codes = {i["code"] for i in report["issues"]}
    assert "SEAM_DANGLING_PORT" in codes


def test_seam_lane_mismatch_rejected(svc):
    # t00 东侧端口 2 出 1 入，t10 西侧仍 1/1 -> 车道数不匹配
    bad = edit_tile(svc, "t00", port_lanes={"E:1": (2, 1)})
    cand = svc.publish_engine.create_candidate("bad-lanes", [bad])
    report = svc.publish_engine.validate(cand.candidate_id)
    assert not report["ok"]
    codes = {i["code"] for i in report["issues"]}
    assert "SEAM_LANE_MISMATCH" in codes


def test_version_skew_rejected(svc):
    # 基于过期基线的编辑（版本号不连续）必须拒绝
    from mapsvc import TileData
    stale = edit_tile(svc, "t00")
    stale = TileData.from_dict({**stale.to_dict(), "version": 5})
    cand = svc.publish_engine.create_candidate("stale", [stale])
    report = svc.publish_engine.validate(cand.candidate_id)
    assert not report["ok"]
    codes = {i["code"] for i in report["issues"]}
    assert "VERSION_SKEW" in codes


def test_cross_region_break_detected(svc):
    # 移除 north 区域全部内部车道边（接缝端口不变）：
    # south 的车辆能跨过边界却无法深入 north 内部 -> 锚点不可达
    drops = []
    for tid in ("t01", "t11"):
        tile = svc.store.get_tile(tid, 1)
        drops.append(edit_tile(svc, tid,
                               edge_drops={e.edge_id for e in tile.edges}))
    cand = svc.publish_engine.create_candidate("break-north", drops)
    report = svc.publish_engine.validate(cand.candidate_id)
    assert not report["ok"]
    codes = {i["code"] for i in report["issues"]}
    assert "CROSS_REGION_UNREACHABLE" in codes
    failed = [c for c in report["cross_region"]["checks"] if not c["ok"]]
    assert any("south" in c["regions"] and "north" in c["regions"]
               for c in failed)


def test_extra_od_pairs_checked(svc):
    ok_tile = edit_tile(svc, "t00")
    cand = svc.publish_engine.create_candidate("od", [ok_tile])
    report = svc.publish_engine.validate(
        cand.candidate_id,
        extra_od_pairs=[{"name": "corner2corner",
                         "src": [10, 10], "dst": [190, 190]}])
    assert report["ok"]
    custom = [c for c in report["cross_region"]["checks"] if c.get("custom")]
    assert custom and custom[0]["ok"]
