# 海洋科考采样记录与岸端同步

一个仅使用 Python 标准库实现的离线优先采样记录服务。船端可以按批次上传站位、样本、母样/分样、保管交接和仪器文件元数据；岸端负责幂等接收、冲突隔离、编号分配和确认锁定。

## 运行

```bash
python app.py --init
python app.py --port 8008
```

打开 <http://127.0.0.1:8008>。`--init` 会创建示例航次 `2026-ECS-01`。数据库默认是 `ocean_samples.db`，可用 `--db` 或 `OCEAN_DB` 修改。

## 离线同步

`POST /api/sync` 的每个记录都包含 `type`、`local_uuid`、`revision` 和 `data`。同一条记录重复上传会识别为 `duplicates`，不会重复建库。

```json
{
  "device_id": "tablet-A",
  "records": [
    {"type": "station", "local_uuid": "st-001", "revision": 1, "data": {
      "voyage_id": 1, "station_code": "S-01", "latitude": 30.1,
      "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00",
      "owner": "member-a"
    }},
    {"type": "sample", "local_uuid": "sample-001", "revision": 1, "data": {
      "station_id": 1, "sample_code": "W-001", "sample_type": "water",
      "depth_m": 5, "storage_condition": "4C", "owner": "member-a"
    }}
  ]
}
```

## 业务规则

- `local_uuid + device_id` 是幂等键；相同内容重复上传直接返回已有记录。
- `revision` 必须递增。旧修订或同修订不同内容会进入冲突表，不会覆盖新数据。
- 站位编号在航次内唯一，样本编号全局唯一；检测到另一设备使用相同编号时分配 `-DUP-xxxx` 后缀并把冲突写入隔离表。
- 记录人员只能修改自己创建的站位/样本，`lead` 可以处理全部记录。
- `lead` 确认样本或站位后，该记录变为只读；发现错误必须通过新记录处理，不能用同步覆盖历史。
- 保管交接是追加式事件；仪器文件以 SHA-256 去重，原始内容相同但来自不同设备时只保存一次元数据。
- 航次、站位、样本、交接、文件和冲突都保存在 SQLite 中，所有同步批次有审计记录。

## 站位封存包

岸端归档在 `/archive.html`。同一站位只保留一个待办包，页面会列出缺项并挡住封存：

- 站位还没有样本，或样本未经负责人确认；
- 样本缺少保管交接记录；
- 没有关联本站位的仪器文件。

封存（`lead`）把当时的清单与文件哈希冻结成一个版本，清单整体再算 SHA-256 作为版本指纹。清单未变化时重复封存指向同一版本；之后补传的文件进入下一次封存的新版本，旧版本始终可查。存档数据（`archive_store.py`）、归档判定（`archive_rules.py`）与页面（`static/archive.html`）分开实现，仍只用标准库。

## API

- `POST /api/voyages`：创建航次。
- `POST /api/sync`：批量同步离线记录。
- `POST /api/confirm/station/{id}` 或 `/api/confirm/sample/{id}`：负责人确认锁定。
- `GET /api/archive`：各站位待办包汇总（缺项数、是否可封存、已封存版本数）。
- `GET /api/archive/{station_id}`：待办包详情，列出缺项与历史版本清单。
- `POST /api/archive/{station_id}/seal`：负责人封存；有缺项时返回 409 与缺项清单。
- `GET /api/stations`、`/api/samples`、`/api/custody`、`/api/instrument-files`：查询记录。
- `GET /api/conflicts`：查看编号冲突、旧修订和权限冲突。
- `GET /api/audit`：查看操作审计。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整离线同步、幂等重复、编号冲突、成员越权、旧修订、确认锁定、追加式交接、文件哈希去重，以及封存包缺项阻挡、清单冻结、补传文件另起新版和旧版本可查。
