"""API 集成测试：候选->验证->发布->路线->补丁->追赶 全链路。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mapsvc.gridmap import CITY_POS, CITY_TOWARD, make_city, make_grid_tile

REGION_OF = {"t00": "south", "t10": "south", "t01": "north", "t11": "north"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSVC_DB", str(tmp_path / "test.db"))
    # 弹出后重新 import，使模块级服务使用临时库
    sys.modules.pop("mapsvc.api", None)
    import mapsvc.api  # noqa: F401 — 重新执行模块，重建服务
    yield TestClient(sys.modules["mapsvc.api"].app)


def bootstrap(client):
    for r in ("south", "north"):
        assert client.post("/api/regions", json={"region_id": r}).status_code == 200
    tiles = [t.to_dict() for t in make_city(REGION_OF)]
    cand = client.post("/api/candidates",
                       json={"name": "init", "edits": tiles}).json()
    rep = client.post(f"/api/candidates/{cand['candidate_id']}/validate",
                      json={}).json()
    assert rep["ok"]
    out = client.post(f"/api/candidates/{cand['candidate_id']}/publish")
    assert out.status_code == 200
    return cand


def test_full_flow_over_http(client):
    bootstrap(client)

    # 有效版本
    regions = client.get("/api/regions").json()
    assert regions["effective_version"]["base"] == {"south": 1, "north": 1}

    # 车辆路线 -> 快照
    route = client.post("/api/routes", json={
        "vehicle_id": "V1", "src_xy": [10, 10], "dst_xy": [190, 190]}).json()
    assert route["base"] == {"south": 1, "north": 1}
    assert route["tiles"]
    sid = route["snapshot_id"]

    # 发布新版本后快照不变（禁止拼接）
    edit = make_grid_tile("t00", "south", *CITY_POS["t00"], version=2,
                          toward=CITY_TOWARD["t00"], speed_limit_kph=30.0)
    cand = client.post("/api/candidates",
                       json={"name": "v2", "edits": [edit.to_dict()]}).json()
    client.post(f"/api/candidates/{cand['candidate_id']}/validate", json={})
    client.post(f"/api/candidates/{cand['candidate_id']}/publish")
    kept = client.get(f"/api/snapshots/{sid}").json()
    assert kept["base"] == {"south": 1, "north": 1}

    # 紧急补丁：关闭路线第一段 -> 自动过渡
    first_edge = client.get(f"/api/snapshots/{sid}").json()
    # 从审查数据里拿路线边（explain 不含 edges，用 tile 版本查）
    # 直接封 t00 内 n00->n01（V1 从 (10,10) 出发必经其一，两段都封更稳）
    res = client.post("/api/patches", json={
        "title": "事故封路", "priority": 300,
        "ops": [{"kind": "CLOSE_EDGE", "tile_id": "t00",
                 "edge_id": "e_n00_n01",
                 "params": {"both_directions": True}}]}).json()
    assert res["patch"]["status"] == "ACTIVE"

    # 离线追赶
    out = client.post("/api/vehicles/V1/catchup",
                      json={"snapshot_id": sid, "at_xy": [60, 10]}).json()
    assert out["action"] in ("switch", "none")

    # 撤销补丁
    pid = res["patch"]["patch_id"]
    rev = client.post(f"/api/patches/{pid}/revoke").json()
    assert rev["status"] == "REVOKED"

    # 审查页与聚合数据
    assert client.get("/review").status_code == 200
    state = client.get("/api/review/state").json()
    assert state["regions"] and state["tiles"]


def test_publish_rejected_when_invalid(client):
    bootstrap(client)
    bad = make_grid_tile("t10", "south", *CITY_POS["t10"], version=2,
                         toward=CITY_TOWARD["t10"],
                         port_shift={"W:1": (0.0, 10.0)})
    cand = client.post("/api/candidates",
                       json={"name": "bad", "edits": [bad.to_dict()]}).json()
    client.post(f"/api/candidates/{cand['candidate_id']}/validate", json={})
    resp = client.post(f"/api/candidates/{cand['candidate_id']}/publish")
    assert resp.status_code == 409
    # 线上代际不变
    regions = client.get("/api/regions").json()
    assert regions["effective_version"]["base"] == {"south": 1, "north": 1}


def test_incompatible_patch_rejected(client):
    bootstrap(client)
    resp = client.post("/api/patches", json={
        "title": "非法", "ops": [{"kind": "ADD_EDGE", "tile_id": "t00",
                                  "edge_id": "e_x"}]})
    assert resp.status_code == 409
