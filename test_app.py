import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_corruption_triggers_retirement_and_replacement(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        # 巡检留下损坏清单和发现时间，副本进入待退役，不再原地修复
        self.assertEqual(result["state"], "pending_retirement")
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        self.assertIsNotNone(result["incident_id"])
        self.assertTrue(result["detected_at"])

        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(len(detail["incidents"]), 1)
        incident = detail["incidents"][0]
        self.assertEqual(incident["corrupt_paths"], ["records/one.xml"])
        self.assertEqual(incident["location"], "offline-disk-a")
        self.assertTrue(incident["detected_at"])
        self.assertIsNone(incident["resolved_at"])
        # 只剩一份健康副本：不受保护，并点明缺少的保障
        self.assertFalse(detail["protection"]["protected"])
        self.assertEqual(detail["protection"]["healthy_copies"], 1)
        self.assertTrue(any("冗余" in m for m in detail["protection"]["missing_safeguards"]))
        self.assertTrue(any("待退役" in m for m in detail["protection"]["missing_safeguards"]))
        self.assertEqual(detail["version"]["state"], "degraded")

        # 健康副本不能走替换流程
        with self.assertRaises(BusinessError) as ctx:
            self.store.replace_copy("owner", self.copy2, "offline-disk-c")
        self.assertEqual(ctx.exception.code, "copy_not_pending_retirement")

        # 登记新位置，重建校验通过后新副本接替
        repl = self.store.replace_copy("owner", self.copy1, "offline-disk-c")
        self.assertEqual(repl["state"], "healthy")
        self.assertEqual(repl["verified_files"], 2)
        detail = self.store.get_version("owner", self.version["id"])
        states = {c["location"]: c["state"] for c in detail["copies"]}
        self.assertEqual(states["offline-disk-a"], "retired")
        self.assertEqual(states["offline-disk-b"], "healthy")
        self.assertEqual(states["offline-disk-c"], "healthy")
        self.assertTrue(detail["protection"]["protected"])
        self.assertEqual(detail["protection"]["missing_safeguards"], [])
        self.assertEqual(detail["version"]["state"], "verified")
        self.assertIsNotNone(detail["incidents"][0]["resolved_at"])
        self.assertEqual(detail["incidents"][0]["resolved_by_copy_id"], repl["new_copy_id"])

        # 旧位置不能再登记副本：同版本和其他版本都不行
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "location_retired")
        other = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "notes.txt", "content_b64": base64.b64encode(b"notes").decode()},
        ])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", other["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "location_retired")

        # 已退役副本不再参与巡检，替换时也不能再用退役位置
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_copy("owner", self.copy1)
        self.assertEqual(ctx.exception.code, "copy_retired")

    def test_verify_is_idempotent_for_pending_retirement(self):
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        first = self.store.verify_copy("owner", self.copy1)
        second = self.store.verify_copy("auditor", self.copy1)
        self.assertEqual(second["state"], "pending_retirement")
        self.assertEqual(second["incident_id"], first["incident_id"])
        self.assertEqual(second["detected_at"], first["detected_at"])
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(len(detail["incidents"]), 1)

    def test_healthy_copy_passes_verification(self):
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        result = self.store.verify_copy("auditor", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["corrupt_paths"], [])
        self.assertIsNone(result["incident_id"])

    def test_protection_requires_two_healthy_copies(self):
        version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "a.txt", "content_b64": base64.b64encode(b"a").decode()},
        ])
        detail = self.store.get_version("owner", version["id"])
        self.assertFalse(detail["protection"]["protected"])
        self.assertIn("缺少健康副本", detail["protection"]["missing_safeguards"])
        self.assertEqual(detail["version"]["state"], "degraded")
        self.store.add_copy("owner", version["id"], "offline-disk-x")
        detail = self.store.get_version("owner", version["id"])
        self.assertFalse(detail["protection"]["protected"])
        self.assertTrue(any("冗余" in m for m in detail["protection"]["missing_safeguards"]))
        self.store.add_copy("owner", version["id"], "offline-disk-y")
        detail = self.store.get_version("owner", version["id"])
        self.assertTrue(detail["protection"]["protected"])
        self.assertEqual(detail["version"]["state"], "verified")

    def test_format_migration(self):
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)
        protected = {v["version"]: v["protected"] for v in status["versions"]}
        self.assertTrue(protected[1])
        self.assertFalse(protected[2])

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")
        with self.assertRaises(BusinessError) as ctx:
            self.store.replace_copy("outsider", self.copy1, "offline-disk-c")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
