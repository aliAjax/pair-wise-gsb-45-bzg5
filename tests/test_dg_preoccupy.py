"""放行前一起预占：全部满足才占位，缺哪项就停住并留下待补原因。"""
import tempfile
import unittest

from src.domain import Conflict, PermissionDenied

from tests.dg_support import DISPATCHER, OUTSIDER, build, occupancy_codes, plan_data, seed_resources


class PreoccupyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build(self.temp.name)
        seed_resources(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_resources_preoccupied_together(self):
        plan = self.service.create_plan(DISPATCHER, "DG-001", plan_data(dangerous_class="1"))
        self.assertEqual(plan["state"], "draft")
        self.assertEqual(plan["payload"]["tugs_required"], 2)
        result = self.service.submit(DISPATCHER, plan["id"])
        self.assertEqual(result["outcome"], "reserved")
        self.assertEqual(result["plan"]["state"], "reserved")
        self.assertEqual(occupancy_codes(self.service, plan["id"]), ["BERTH-A", "SEG-1", "TUG-1", "TUG-2"])
        preoccupied = [event for event in self.service.ledger(DISPATCHER) if event["kind"] == "preoccupied"]
        self.assertEqual(len(preoccupied), 1)
        self.assertEqual(len(preoccupied[0]["payload"]["reservations"]), 4)

    def test_missing_items_stop_with_pending_reasons(self):
        plan = self.service.create_plan(DISPATCHER, "DG-002", plan_data(segments=["SEG-1", "SEG-9"], berth="BERTH-B"))
        result = self.service.submit(DISPATCHER, plan["id"])
        self.assertEqual(result["outcome"], "pending")
        reasons = result["reasons"]
        self.assertTrue(any("SEG-9未登记" in reason for reason in reasons))
        self.assertTrue(any("长度不足" in reason for reason in reasons))
        self.assertTrue(any("富余水深不足" in reason for reason in reasons))
        self.assertTrue(any("不接受危险品类别3" in reason for reason in reasons))
        plan = self.service.get_plan(DISPATCHER, plan["id"])
        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["payload"]["pending_reasons"], reasons)
        self.assertEqual(occupancy_codes(self.service), [])

    def test_tug_shortage_is_pending_reason(self):
        first = self.service.create_plan(DISPATCHER, "DG-010", plan_data(dangerous_class="1"))
        self.assertEqual(self.service.submit(DISPATCHER, first["id"])["outcome"], "reserved")
        second = self.service.create_plan(DISPATCHER, "DG-011", plan_data(vessel="海川轮", vessel_length_m=120, dangerous_class="1", draft_m=8.0, segments=["SEG-2"], berth="BERTH-B"))
        result = self.service.submit(DISPATCHER, second["id"])
        self.assertEqual(result["outcome"], "pending")
        self.assertEqual(result["reasons"], ["护航拖轮不足：需要2艘，可用0艘"])
        self.assertEqual(occupancy_codes(self.service, second["id"]), [])

    def test_release_requires_full_preoccupy(self):
        plan = self.service.create_plan(DISPATCHER, "DG-020", plan_data())
        with self.assertRaises(Conflict):
            self.service.release(DISPATCHER, plan["id"])
        blocked = self.service.create_plan(DISPATCHER, "DG-021", plan_data(segments=["SEG-9"]))
        self.assertEqual(self.service.submit(DISPATCHER, blocked["id"])["outcome"], "pending")
        with self.assertRaises(Conflict):
            self.service.release(DISPATCHER, blocked["id"])
        self.assertEqual(self.service.submit(DISPATCHER, plan["id"])["outcome"], "reserved")
        released = self.service.release(DISPATCHER, plan["id"])
        self.assertEqual(released["state"], "released")

    def test_berth_records_basis_and_depart_releases_occupancy(self):
        plan = self.service.create_plan(DISPATCHER, "DG-030", plan_data())
        self.service.submit(DISPATCHER, plan["id"])
        self.service.release(DISPATCHER, plan["id"])
        berthed = self.service.berth(DISPATCHER, plan["id"])
        basis = berthed["payload"]["basis"]
        self.assertEqual(basis["ukc_required_m"], 0.5)
        self.assertEqual(basis["dangerous_class"], "3")
        self.assertEqual(sorted(item["resource_code"] for item in basis["reservations"]), ["BERTH-A", "SEG-1", "TUG-1"])
        departed = self.service.depart(DISPATCHER, plan["id"])
        self.assertEqual(departed["state"], "departed")
        self.assertEqual(occupancy_codes(self.service), [])

    def test_write_roles_are_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(OUTSIDER, "DG-040", plan_data())
        with self.assertRaises(PermissionDenied):
            self.service.register_resource(OUTSIDER, {"kind": "tug", "code": "TUG-9", "attrs": {}})


if __name__ == "__main__":
    unittest.main()
