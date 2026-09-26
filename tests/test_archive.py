import tempfile
import unittest
from pathlib import Path

from app import Database, seed_demo
from archive_store import ArchiveError, ArchiveStore


class ArchiveFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "test.db"
        self.db = Database(path)
        self.archives = ArchiveStore(path)
        self.voyage = seed_demo(self.db)["voyage"]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, kind, uuid, revision, data, device="tablet-A"):
        return {"type": kind, "local_uuid": uuid, "revision": revision, "data": data}

    def make_station(self):
        station = {"voyage_id": self.voyage, "station_code": "S-01", "latitude": 30.1, "longitude": 122.0,
                   "sampled_at": "2026-09-05T08:30:00+08:00", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("station", "st-001", 1, station)]})
        return self.db.list_stations()[0]["id"]

    def add_sample(self, station_id, code="W-001", uuid="sample-001"):
        sample = {"station_id": station_id, "sample_code": code, "sample_type": "water", "depth_m": 5,
                  "storage_condition": "4C", "owner": "member-a"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("sample", uuid, 1, sample)]})
        return next(s["id"] for s in self.db.list_samples() if s["sample_code"] == code)

    def add_custody(self, sample_id, uuid="custody-001"):
        custody = {"sample_id": sample_id, "event_type": "handover", "from_party": "member-a",
                   "to_party": "shore-lab", "occurred_at": "2026-09-26T09:00:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("custody", uuid, 1, custody)]})

    def add_file(self, station_id, digest, uuid):
        record = {"voyage_id": self.voyage, "station_id": station_id, "file_name": "ctd.csv", "sha256": digest,
                  "size_bytes": 120, "captured_at": "2026-09-05T08:20:00+08:00"}
        self.db.sync("member-a", "member", {"device_id": "tablet-A", "records": [self.record("instrument_file", uuid, 1, record)]})

    def test_missing_items_block_seal(self):
        station_id = self.make_station()
        empty = self.archives.station_status(station_id)
        self.assertEqual({m["code"] for m in empty["readiness"]["missing"]}, {"no_samples", "instrument_files_missing"})

        self.add_sample(station_id)
        status = self.archives.station_status(station_id)
        self.assertFalse(status["readiness"]["ready"])
        codes = {m["code"] for m in status["readiness"]["missing"]}
        self.assertEqual(codes, {"samples_unconfirmed", "custody_missing", "instrument_files_missing"})

        with self.assertRaises(ArchiveError) as ctx:
            self.archives.seal_station(station_id, "lead-01", "lead")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual({m["code"] for m in ctx.exception.details["missing"]}, codes)
        packages = self.archives.list_packages(station_id)
        self.assertEqual([(p["version"], p["status"]) for p in packages], [(1, "pending")])

    def test_seal_freezes_manifest_and_next_version_collects_new_files(self):
        station_id = self.make_station()
        sample_id = self.add_sample(station_id)
        self.db.confirm("sample", sample_id, "lead-01", "lead")
        self.add_custody(sample_id)
        self.add_file(station_id, "a" * 64, "file-001")

        sealed = self.archives.seal_station(station_id, "lead-01", "lead")
        self.assertEqual((sealed["version"], sealed["status"]), (1, "sealed"))
        self.assertEqual(sealed["sealed_by"], "lead-01")
        self.assertEqual(len(sealed["manifest"]["instrument_files"]), 1)
        self.assertEqual(sealed["manifest"]["instrument_files"][0]["sha256"], "a" * 64)
        first_hash = sealed["manifest_hash"]

        self.add_file(station_id, "b" * 64, "file-002")
        status = self.archives.station_status(station_id)
        self.assertEqual(status["package"]["version"], 2)
        self.assertTrue(status["readiness"]["ready"])

        sealed2 = self.archives.seal_station(station_id, "lead-01", "lead")
        self.assertEqual(sealed2["version"], 2)
        self.assertEqual(len(sealed2["manifest"]["instrument_files"]), 2)
        self.assertNotEqual(sealed2["manifest_hash"], first_hash)

        old = self.archives.get_package(sealed["id"])
        self.assertEqual(old["manifest_hash"], first_hash)
        self.assertEqual(len(old["manifest"]["instrument_files"]), 1)
        versions = [(p["version"], p["status"]) for p in self.archives.list_packages(station_id)]
        self.assertEqual(versions, [(1, "sealed"), (2, "sealed"), (3, "pending")])

    def test_only_lead_can_seal_and_unknown_station_is_404(self):
        station_id = self.make_station()
        with self.assertRaises(ArchiveError) as ctx:
            self.archives.seal_station(station_id, "member-a", "member")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ArchiveError) as ctx:
            self.archives.station_status(9999)
        self.assertEqual(ctx.exception.status, 404)

    def test_single_pending_package_per_station(self):
        station_id = self.make_station()
        first = self.archives.station_status(station_id)["package"]
        second = self.archives.station_status(station_id)["package"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["version"], 1)


if __name__ == "__main__":
    unittest.main()
