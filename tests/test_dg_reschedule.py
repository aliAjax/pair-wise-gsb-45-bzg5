"""封航范围、富余水深、危险品类别修改后：未靠泊立即作废重排，已靠泊沿用原依据。"""
import tempfile
import unittest

from tests.dg_support import DISPATCHER, build, occupancy_codes, plan_data, seed_resources


class RescheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build(self.temp.name)
        seed_resources(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def _reserved_plan(self, reference, **overrides):
        plan = self.service.create_plan(DISPATCHER, reference, plan_data(**overrides))
        self.assertEqual(self.service.submit(DISPATCHER, plan["id"])["outcome"], "reserved")
        return plan

    def test_closure_voids_unberthed_but_berthed_keeps_basis(self):
        plan_a = self._reserved_plan("DG-101")
        plan_b = self._reserved_plan("DG-102", vessel="远洋轮", vessel_length_m=120, dangerous_class="4", draft_m=8.0, segments=["SEG-2"], berth="BERTH-B")
        self.service.release(DISPATCHER, plan_b["id"])
        self.service.berth(DISPATCHER, plan_b["id"])
        result = self.service.issue_closure(DISPATCHER, {"notice_no": "NT-1", "segments": ["SEG-1", "SEG-2"], "start_hour": 10, "end_hour": 18, "reason": "临时封航"})
        affected = {entry["reference"]: entry["outcome"] for entry in result["affected"]}
        self.assertEqual(affected, {"DG-101": "pending", "DG-102": "basis_retained"})
        plan_a = self.service.get_plan(DISPATCHER, plan_a["id"])
        self.assertEqual(plan_a["state"], "pending")
        self.assertTrue(any("封航" in reason and "SEG-1" in reason for reason in plan_a["payload"]["pending_reasons"]))
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), [])
        plan_b = self.service.get_plan(DISPATCHER, plan_b["id"])
        self.assertEqual(plan_b["state"], "berthed")
        self.assertEqual(occupancy_codes(self.service, plan_b["id"]), ["BERTH-B", "SEG-2", "TUG-2"])
        retained = [event for event in self.service.ledger(DISPATCHER) if event["kind"] == "basis_retained" and event["plan_id"] == plan_b["id"]]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["payload"]["basis"]["ukc_required_m"], 0.5)

    def test_closure_scope_modified_replans_affected(self):
        plan_a = self._reserved_plan("DG-111")
        closure = self.service.issue_closure(DISPATCHER, {"notice_no": "NT-2", "segments": ["SEG-1"], "start_hour": 10, "end_hour": 18, "reason": "临时封航"})["closure"]
        self.assertEqual(self.service.get_plan(DISPATCHER, plan_a["id"])["state"], "pending")
        result = self.service.modify_closure(DISPATCHER, closure["id"], {"segments": ["SEG-2"]})
        affected = {entry["reference"]: entry["outcome"] for entry in result["affected"]}
        self.assertEqual(affected.get("DG-111"), "reserved")
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), ["BERTH-A", "SEG-1", "TUG-1"])

    def test_ukc_change_replans_immediately(self):
        plan_a = self._reserved_plan("DG-121")
        result = self.service.modify_plan(DISPATCHER, plan_a["id"], {"ukc_required_m": 3.5})
        self.assertEqual(result["outcome"], "pending")
        self.assertTrue(any("富余水深不足" in reason for reason in result["reasons"]))
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), [])
        result = self.service.modify_plan(DISPATCHER, plan_a["id"], {"ukc_required_m": 0.5})
        self.assertEqual(result["outcome"], "reserved")
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), ["BERTH-A", "SEG-1", "TUG-1"])

    def test_dg_class_change_replans_immediately(self):
        plan_a = self._reserved_plan("DG-131")
        result = self.service.modify_plan(DISPATCHER, plan_a["id"], {"dangerous_class": "1"})
        self.assertEqual(result["outcome"], "reserved")
        self.assertEqual(result["plan"]["payload"]["assignments"]["tugs"], ["TUG-1", "TUG-2"])
        result = self.service.modify_plan(DISPATCHER, plan_a["id"], {"dangerous_class": "2"})
        self.assertEqual(result["outcome"], "pending")
        self.assertTrue(any("不接受危险品类别2" in reason for reason in result["reasons"]))

    def test_berthed_plan_keeps_original_basis_on_param_change(self):
        plan_a = self._reserved_plan("DG-141")
        self.service.release(DISPATCHER, plan_a["id"])
        self.service.berth(DISPATCHER, plan_a["id"])
        before = occupancy_codes(self.service, plan_a["id"])
        result = self.service.modify_plan(DISPATCHER, plan_a["id"], {"ukc_required_m": 5.0, "dangerous_class": "1"})
        self.assertEqual(result["outcome"], "basis_retained")
        plan = result["plan"]
        self.assertEqual(plan["state"], "berthed")
        self.assertEqual(plan["payload"]["ukc_required_m"], 0.5)
        self.assertEqual(plan["payload"]["basis"]["ukc_required_m"], 0.5)
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), before)
        retained = [event for event in self.service.ledger(DISPATCHER) if event["kind"] == "basis_retained"]
        self.assertEqual(retained[0]["payload"]["info"]["requested_changes"], {"ukc_required_m": 5.0, "dangerous_class": "1"})

    def test_lift_closure_allows_replan(self):
        plan_a = self._reserved_plan("DG-151")
        closure = self.service.issue_closure(DISPATCHER, {"notice_no": "NT-3", "segments": ["SEG-1"], "start_hour": 10, "end_hour": 18, "reason": "临时封航"})["closure"]
        self.assertEqual(self.service.get_plan(DISPATCHER, plan_a["id"])["state"], "pending")
        result = self.service.lift_closure(DISPATCHER, closure["id"])
        affected = {entry["reference"]: entry["outcome"] for entry in result["affected"]}
        self.assertEqual(affected.get("DG-151"), "reserved")
        self.assertEqual(occupancy_codes(self.service, plan_a["id"]), ["BERTH-A", "SEG-1", "TUG-1"])


if __name__ == "__main__":
    unittest.main()
