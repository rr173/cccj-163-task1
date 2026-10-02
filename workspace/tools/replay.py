#!/usr/bin/env python3
"""路线复现工具：重放行程事件流，逐步校验版本一致性不变量。

用法：
    python3 tools/replay.py scenarios/trip_demo.jsonl
    python3 tools/replay.py scenarios/trip_demo.jsonl --json

事件（JSONL，每行一个）：
    {"op":"bootstrap"}                                   建初始 2x2 城市并发布
    {"op":"route","vehicle":"V1","src":[x,y],"dst":[x,y]}
    {"op":"edit","tile":"t00","speed_limit_kph":30}      候选->验证->发布
    {"op":"edit","tile":"t10","port_shift":{"W:1":[0,10]},"expect_fail":true}
    {"op":"patch","title":"封路","tile":"t00","edge":"e_n00_n01","priority":300}
    {"op":"revoke_last_patch":true}  /  {"op":"revoke","patch_id":"..."}
    {"op":"offline","vehicle":"V1"} {"op":"online","vehicle":"V1","at":[x,y]}
    {"op":"refresh","vehicle":"V1","at":[x,y]}
    {"op":"close","vehicle":"V1"}

校验的不变量：
    I1 无拼接：车辆每个状态都是单一快照，瓦片版本与基线代际记录一致；
    I2 每辆车同一时刻至多一个 ACTIVE 快照；
    I3 快照路线在其版本（基线+补丁）合成图上连通；
    I4 离线追赶/切换后，车辆版本 == 当时有效版本（单一目标）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mapsvc import MapService, PatchOp, Store
from mapsvc.gridmap import CITY_POS, CITY_TOWARD, make_city, make_grid_tile
from mapsvc.routing import build_merged_map

REGION_OF = {"t00": "south", "t10": "south", "t01": "north", "t11": "north"}


class Replay:
    def __init__(self):
        self.svc = MapService(Store(":memory:"))
        self.vehicles = {}          # vehicle -> 车辆持有的快照 id（客户端视角）
        self.offline = set()
        self.last_patch = None
        self.violations = []
        self.log = []

    # ------------------------------------------------------------ 工具

    def say(self, msg):
        self.log.append(msg)

    def fail(self, msg):
        self.violations.append(msg)
        self.say(f"    ❌ 不变量违例: {msg}")

    def check_invariants(self, when):
        svc = self.svc
        # I2: 每辆车至多一个 ACTIVE 快照
        for vid in {s.vehicle_id for s in svc.store.list_snapshots()}:
            act = [s for s in svc.store.list_snapshots(vehicle_id=vid)
                   if s.status == "ACTIVE"]
            if len(act) > 1:
                self.fail(f"[{when}] I2 {vid} 有 {len(act)} 个 ACTIVE 快照")
        # I1 + I3: 车辆持有的快照自洽且路线连通
        for vid, sid in self.vehicles.items():
            if vid in self.offline:
                continue
            snap = svc.store.get_snapshot(sid)
            if snap.status != "ACTIVE":
                self.fail(f"[{when}] I1 {vid} 持有的快照 {sid} 非 ACTIVE "
                          f"({snap.status})，车辆失去单一版本")
                continue
            for region_id, gen in snap.base.items():
                rec = svc.store.get_generation(region_id, gen)
                for tile_id, ver in snap.tiles.items():
                    if tile_id in rec.tile_versions and rec.tile_versions[tile_id] != ver:
                        self.fail(f"[{when}] I1 {vid} 快照拼接 detected: "
                                  f"{tile_id}@v{ver} 不属于 {region_id}@g{gen}")
            patches = [svc.store.get_patch(pid) for pid in snap.patches]
            mm = build_merged_map(svc.store, snap.base, patches)
            if not self._route_connected(mm, snap):
                self.fail(f"[{when}] I3 {vid} 快照路线在其版本上不连通")

    @staticmethod
    def _route_connected(mm, snap) -> bool:
        adj = mm.adj
        nodes = [tuple(n) for n in snap.route.get("nodes", [])]
        if not nodes:
            return False
        for a, b in zip(nodes, nodes[1:]):
            if not any(nbr == b for nbr, _c, _r in adj.get(a, [])):
                return False
        return True

    # ------------------------------------------------------------ 事件

    def op_bootstrap(self, ev):
        self.svc.store.register_region("south")
        self.svc.store.register_region("north")
        cand = self.svc.publish_engine.create_candidate(
            "初始城市", make_city(REGION_OF))
        rep = self.svc.publish_engine.validate(cand.candidate_id)
        assert rep["ok"], rep["issues"]
        self.svc.publish_engine.publish(cand.candidate_id)
        self.say(f"  建图完成: {self.svc.effective_version()['base']}")

    def op_route(self, ev):
        v = ev["vehicle"]
        snap = self.svc.snapshots.request_route(v, ev["src"], ev["dst"])
        self.vehicles[v] = snap.snapshot_id
        self.say(f"  {v} 路线 {snap.route['length_m']}m "
                 f"快照 {snap.snapshot_id} base={snap.base}")

    def op_edit(self, ev):
        tid = ev["tile"]
        ver = self.svc.store.latest_tile_version(tid) + 1
        kw = {}
        if "speed_limit_kph" in ev:
            kw["speed_limit_kph"] = ev["speed_limit_kph"]
        if "port_shift" in ev:
            kw["port_shift"] = {k: tuple(v) for k, v in ev["port_shift"].items()}
        if "edge_drops" in ev:
            kw["edge_drops"] = set(ev["edge_drops"])
        tile = make_grid_tile(tid, REGION_OF[tid], *CITY_POS[tid], version=ver,
                              toward=CITY_TOWARD[tid], **kw)
        cand = self.svc.publish_engine.create_candidate(
            ev.get("name", f"编辑 {tid}@v{ver}"), [tile])
        rep = self.svc.publish_engine.validate(cand.candidate_id)
        if ev.get("expect_fail"):
            if rep["ok"]:
                self.fail(f"编辑 {tid} 预期验证失败但却通过")
                return
            try:
                self.svc.publish_engine.publish(cand.candidate_id)
                self.fail(f"编辑 {tid} 验证失败却被发布")
            except Exception:
                self.say(f"  坏编辑被拦截（验证 {len(rep['issues'])} 个问题，"
                         f"线上版本不变）")
            return
        if not rep["ok"]:
            self.fail(f"编辑 {tid} 意外验证失败: "
                      f"{[i['code'] for i in rep['issues']]}")
            return
        self.svc.publish_engine.publish(cand.candidate_id)
        self.say(f"  发布 {tid}@v{ver}: {self.svc.effective_version()['base']}")

    def op_patch(self, ev):
        ops = [PatchOp(kind="CLOSE_EDGE", tile_id=ev["tile"], edge_id=ev["edge"],
                       params={"both_directions": True})]
        res = self.svc.patches.create_patch(
            ev.get("title", "紧急封路"), ops, priority=ev.get("priority", 200))
        self.last_patch = res["patch"]["patch_id"]
        for tr in res["transitions"]:
            if tr["ok"]:
                self.vehicles[tr["vehicle_id"]] = tr["to_snapshot"]
                self.say(f"  补丁过渡: {tr['vehicle_id']} -> {tr['to_snapshot']}")
            else:
                self.say(f"  补丁过渡失败（保留旧快照）: {tr['vehicle_id']}")
        self.say(f"  补丁 {self.last_patch} 生效")

    def op_revoke(self, ev):
        pid = ev.get("patch_id") or self.last_patch
        out = self.svc.patches.revoke_patch(pid)
        for rb in out["rebased_snapshots"]:
            if rb["to"]:
                self.vehicles[rb["vehicle_id"]] = rb["to"]
        self.say(f"  补丁 {pid} 已撤销，重定基线 {len(out['rebased_snapshots'])} 个快照")

    def op_offline(self, ev):
        self.offline.add(ev["vehicle"])
        self.say(f"  {ev['vehicle']} 离线")

    def op_online(self, ev):
        v = ev["vehicle"]
        self.offline.discard(v)
        out = self.svc.snapshots.catchup(v, self.vehicles[v], ev["at"])
        self.say(f"  {v} 重连追赶: {out['action']}")
        if out["action"] == "switch":
            self.vehicles[v] = out["to"]["snapshot_id"]
            # I4: 追赶后车辆版本必须等于当前有效版本
            ver = self.svc.effective_version()
            to_patches = [p["patch_id"] for p in out["to"]["patches"]]
            if out["to"]["base"] != ver["base"] or to_patches != ver["patches"]:
                self.fail(f"I4 {v} 追赶目标不是单一当前有效版本")
        elif out["action"] == "none":
            ver = self.svc.effective_version()
            snap = self.svc.store.get_snapshot(self.vehicles[v])
            if snap.base != ver["base"] or snap.patches != ver["patches"]:
                self.fail(f"I4 {v} 无需动作但版本与有效版本不符")

    def op_refresh(self, ev):
        v = ev["vehicle"]
        snap = self.svc.snapshots.refresh(self.vehicles[v], ev["at"])
        self.vehicles[v] = snap.snapshot_id
        self.say(f"  {v} 过渡点切换 -> {snap.snapshot_id} base={snap.base}")
        ver = self.svc.effective_version()
        if snap.base != ver["base"]:
            self.fail(f"I4 {v} 切换后基线不等于当前有效版本")

    def op_close(self, ev):
        v = ev["vehicle"]
        self.svc.snapshots.close(self.vehicles.pop(v))
        self.say(f"  {v} 行程结束")

    # ------------------------------------------------------------ 主流程

    def run(self, events):
        for i, ev in enumerate(events):
            op = ev["op"].replace("_last_patch", "")
            self.say(f"[t={ev.get('t', i)}] {ev['op']} "
                     f"{ev.get('vehicle', ev.get('tile', ''))}")
            handler = getattr(self, f"op_{op}", None)
            if handler is None:
                self.fail(f"未知事件: {ev['op']}")
                continue
            try:
                handler(ev)
            except Exception as exc:
                self.fail(f"事件 {ev['op']} 执行异常: {exc}")
            self.check_invariants(f"t={ev.get('t', i)}")
        return self


def main():
    ap = argparse.ArgumentParser(description="路线复现工具")
    ap.add_argument("scenario", help="JSONL 场景文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = ap.parse_args()

    with open(args.scenario, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()
                  and not line.startswith("#")]

    replay = Replay().run(events)

    report = {
        "events": len(events),
        "violations": replay.violations,
        "ok": not replay.violations,
        "log": replay.log,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("\n".join(replay.log))
        print("-" * 60)
        if replay.violations:
            print(f"复现完成：{len(events)} 个事件，"
                  f"发现 {len(replay.violations)} 个不变量违例 ❌")
            for v in replay.violations:
                print(f"  ❌ {v}")
        else:
            print(f"复现完成：{len(events)} 个事件，全部不变量通过 ✅")
    sys.exit(0 if not replay.violations else 1)


if __name__ == "__main__":
    main()
