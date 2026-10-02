"""发布 API：候选版本、验证、发布、补丁、路线快照、审查数据。

启动：
    uvicorn mapsvc.api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .models import PatchOp, TileData
from .services import MapService, ServiceError
from .store import Store

DB_PATH = os.environ.get("MAPSVC_DB", "/workspace/data/mapsvc.db")


def create_service() -> MapService:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return MapService(Store(DB_PATH))


svc = create_service()
app = FastAPI(title="高精地图分区发布与路线快照服务", version="1.0.0")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _err(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ServiceError as exc:
        raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- 请求模型

class CandidateIn(BaseModel):
    name: str
    edits: list[dict]                 # TileData dict
    effective_regions: Optional[list[str]] = None


class ValidateIn(BaseModel):
    extra_od_pairs: Optional[list[dict]] = None


class PatchIn(BaseModel):
    title: str
    ops: list[dict]                   # PatchOp dict
    priority: int = 100
    base: Optional[dict] = None


class RouteIn(BaseModel):
    vehicle_id: str
    src_xy: list[float]
    dst_xy: list[float]


class RefreshIn(BaseModel):
    at_xy: list[float]


class CatchupIn(BaseModel):
    snapshot_id: str
    at_xy: list[float]


class RegionIn(BaseModel):
    region_id: str


# ---------------------------------------------------------------- 区域与代际

@app.post("/api/regions")
def register_region(body: RegionIn):
    svc.store.register_region(body.region_id)
    return {"ok": True}


@app.get("/api/regions")
def list_regions():
    return {"regions": svc.store.list_regions(),
            "effective_version": svc.effective_version()}


@app.get("/api/regions/{region_id}/generations")
def list_generations(region_id: str):
    return {"generations": [g.to_dict()
                            for g in svc.store.list_generations(region_id)]}


# ---------------------------------------------------------------- 候选版本

@app.post("/api/candidates")
def create_candidate(body: CandidateIn):
    edits = [TileData.from_dict(e) for e in body.edits]
    cand = _err(svc.publish_engine.create_candidate,
                body.name, edits, body.effective_regions)
    return cand.to_dict()


@app.get("/api/candidates")
def list_candidates():
    return {"candidates": [c.to_dict() for c in svc.store.list_candidates()]}


@app.get("/api/candidates/{candidate_id}")
def get_candidate(candidate_id: str):
    return _err(svc.store.get_candidate, candidate_id).to_dict()


@app.post("/api/candidates/{candidate_id}/validate")
def validate(candidate_id: str, body: Optional[ValidateIn] = None):
    od = body.extra_od_pairs if body else None
    return _err(svc.publish_engine.validate, candidate_id, od)


@app.post("/api/candidates/{candidate_id}/publish")
def publish(candidate_id: str):
    return _err(svc.publish_engine.publish, candidate_id)


# ---------------------------------------------------------------- 补丁

@app.post("/api/patches")
def create_patch(body: PatchIn):
    ops = [PatchOp.from_dict(o) for o in body.ops]
    return _err(svc.patches.create_patch, body.title, ops,
                body.priority, body.base)


@app.get("/api/patches")
def list_patches(status: Optional[str] = None):
    return {"patches": [p.to_dict() for p in svc.store.list_patches(status=status)]}


@app.post("/api/patches/{patch_id}/revoke")
def revoke_patch(patch_id: str):
    return _err(svc.patches.revoke_patch, patch_id)


# ---------------------------------------------------------------- 路线快照

@app.post("/api/routes")
def request_route(body: RouteIn):
    snap = _err(svc.snapshots.request_route,
                body.vehicle_id, body.src_xy, body.dst_xy)
    return svc.explain_snapshot(snap)


@app.get("/api/snapshots")
def list_snapshots(vehicle_id: Optional[str] = None,
                   status: Optional[str] = None):
    snaps = svc.store.list_snapshots(vehicle_id=vehicle_id, status=status)
    return {"snapshots": [svc.explain_snapshot(s) for s in snaps]}


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str):
    snap = _err(svc.store.get_snapshot, snapshot_id)
    return svc.explain_snapshot(snap)


@app.post("/api/snapshots/{snapshot_id}/refresh")
def refresh_snapshot(snapshot_id: str, body: RefreshIn):
    snap = _err(svc.snapshots.refresh, snapshot_id, body.at_xy)
    return svc.explain_snapshot(snap)


@app.post("/api/snapshots/{snapshot_id}/close")
def close_snapshot(snapshot_id: str):
    snap = _err(svc.snapshots.close, snapshot_id)
    return svc.explain_snapshot(snap)


@app.post("/api/vehicles/{vehicle_id}/catchup")
def catchup(vehicle_id: str, body: CatchupIn):
    return _err(svc.snapshots.catchup, vehicle_id, body.snapshot_id, body.at_xy)


# ---------------------------------------------------------------- 审查页数据

@app.get("/api/review/state")
def review_state():
    """审查页聚合数据：区域代际、候选、补丁、活跃快照、当前有效版本。"""
    regions = svc.store.list_regions()
    gen_detail = {}
    tiles_view = []
    for r in regions:
        rid = r["region_id"]
        gens = svc.store.list_generations(rid)
        gen_detail[rid] = [g.to_dict() for g in gens]
        if r["generation"] > 0:
            rec = svc.store.get_generation(rid, r["generation"])
            for tile_id, ver in rec.tile_versions.items():
                t = svc.store.get_tile(tile_id, ver)
                tiles_view.append(t.to_dict())
    return {
        "regions": regions,
        "generations": gen_detail,
        "candidates": [c.to_dict() for c in svc.store.list_candidates()],
        "patches": [p.to_dict() for p in svc.store.list_patches()],
        "snapshots": [svc.explain_snapshot(s)
                      for s in svc.store.list_snapshots()],
        "effective_version": svc.effective_version(),
        "tiles": tiles_view,
    }


@app.get("/review")
def review_page():
    return FileResponse(os.path.join(WEB_DIR, "review.html"))


@app.get("/")
def index():
    return {"service": "高精地图分区发布与路线快照服务",
            "review_page": "/review", "docs": "/docs"}
