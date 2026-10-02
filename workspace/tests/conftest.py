import pytest

from mapsvc import MapService, Store
from mapsvc.gridmap import CITY_POS, CITY_TOWARD, make_city, make_grid_tile

REGION_OF = {"t00": "south", "t10": "south", "t01": "north", "t11": "north"}


def bootstrap() -> MapService:
    """两区域（south/north）2x2 城市，初始候选验证并发布 -> 各区域 g1。"""
    svc = MapService(Store(":memory:"))
    svc.store.register_region("south")
    svc.store.register_region("north")
    tiles = make_city(REGION_OF)
    cand = svc.publish_engine.create_candidate("init", tiles)
    report = svc.publish_engine.validate(cand.candidate_id)
    assert report["ok"], report["issues"]
    svc.publish_engine.publish(cand.candidate_id)
    return svc


@pytest.fixture()
def svc() -> MapService:
    return bootstrap()


def edit_tile(svc: MapService, tile_id: str, **kwargs):
    """基于当前发布版本生成下一版本的瓦片编辑。"""
    region = REGION_OF[tile_id]
    ver = svc.store.latest_tile_version(tile_id) + 1
    return make_grid_tile(tile_id, region, *CITY_POS[tile_id],
                          version=ver, toward=CITY_TOWARD[tile_id], **kwargs)
