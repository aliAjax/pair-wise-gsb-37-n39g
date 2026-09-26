import tempfile
import unittest
from pathlib import Path

from app import Database, seed_demo
from archive_store import ArchiveError, ArchiveStore


class OceanSyncFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.voyage = seed_demo(self.db)["voyage"]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, kind, uuid, revision, data, device="tablet-A"):
        return {"type": kind, "local_uuid": uuid, "revision": revision, "data": data}

    def test_full_offline_sync_conflict_confirm_and_file_dedupe(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        batch = {"device_id": "tablet-A", "records": [self.record("station", "st-001", 1, station)]}
        first = self.db.sync("member-a", "member", batch)
        self.assertEqual(first["created"], 1)
        duplicate = self.db.sync("member-a", "member", batch)
        self.assertEqual(duplicate["duplicates"], 1)
        station_id = self.db.list_stations()[0]["id"]

        sample = {"station_id": station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 1, sample)]})
        updated = dict(sample, storage_condition="negative-20C")
        result = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 2, updated)]})
        self.assertEqual(result["updated"], 1)
        stale = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 1, sample)]})
        self.assertEqual(len(stale["conflicts"]), 1)
        sample_id = self.db.list_samples()[0]["id"]
        self.db.confirm("sample", sample_id, "lead-01", "lead")
        locked = self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 3, dict(updated, depth_m=6))]})
        self.assertIn("不能覆盖", locked["conflicts"][0]["reason"])

        custody = {"sample_id": sample_id, "event_type": "handover", "from_party": "member-a", "to_party": "shore-lab", "occurred_at": "2026-09-26T09:00:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("custody", "custody-001", 1, custody)]})
        self.assertEqual(len(self.db.list_custody()), 1)

        digest = "a" * 64
        file_record = {"voyage_id": self.voyage, "station_id": station_id, "file_name": "ctd.csv", "sha256": digest, "size_bytes": 120, "captured_at": "2026-09-05T08:20:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("instrument_file", "file-001", 1, file_record)]})
        duplicate_file = self.db.sync("member-b", "member", {"device_id": "tablet-B", "records": [self.record("instrument_file", "file-002", 1, file_record, "tablet-B")]})
        self.assertEqual(duplicate_file["duplicates"], 1)
        self.assertEqual(len(self.db.list_files()), 1)

    def test_duplicate_code_and_member_update_are_isolated(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("station", "st-a", 1, station, "A")]})
        station_id = self.db.list_stations()[0]["id"]
        sample = {"station_id": station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5, "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "A", "records": [self.record("sample", "sample-a", 1, sample, "A")]})
        conflict = self.db.sync("member-b", "member", {"device_id": "B", "records": [self.record("sample", "sample-b", 1, sample, "B")]})
        self.assertEqual(conflict["created"], 1)
        self.assertIn("已分配", conflict["conflicts"][0]["reason"])
        samples = self.db.list_samples()
        sample_b = next(item for item in samples if item["id"] != samples[0]["id"])
        unauthorized = self.db.sync("member-c", "member", {"device_id": "B", "records": [self.record("sample", "sample-b", 2, dict(sample, sample_code=sample_b["sample_code"], depth_m=9), "B")]})
        self.assertIn("只有记录人", unauthorized["conflicts"][0]["reason"])


class ArchiveFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.voyage = seed_demo(self.db)["voyage"]
        self.archives = ArchiveStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, kind, uuid, revision, data, device="tablet-A"):
        return {"type": kind, "local_uuid": uuid, "revision": revision, "data": data}

    def make_station(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0,
                   "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("station", "st-001", 1, station)]})
        return self.db.list_stations()[0]["id"]

    def add_sample(self, station_id):
        sample = {"station_id": station_id, "sample_code": "W-001", "sample_type": "water", "depth_m": 5,
                  "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", "sample-001", 1, sample)]})
        return self.db.list_samples()[0]["id"]

    def add_custody(self, sample_id):
        custody = {"sample_id": sample_id, "event_type": "handover", "from_party": "member-a",
                   "to_party": "shore-lab", "occurred_at": "2026-09-26T09:00:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("custody", "custody-001", 1, custody)]})

    def add_file(self, station_id, digest, uuid):
        file_record = {"voyage_id": self.voyage, "station_id": station_id, "file_name": "ctd.csv",
                       "sha256": digest, "size_bytes": 120, "captured_at": "2026-09-05T08:20:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("instrument_file", uuid, 1, file_record)]})

    def ready_station(self):
        station_id = self.make_station()
        sample_id = self.add_sample(station_id)
        self.db.confirm("sample", sample_id, "lead-01", "lead")
        self.add_custody(sample_id)
        self.add_file(station_id, "a" * 64, "file-001")
        return station_id

    def test_missing_items_are_listed_and_block_seal(self):
        station_id = self.make_station()
        self.add_sample(station_id)
        detail = self.archives.evaluate(station_id, "lead-01")
        codes = {m["code"] for m in detail["missing"]}
        self.assertEqual(codes, {"sample_unconfirmed", "custody_missing", "no_station_files"})
        self.assertFalse(detail["sealable"])
        with self.assertRaises(ArchiveError) as ctx:
            self.archives.seal(station_id, "lead-01", "lead")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual({m["code"] for m in ctx.exception.missing}, codes)
        summary = self.archives.list_packages("lead-01")
        self.assertEqual(summary[0]["missing_count"], 3)
        self.assertFalse(summary[0]["sealable"])

    def test_empty_station_reports_no_samples_and_no_files(self):
        station_id = self.make_station()
        detail = self.archives.evaluate(station_id, "lead-01")
        self.assertEqual({m["code"] for m in detail["missing"]}, {"no_samples", "no_station_files"})

    def test_seal_freezes_manifest_and_late_files_form_next_version(self):
        station_id = self.ready_station()
        detail = self.archives.evaluate(station_id, "lead-01")
        self.assertTrue(detail["sealable"])
        self.assertEqual(detail["next_version"], 1)

        v1 = self.archives.seal(station_id, "lead-01", "lead")
        self.assertFalse(v1["duplicate"])
        self.assertEqual(v1["version"], 1)
        self.assertEqual(len(v1["manifest"]["files"]), 1)
        self.assertEqual(v1["manifest"]["files"][0]["sha256"], "a" * 64)
        self.assertTrue(any(a["action"] == "archive.sealed" for a in self.db.audit()))

        again = self.archives.seal(station_id, "lead-01", "lead")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["version"], 1)

        self.add_file(station_id, "b" * 64, "file-002")
        v2 = self.archives.seal(station_id, "lead-01", "lead")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(len(v2["manifest"]["files"]), 2)
        self.assertNotEqual(v1["manifest_hash"], v2["manifest_hash"])

        detail = self.archives.evaluate(station_id, "lead-01")
        self.assertEqual([v["version"] for v in detail["versions"]], [2, 1])
        old = next(v for v in detail["versions"] if v["version"] == 1)
        self.assertEqual(len(old["manifest"]["files"]), 1)
        self.assertEqual(old["manifest_hash"], v1["manifest_hash"])
        self.assertEqual(detail["next_version"], 3)

    def test_one_pending_package_per_station(self):
        station_id = self.make_station()
        first = self.archives.evaluate(station_id, "lead-01")
        second = self.archives.evaluate(station_id, "shore-b")
        self.assertEqual(first["package_id"], second["package_id"])
        summary = self.archives.list_packages("lead-01")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["package_id"], first["package_id"])

    def test_seal_requires_lead_and_known_station(self):
        station_id = self.ready_station()
        with self.assertRaises(ArchiveError) as ctx:
            self.archives.seal(station_id, "member-a", "member")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ArchiveError) as ctx:
            self.archives.evaluate(999, "lead-01")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
