"""持久化存储：SQLite 单文件，发布等关键路径在事务内完成，保证原子性。

表结构：
- tiles(tile_id, version, region_id, data)        不可变瓦片内容
- regions(region_id, generation)                  区域当前代际指针
- generations(region_id, generation, data)        不可变代际记录
- candidates(candidate_id, data)                  候选版本
- patches(patch_id, data)                         补丁
- snapshots(snapshot_id, data)                    车辆路线快照
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from .models import (
    Candidate, GenerationRecord, Patch, Snapshot, TileData,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    tile_id   TEXT NOT NULL,
    version   INTEGER NOT NULL,
    region_id TEXT NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (tile_id, version)
);
CREATE TABLE IF NOT EXISTS regions (
    region_id  TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generations (
    region_id  TEXT NOT NULL,
    generation INTEGER NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (region_id, generation)
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------ 区域与瓦片

    def register_region(self, region_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO regions(region_id, generation) VALUES (?, 0)",
                (region_id,),
            )

    def list_regions(self) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT region_id, generation FROM regions ORDER BY region_id"
            ).fetchall()
        return [{"region_id": r["region_id"], "generation": r["generation"]} for r in rows]

    def current_generation(self, region_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT generation FROM regions WHERE region_id=?", (region_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"未知区域: {region_id}")
        return row["generation"]

    def put_tile(self, tile: TileData) -> None:
        """写入不可变瓦片版本；重复写入同 (tile_id, version) 视为冲突。"""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO tiles(tile_id, version, region_id, data) VALUES (?,?,?,?)",
                (tile.tile_id, tile.version, tile.region_id, json.dumps(tile.to_dict())),
            )

    def put_tiles_tx(self, cur: sqlite3.Cursor, tiles: list) -> None:
        for t in tiles:
            cur.execute(
                "INSERT INTO tiles(tile_id, version, region_id, data) VALUES (?,?,?,?)",
                (t.tile_id, t.version, t.region_id, json.dumps(t.to_dict())),
            )

    def get_tile(self, tile_id: str, version: int) -> TileData:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM tiles WHERE tile_id=? AND version=?",
                (tile_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"瓦片不存在: {tile_id}@v{version}")
        return TileData.from_dict(json.loads(row["data"]))

    def latest_tile_version(self, tile_id: str) -> Optional[int]:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM tiles WHERE tile_id=?", (tile_id,)
            ).fetchone()
        return row["v"] if row and row["v"] is not None else None

    # ------------------------------------------------------------ 代际记录

    def put_generation(self, rec: GenerationRecord) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO generations(region_id, generation, data) VALUES (?,?,?)",
                (rec.region_id, rec.generation, json.dumps(rec.to_dict())),
            )

    def get_generation(self, region_id: str, generation: int) -> GenerationRecord:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM generations WHERE region_id=? AND generation=?",
                (region_id, generation),
            ).fetchone()
        if row is None:
            raise KeyError(f"代际不存在: {region_id}@g{generation}")
        return GenerationRecord.from_dict(json.loads(row["data"]))

    def list_generations(self, region_id: str) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT data FROM generations WHERE region_id=? ORDER BY generation",
                (region_id,),
            ).fetchall()
        return [GenerationRecord.from_dict(json.loads(r["data"])) for r in rows]

    # ------------------------------------------------------------ 候选版本

    def put_candidate(self, cand: Candidate) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO candidates(candidate_id, data) VALUES (?,?)",
                (cand.candidate_id, json.dumps(cand.to_dict())),
            )

    def get_candidate(self, candidate_id: str) -> Candidate:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"候选版本不存在: {candidate_id}")
        return Candidate.from_dict(json.loads(row["data"]))

    def list_candidates(self) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT data FROM candidates ORDER BY json_extract(data,'$.created_at')"
            ).fetchall()
        return [Candidate.from_dict(json.loads(r["data"])) for r in rows]

    # ------------------------------------------------------------ 补丁

    def put_patch(self, patch: Patch) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO patches(patch_id, data) VALUES (?,?)",
                (patch.patch_id, json.dumps(patch.to_dict())),
            )

    def get_patch(self, patch_id: str) -> Patch:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM patches WHERE patch_id=?", (patch_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"补丁不存在: {patch_id}")
        return Patch.from_dict(json.loads(row["data"]))

    def list_patches(self, status: Optional[str] = None) -> list:
        with self._lock:
            rows = self.conn.execute("SELECT data FROM patches").fetchall()
        patches = [Patch.from_dict(json.loads(r["data"])) for r in rows]
        if status:
            patches = [p for p in patches if p.status == status]
        # 高优先级在前，同优先级按创建时间
        patches.sort(key=lambda p: (-p.priority, p.created_at))
        return patches

    # ------------------------------------------------------------ 快照

    def put_snapshot(self, snap: Snapshot) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO snapshots(snapshot_id, vehicle_id, data) VALUES (?,?,?)",
                (snap.snapshot_id, snap.vehicle_id, json.dumps(snap.to_dict())),
            )

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"快照不存在: {snapshot_id}")
        return Snapshot.from_dict(json.loads(row["data"]))

    def list_snapshots(self, vehicle_id: Optional[str] = None,
                       status: Optional[str] = None) -> list:
        sql = "SELECT data FROM snapshots"
        conds, args = [], []
        if vehicle_id:
            conds.append("vehicle_id=?")
            args.append(vehicle_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        snaps = [Snapshot.from_dict(json.loads(r["data"])) for r in rows]
        if status:
            snaps = [s for s in snaps if s.status == status]
        snaps.sort(key=lambda s: s.created_at)
        return snaps
