"""归档判定：根据站位当前数据计算封存缺项，并组装冻结清单。

只包含纯函数，不接触数据库；站位、样本、交接和文件由 archive_store 读取后传入。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evaluate_readiness(bundle: dict[str, Any]) -> dict[str, Any]:
    """检查站位是否满足封存条件，返回 ready 标记、缺项清单和数量统计。"""
    samples = bundle["samples"]
    custody = bundle["custody_events"]
    files = bundle["instrument_files"]
    missing: list[dict[str, Any]] = []
    if not samples:
        missing.append({"code": "no_samples", "message": "站位还没有同步样本"})
    unconfirmed = [s["sample_code"] for s in samples if not s["confirmed"]]
    if unconfirmed:
        missing.append({
            "code": "samples_unconfirmed",
            "message": "样本未确认: " + ", ".join(unconfirmed),
            "samples": unconfirmed,
        })
    with_custody = {e["sample_id"] for e in custody}
    no_custody = [s["sample_code"] for s in samples if s["id"] not in with_custody]
    if no_custody:
        missing.append({
            "code": "custody_missing",
            "message": "样本缺少保管交接: " + ", ".join(no_custody),
            "samples": no_custody,
        })
    if not files:
        missing.append({"code": "instrument_files_missing", "message": "仪器文件未关联本站位"})
    return {
        "ready": not missing,
        "missing": missing,
        "counts": {
            "samples": len(samples),
            "unconfirmed_samples": len(unconfirmed),
            "custody_events": len(custody),
            "instrument_files": len(files),
        },
    }


_STATION_FIELDS = ("id", "voyage_id", "station_code", "latitude", "longitude", "sampled_at", "owner", "confirmed", "revision")
_SAMPLE_FIELDS = ("id", "parent_sample_id", "sample_code", "sample_type", "depth_m", "storage_condition", "owner", "confirmed", "revision")
_CUSTODY_FIELDS = ("id", "sample_id", "event_type", "from_party", "to_party", "occurred_at", "recorded_by")
_FILE_FIELDS = ("id", "file_name", "sha256", "size_bytes", "captured_at", "source_device")


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: row[k] for k in fields}


def build_manifest(bundle: dict[str, Any], version: int) -> dict[str, Any]:
    """把站位当前的水样、交接和仪器文件整理成可冻结的清单。

    只取固定字段且不含时间戳，内容相同则清单哈希相同，便于事后校验。
    """
    return {
        "version": version,
        "station": _pick(bundle["station"], _STATION_FIELDS),
        "samples": [_pick(s, _SAMPLE_FIELDS) for s in bundle["samples"]],
        "custody_events": [_pick(e, _CUSTODY_FIELDS) for e in bundle["custody_events"]],
        "instrument_files": [_pick(f, _FILE_FIELDS) for f in bundle["instrument_files"]],
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()
