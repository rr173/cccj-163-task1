"""发布引擎：按区域代际、原子性、失败回滚。"""
import pytest

from mapsvc import ServiceError
from conftest import edit_tile


def test_publish_bumps_generation_per_region(svc):
    assert svc.store.current_generation("south") == 1
    assert svc.store.current_generation("north") == 1

    edit = edit_tile(svc, "t10", speed_limit_kph=40.0)
    cand = svc.publish_engine.create_candidate("t10-v2", [edit])
    report = svc.publish_engine.validate(cand.candidate_id)
    assert report["ok"]
    svc.publish_engine.publish(cand.candidate_id)

    # 只涉及 south：south 升到 g2，north 保持 g1 —— 按区域代际
    assert svc.store.current_generation("south") == 2
    assert svc.store.current_generation("north") == 1
    rec = svc.store.get_generation("south", 2)
    assert rec.tile_versions["t10"] == 2
    assert rec.tile_versions["t00"] == 1   # 未编辑瓦片沿用旧版本


def test_publish_requires_validation(svc):
    edit = edit_tile(svc, "t00")
    cand = svc.publish_engine.create_candidate("no-validate", [edit])
    with pytest.raises(ServiceError):
        svc.publish_engine.publish(cand.candidate_id)


def test_invalid_candidate_cannot_publish(svc):
    bad = edit_tile(svc, "t10", port_shift={"W:1": (0.0, 10.0)})
    cand = svc.publish_engine.create_candidate("bad", [bad])
    svc.publish_engine.validate(cand.candidate_id)
    with pytest.raises(ServiceError):
        svc.publish_engine.publish(cand.candidate_id)
    # 发布失败：线上代际不变，单一可解释版本保持
    assert svc.store.current_generation("south") == 1
    assert svc.store.get_candidate(cand.candidate_id).status == "FAILED"


def test_publish_failure_rolls_back_all_regions(svc, monkeypatch):
    """多区域发布中途失败：整体回滚，任何区域代际都不变。"""
    edit_s = edit_tile(svc, "t00")
    edit_n = edit_tile(svc, "t01")
    cand = svc.publish_engine.create_candidate("two-region", [edit_s, edit_n])
    report = svc.publish_engine.validate(cand.candidate_id)
    assert report["ok"]

    # 在写第二块瓦片时注入故障
    orig = svc.store.put_tiles_tx
    calls = {"n": 0}

    def flaky(cur, tiles):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟存储故障")
        return orig(cur, tiles)

    monkeypatch.setattr(svc.store, "put_tiles_tx", flaky)
    with pytest.raises(ServiceError):
        svc.publish_engine.publish(cand.candidate_id)

    # 单一可解释版本：两个区域都停在 g1，无半截发布
    assert svc.store.current_generation("south") == 1
    assert svc.store.current_generation("north") == 1
    assert svc.store.get_candidate(cand.candidate_id).status == "FAILED"


def test_old_generations_retained_for_snapshots(svc):
    """发布后旧代际记录仍可解析（车辆快照可继续使用旧版本）。"""
    edit = edit_tile(svc, "t00")
    cand = svc.publish_engine.create_candidate("v2", [edit])
    svc.publish_engine.validate(cand.candidate_id)
    svc.publish_engine.publish(cand.candidate_id)
    old = svc.store.get_generation("south", 1)
    assert old.tile_versions["t00"] == 1
    tile = svc.store.get_tile("t00", 1)
    assert tile.version == 1
