#!/usr/bin/env python3
"""向运行中的服务灌入演示数据（供审查页查看）。

用法：先启动 API（uvicorn mapsvc.api:app --port 8000），再运行
      python3 scripts/seed.py [http://127.0.0.1:8000]
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mapsvc.gridmap import CITY_POS, CITY_TOWARD, make_city, make_grid_tile

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
REGION_OF = {"t00": "south", "t10": "south", "t01": "north", "t11": "north"}


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    for r in ("south", "north"):
        call("POST", "/api/regions", {"region_id": r})

    # 初始城市
    tiles = [t.to_dict() for t in make_city(REGION_OF)]
    cand = call("POST", "/api/candidates", {"name": "初始城市 v1", "edits": tiles})
    call("POST", f"/api/candidates/{cand['candidate_id']}/validate", {})
    call("POST", f"/api/candidates/{cand['candidate_id']}/publish")
    print("初始城市已发布")

    # 一次限速编辑
    edit = make_grid_tile("t00", "south", *CITY_POS["t00"], version=2,
                          toward=CITY_TOWARD["t00"], speed_limit_kph=30.0)
    cand2 = call("POST", "/api/candidates",
                 {"name": "t00 限速 30", "edits": [edit.to_dict()]})
    call("POST", f"/api/candidates/{cand2['candidate_id']}/validate", {})
    call("POST", f"/api/candidates/{cand2['candidate_id']}/publish")
    print("t00@v2 已发布（south g2）")

    # 一个验证失败的坏编辑（展示接缝错误高亮）
    bad = make_grid_tile("t10", "south", *CITY_POS["t10"], version=2,
                         toward=CITY_TOWARD["t10"],
                         port_shift={"W:1": (0.0, 10.0)})
    cand3 = call("POST", "/api/candidates",
                 {"name": "错位编辑（演示验证失败）", "edits": [bad.to_dict()]})
    rep = call("POST", f"/api/candidates/{cand3['candidate_id']}/validate", {})
    print(f"坏编辑验证: ok={rep['ok']}，{len(rep['issues'])} 个问题")

    # 两辆车的路线快照
    call("POST", "/api/routes",
         {"vehicle_id": "V1", "src_xy": [10, 10], "dst_xy": [190, 190]})
    call("POST", "/api/routes",
         {"vehicle_id": "V2", "src_xy": [190, 10], "dst_xy": [10, 190]})
    print("V1/V2 路线快照已固定")

    # 紧急封路补丁
    res = call("POST", "/api/patches", {
        "title": "t00 北侧事故紧急封路", "priority": 300,
        "ops": [{"kind": "CLOSE_EDGE", "tile_id": "t00",
                 "edge_id": "e_n01_n02",
                 "params": {"both_directions": True}}]})
    print(f"补丁 {res['patch']['patch_id']} 生效，"
          f"过渡 {len(res['transitions'])} 辆车")
    print(f"\n打开审查页: {BASE}/review")


if __name__ == "__main__":
    main()
