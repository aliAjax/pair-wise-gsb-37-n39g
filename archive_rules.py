"""归档判定：根据站位当前记录计算封存缺项并生成冻结清单。

纯函数模块，不接触数据库；输入是存储层取出的字典行，输出是缺项与清单。
存档数据在 archive_store.py，页面在 static/archive.html，三者保持分离。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_missing(station: dict[str, Any], samples: list[dict[str, Any]],
                 custody: list[dict[str, Any]], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出阻止封存的缺项；返回空列表表示可以封存。"""
    missing: list[dict[str, Any]] = []
    if not samples:
        missing.append({"code": "no_samples", "message": "站位还没有任何样本"})
    for sample in samples:
        if not sample["confirmed"]:
            missing.append({"code": "sample_unconfirmed", "sample_id": sample["id"],
                            "message": f"样本 {sample['sample_code']} 未确认"})
    handed_over = {event["sample_id"] for event in custody}
    for sample in samples:
        if sample["id"] not in handed_over:
            missing.append({"code": "custody_missing", "sample_id": sample["id"],
                            "message": f"样本 {sample['sample_code']} 缺少保管交接"})
    if not files:
        missing.append({"code": "no_station_files", "message": "没有关联本站位的仪器文件"})
    return missing


_STATION_KEYS = ("id", "voyage_id", "station_code", "latitude", "longitude", "sampled_at", "owner", "confirmed", "revision")
_SAMPLE_KEYS = ("id", "sample_code", "sample_type", "depth_m", "storage_condition", "owner", "confirmed", "revision")
_CUSTODY_KEYS = ("id", "sample_id", "event_type", "from_party", "to_party", "occurred_at", "recorded_by")
_FILE_KEYS = ("id", "file_name", "sha256", "size_bytes", "captured_at", "source_device")


def build_manifest(station: dict[str, Any], samples: list[dict[str, Any]],
                   custody: list[dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, Any]:
    """把封存时刻的清单与文件哈希冻结成一份清单。"""
    return {
        "station": {k: station[k] for k in _STATION_KEYS},
        "samples": [{k: s[k] for k in _SAMPLE_KEYS} for s in samples],
        "custody": [{k: c[k] for k in _CUSTODY_KEYS} for c in custody],
        "files": [{k: f[k] for k in _FILE_KEYS} for f in files],
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    """清单内容寻址哈希；清单条目或任一文件哈希变化都会改变它。"""
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()
