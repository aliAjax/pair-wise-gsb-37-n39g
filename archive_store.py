"""封存包存档数据：待办包与冻结版本的 SQLite 存储。

每个站位只保留一个待办包；每次封存把当时的清单与文件哈希冻结成一个新版本，
之后补传的文件只会进入下一次封存，历史版本始终可查。判定逻辑在 archive_rules.py。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from archive_rules import build_manifest, find_missing, manifest_hash


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ArchiveError(Exception):
    def __init__(self, message: str, status: int = 400, missing: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status = status
        self.missing = missing or []


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL UNIQUE REFERENCES stations(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES archive_packages(id),
    version INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest TEXT NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE(package_id, version)
);
"""


class ArchiveStore:
    """待办包与封存版本的存储；复用主数据库连接，表结构自己维护。"""

    def __init__(self, db: Any):
        self.db = db
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)

    def _collect(self, conn: sqlite3.Connection, station_id: int):
        station = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if not station:
            raise ArchiveError("站位不存在", 404)
        samples = [dict(r) for r in conn.execute(
            "SELECT * FROM samples WHERE station_id=? ORDER BY id", (station_id,)).fetchall()]
        if samples:
            marks = ",".join("?" * len(samples))
            custody = [dict(r) for r in conn.execute(
                f"SELECT * FROM custody_events WHERE sample_id IN ({marks}) ORDER BY id",
                [s["id"] for s in samples]).fetchall()]
        else:
            custody = []
        files = [dict(r) for r in conn.execute(
            "SELECT * FROM instrument_files WHERE station_id=? ORDER BY id", (station_id,)).fetchall()]
        return dict(station), samples, custody, files

    def _ensure_package(self, conn: sqlite3.Connection, station_id: int, actor: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM archive_packages WHERE station_id=?", (station_id,)).fetchone()
        if row:
            return dict(row)
        try:
            cur = conn.execute(
                "INSERT INTO archive_packages(station_id,created_by,created_at) VALUES(?,?,?)",
                (station_id, actor, utcnow()))
        except sqlite3.IntegrityError:  # 并发下另一请求已创建同一站位的待办包
            return dict(conn.execute("SELECT * FROM archive_packages WHERE station_id=?", (station_id,)).fetchone())
        return dict(conn.execute("SELECT * FROM archive_packages WHERE id=?", (cur.lastrowid,)).fetchone())

    def _versions(self, conn: sqlite3.Connection, package_id: int) -> list[dict[str, Any]]:
        versions = []
        for row in conn.execute(
                "SELECT * FROM archive_versions WHERE package_id=? ORDER BY version DESC", (package_id,)).fetchall():
            item = dict(row)
            item["manifest"] = json.loads(item["manifest"])
            versions.append(item)
        return versions

    def evaluate(self, station_id: int, actor: str = "system") -> dict[str, Any]:
        """待办包详情：当前缺项、是否可封存、下一版号和历史版本。"""
        with self.db.connect() as conn:
            station, samples, custody, files = self._collect(conn, station_id)
            package = self._ensure_package(conn, station_id, actor)
            versions = self._versions(conn, package["id"])
        missing = find_missing(station, samples, custody, files)
        return {
            "package_id": package["id"],
            "station": station,
            "samples": samples,
            "custody": custody,
            "files": files,
            "missing": missing,
            "sealable": not missing,
            "next_version": (versions[0]["version"] + 1) if versions else 1,
            "versions": versions,
        }

    def list_packages(self, actor: str = "system") -> list[dict[str, Any]]:
        """每个站位的待办包汇总，供页面列表使用。"""
        items = []
        with self.db.connect() as conn:
            for row in conn.execute("SELECT id FROM stations ORDER BY id").fetchall():
                station, samples, custody, files = self._collect(conn, row["id"])
                package = self._ensure_package(conn, station["id"], actor)
                missing = find_missing(station, samples, custody, files)
                versions = self._versions(conn, package["id"])
                items.append({
                    "package_id": package["id"],
                    "station_id": station["id"],
                    "station_code": station["station_code"],
                    "voyage_id": station["voyage_id"],
                    "sealable": not missing,
                    "missing_count": len(missing),
                    "sample_count": len(samples),
                    "file_count": len(files),
                    "sealed_versions": len(versions),
                    "next_version": (versions[0]["version"] + 1) if versions else 1,
                })
        return items

    def seal(self, station_id: int, actor: str, role: str) -> dict[str, Any]:
        """封存当前清单为新版本；有缺项时抛出带缺项清单的 ArchiveError。"""
        if role != "lead":
            raise ArchiveError("只有航次负责人可以封存", 403)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            station, samples, custody, files = self._collect(conn, station_id)
            missing = find_missing(station, samples, custody, files)
            if missing:
                raise ArchiveError("存在缺项，不能封存", 409, missing)
            package = self._ensure_package(conn, station_id, actor)
            manifest = build_manifest(station, samples, custody, files)
            digest = manifest_hash(manifest)
            latest = conn.execute(
                "SELECT * FROM archive_versions WHERE package_id=? ORDER BY version DESC LIMIT 1",
                (package["id"],)).fetchone()
            if latest and latest["manifest_hash"] == digest:
                result = dict(latest)
                result["manifest"] = json.loads(result["manifest"])
                result["duplicate"] = True
                return result
            version = (int(latest["version"]) + 1) if latest else 1
            cur = conn.execute(
                "INSERT INTO archive_versions(package_id,version,manifest_hash,manifest,sealed_by,sealed_at) VALUES(?,?,?,?,?,?)",
                (package["id"], version, digest, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()))
            self.db._audit(conn, actor, "archive.sealed", "station", station_id,
                           {"package_id": package["id"], "version": version, "manifest_hash": digest})
            result = dict(conn.execute("SELECT * FROM archive_versions WHERE id=?", (cur.lastrowid,)).fetchone())
            result["manifest"] = json.loads(result["manifest"])
            result["duplicate"] = False
            return result
