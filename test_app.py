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
        self.vid = self.version["id"]
        self.copy1 = self.store.add_copy("owner", self.vid, "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.vid, "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_healthy_copies_means_protected(self):
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(detail["protection"]["state"], "protected")
        self.assertEqual(detail["version"]["state"], "verified")

    def test_new_version_without_copies_is_at_risk(self):
        self.store.ingest_version("owner", self.archive["id"], [
            {"path": "a.txt", "content_b64": base64.b64encode(b"a").decode()},
        ])
        status = self.store.archive_status("owner", self.archive["id"])
        v2 = next(v for v in status["versions"] if v["version"] == 2)
        self.assertEqual(v2["protection"]["state"], "at_risk")
        self.assertEqual(v2["state"], "degraded")
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(detail["protection"]["state"], "protected")

    def test_replacement_flow_damage_record_retire_rebuild_verify_takeover(self):
        # 1. 巡检发现损坏：留下损坏清单与发现时间，原副本待退役，原介质不被覆写
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "pending_retirement")
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        self.assertIn("found_at", result)
        self.assertIsNotNone(result["found_at"])
        self.assertNotIn("repaired", result)

        detail = self.store.get_version("owner", self.vid)
        reports = detail["damage_reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["copy_id"], self.copy1)
        self.assertEqual(reports[0]["corrupt_paths"], ["records/one.xml"])
        self.assertEqual(reports[0]["state"], "open")
        self.assertTrue(reports[0]["found_at"])
        # 只有一份健康副本：不受保护，页面/响应必须点明缺少的保障
        self.assertEqual(detail["protection"]["state"], "at_risk")
        self.assertEqual(detail["protection"]["healthy_copy_count"], 1)
        self.assertTrue(detail["protection"]["missing_safeguards"])
        self.assertEqual(detail["protection"]["pending_retirement_copies"], [self.copy1])
        self.assertEqual(detail["version"]["state"], "degraded")
        # 原位置上的损坏内容没有被就地修复
        copy1_state = next(c for c in detail["copies"] if c["id"] == self.copy1)
        self.assertEqual(copy1_state["state"], "pending_retirement")

        # 巡检权限：auditor 可以发现损坏，但不能登记替换位置
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_replace_copy("auditor", self.vid, "offline-disk-c")
        self.assertEqual(ctx.exception.status, 403)
        # 未授权用户同样不行
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_replace_copy("outsider", self.vid, "offline-disk-c")
        self.assertEqual(ctx.exception.status, 403)

        # 2. 管理员登记新的存储位置，新副本进入重建中（尚未接替）
        reg = self.store.register_replace_copy("owner", self.vid, "offline-disk-c", self.copy1)
        new_id = reg["id"]
        self.assertEqual(reg["state"], "rebuilding")
        self.assertEqual(reg["replaces_copy_id"], self.copy1)
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == new_id)["state"], "rebuilding")
        # 重建中仍只有 1 份健康副本，仍不受保护
        self.assertEqual(detail["protection"]["state"], "at_risk")

        # 3. 新介质上又损坏：完整校验不通过，不能接替；重建后再校验通过才接替
        self.store.simulate_corruption("owner", new_id, "README.txt")
        failed = self.store.complete_replacement("owner", new_id)
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["state"], "rebuilding")
        self.assertEqual(failed["mismatched"], ["README.txt"])
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == self.copy1)["state"], "pending_retirement")
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == new_id)["state"], "rebuilding")

        self.store.rebuild_copy("owner", new_id)
        ok = self.store.complete_replacement("owner", new_id)
        self.assertTrue(ok["passed"])
        self.assertEqual(ok["state"], "healthy")
        self.assertEqual(ok["retired_copy_id"], self.copy1)
        self.assertEqual(ok["retired_location"], "offline-disk-a")
        self.assertEqual(ok["protection"]["state"], "protected")

        # 4. 接替后：旧副本退役、损坏清单闭环、版本恢复受保护
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == self.copy1)["state"], "retired")
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == new_id)["state"], "healthy")
        self.assertEqual(detail["damage_reports"][0]["state"], "resolved")
        self.assertEqual(detail["damage_reports"][0]["replacement_copy_id"], new_id)
        self.assertTrue(detail["damage_reports"][0]["resolved_at"])
        self.assertEqual(detail["protection"]["state"], "protected")
        self.assertEqual(detail["version"]["state"], "verified")

        # 5. 旧位置全局封禁：任何版本/档案都不能再登记副本
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.vid, "offline-disk-a")
        self.assertEqual(ctx.exception.code, "location_retired")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_replace_copy("owner", self.vid, "offline-disk-a")
        self.assertEqual(ctx.exception.code, "location_retired")
        other = self.store.create_archive("owner", "第二档案馆藏", (date.today() + timedelta(days=365)).isoformat())
        other_v = self.store.ingest_version("owner", other["id"], [
            {"path": "x.txt", "content_b64": base64.b64encode(b"x").decode()}])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", other_v["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "location_retired")

    def test_no_healthy_donor_blocks_replacement(self):
        # 两份副本都损坏：没有健康副本可供重建
        self.store.simulate_corruption("owner", self.copy1, "README.txt")
        self.store.verify_copy("owner", self.copy1)
        self.store.simulate_corruption("owner", self.copy2, "README.txt")
        self.store.verify_copy("owner", self.copy2)
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_replace_copy("owner", self.vid, "offline-disk-c")
        self.assertEqual(ctx.exception.code, "no_healthy_donor")

    def test_replacement_requires_pending_copy(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_replace_copy("owner", self.vid, "offline-disk-c")
        self.assertEqual(ctx.exception.code, "no_pending_retirement")

    def test_retired_copy_is_excluded_from_inspection(self):
        self.store.simulate_corruption("owner", self.copy1, "README.txt")
        self.store.verify_copy("owner", self.copy1)
        new_id = self.store.register_replace_copy("owner", self.vid, "offline-disk-c", self.copy1)["id"]
        self.store.complete_replacement("owner", new_id)
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_copy("owner", self.copy1)
        self.assertEqual(ctx.exception.code, "copy_retired")
        with self.assertRaises(BusinessError) as ctx:
            self.store.simulate_corruption("owner", self.copy1, "README.txt")
        self.assertEqual(ctx.exception.code, "copy_retired")

    def test_rebuilding_copy_cannot_be_inspected_as_regular(self):
        self.store.simulate_corruption("owner", self.copy1, "README.txt")
        self.store.verify_copy("owner", self.copy1)
        new_id = self.store.register_replace_copy("owner", self.vid, "offline-disk-c", self.copy1)["id"]
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_copy("owner", new_id)
        self.assertEqual(ctx.exception.code, "copy_rebuilding")

    def test_format_migration_still_works(self):
        migrated = self.store.migrate(
            "owner", self.vid, "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        # 新版本还没有副本：必须显示缺少保障
        self.assertEqual(detail["protection"]["state"], "at_risk")
        self.assertEqual(detail["version"]["state"], "degraded")
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.vid)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.vid, "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")

    def test_repeated_inspections_record_separate_damage_reports(self):
        self.store.simulate_corruption("owner", self.copy1, "README.txt")
        first = self.store.verify_copy("owner", self.copy1)
        second = self.store.verify_copy("owner", self.copy1)
        self.assertIsNotNone(first["damage_report_id"])
        self.assertIsNotNone(second["damage_report_id"])
        self.assertNotEqual(first["damage_report_id"], second["damage_report_id"])
        detail = self.store.get_version("owner", self.vid)
        self.assertEqual(len([r for r in detail["damage_reports"] if r["state"] == "open"]), 2)


if __name__ == "__main__":
    unittest.main()
