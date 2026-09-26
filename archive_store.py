"""封存包存储：每个站位保留一个待办包，封存后冻结清单并另起下一版。

只负责 SQLite 读写和封存事务；缺项判定与清单组装在 archive_rules。
与主服务共用同一个 SQLite 文件（需先由 app.Database 建好基础表），不引入新依赖。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from archive_rules import build_manifest, canonical, evaluate_readiness, manifest_hash


class ArchiveError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ArchiveStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_id INTEGER NOT NULL REFERENCES stations(id),
                    voyage_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sealed')),
                    manifest TEXT NOT NULL DEFAULT '',
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    sealed_by TEXT,
                    sealed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(station_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_one_pending
                    ON archive_packages(station_id) WHERE status='pending';
                """
            )

    def collect_station_bundle(self, conn: sqlite3.Connection, station_id: int) -> dict[str, Any]:
        """读取站位及其样本、交接和本站位仪器文件，供判定与组清单使用。"""
        station = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if not station:
            raise ArchiveError("站位不存在", 404)
        samples = [dict(r) for r in conn.execute("SELECT * FROM samples WHERE station_id=? ORDER BY id", (station_id,)).fetchall()]
        if samples:
            marks = ",".join("?" * len(samples))
            custody = [dict(r) for r in conn.execute(
                f"SELECT * FROM custody_events WHERE sample_id IN ({marks}) ORDER BY id",
                [s["id"] for s in samples],
            ).fetchall()]
        else:
            custody = []
        files = [dict(r) for r in conn.execute("SELECT * FROM instrument_files WHERE station_id=? ORDER BY id", (station_id,)).fetchall()]
        return {"station": dict(station), "samples": samples, "custody_events": custody, "instrument_files": files}

    def _ensure_pending(self, conn: sqlite3.Connection, station: dict[str, Any]) -> sqlite3.Row:
        """保证站位有一个待办包；已存在时 INSERT 被唯一索引忽略。"""
        conn.execute(
            """INSERT OR IGNORE INTO archive_packages(station_id,voyage_id,version,status,created_at)
               VALUES(?,?,(SELECT COALESCE(MAX(version),0)+1 FROM archive_packages WHERE station_id=?),'pending',?)""",
            (station["id"], station["voyage_id"], station["id"], utcnow()),
        )
        return conn.execute("SELECT * FROM archive_packages WHERE station_id=? AND status='pending'", (station["id"],)).fetchone()

    def station_status(self, station_id: int) -> dict[str, Any]:
        """待办包版本 + 当前缺项，供页面展示并决定是否允许封存。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bundle = self.collect_station_bundle(conn, station_id)
            package = dict(self._ensure_pending(conn, bundle["station"]))
            readiness = evaluate_readiness(bundle)
        package.pop("manifest", None)
        return {"package": package, "readiness": readiness}

    def seal_station(self, station_id: int, actor: str, role: str) -> dict[str, Any]:
        """封存当前待办包：冻结清单与文件哈希，并另起下一版待办包。"""
        if role != "lead":
            raise ArchiveError("只有航次负责人可以封存站位", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bundle = self.collect_station_bundle(conn, station_id)
            readiness = evaluate_readiness(bundle)
            if not readiness["ready"]:
                raise ArchiveError("存在缺项，无法封存", 409, {"missing": readiness["missing"]})
            package = self._ensure_pending(conn, bundle["station"])
            version = int(package["version"])
            manifest = build_manifest(bundle, version)
            digest = manifest_hash(manifest)
            now = utcnow()
            cur = conn.execute(
                "UPDATE archive_packages SET status='sealed',manifest=?,manifest_hash=?,sealed_by=?,sealed_at=? WHERE id=? AND status='pending'",
                (canonical(manifest), digest, actor, now, package["id"]),
            )
            if cur.rowcount != 1:
                raise ArchiveError("待办包状态已变化，请重试", 409)
            conn.execute(
                "INSERT INTO archive_packages(station_id,voyage_id,version,status,created_at) VALUES(?,?,?,'pending',?)",
                (station_id, bundle["station"]["voyage_id"], version + 1, now),
            )
            conn.execute(
                "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (actor, "archive.sealed", "archive_package", package["id"],
                 json.dumps({"station_id": station_id, "version": version, "manifest_hash": digest}, ensure_ascii=False), now),
            )
            sealed = dict(conn.execute("SELECT * FROM archive_packages WHERE id=?", (package["id"],)).fetchone())
        sealed["manifest"] = manifest
        return sealed

    def list_packages(self, station_id: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT id,station_id,voyage_id,version,status,manifest_hash,sealed_by,sealed_at,created_at "
               "FROM archive_packages")
        params: tuple[Any, ...] = ()
        if station_id is not None:
            sql += " WHERE station_id=?"
            params = (station_id,)
        sql += " ORDER BY station_id, version"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_package(self, package_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM archive_packages WHERE id=?", (package_id,)).fetchone()
        if not row:
            raise ArchiveError("封存包不存在", 404)
        package = dict(row)
        package["manifest"] = json.loads(package["manifest"]) if package["manifest"] else None
        return package
