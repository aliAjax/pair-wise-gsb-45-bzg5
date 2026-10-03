import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


def params(**overrides):
    base = {
        "vessel": "HaiYun", "dg_class": "1", "draft_m": 10.2, "length_m": 180,
        "channel_id": "C1", "berth_id": "B12", "start_hour": 6, "end_hour": 12,
    }
    base.update(overrides)
    return base


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.dispatcher = Actor("disp-a", "dispatcher")

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        self.service.register_resource(Actor("disp-b", "dispatcher"),
                                       {"kind": "channel", "id": "C1", "depth_m": 15.0})
        self.service.register_resource(Actor("disp-b", "dispatcher"),
                                       {"kind": "berth", "id": "B12", "depth_m": 12.0, "length_m": 220})
        for tug in ("T1", "T2", "T3"):
            self.service.register_resource(Actor("disp-b", "dispatcher"),
                                           {"kind": "tug", "id": tug, "power_kn": 60})

    def test_register_blocked_then_resupply_then_full_lifecycle(self):
        # 资源尚未登记，先到的放行尝试必须停在 blocked，且不留下任何占位
        plan = self.service.register_plan(self.dispatcher, "V-21001", params())
        self.assertEqual(plan["state"], "draft")
        plan = self.service.release_plan(self.dispatcher, "V-21001")
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue({r["code"] for r in plan["reasons"]} >= {"channel_missing", "berth_missing"})
        self.assertEqual(self.service.list_holds(self.dispatcher), [])

        # 补齐资源后再次放行，航道+泊位+3艘拖轮一起预占
        self._seed()
        plan = self.service.release_plan(self.dispatcher, "V-21001")
        self.assertEqual(plan["state"], "released")
        holds = self.service.list_holds(self.dispatcher)
        self.assertEqual(sorted(h["resource_type"] for h in holds),
                         ["berth", "channel", "tug", "tug", "tug"])

        # 靠泊 -> 航道与拖轮占位释放，仅留泊位；离泊后全部释放
        plan = self.service.berth_plan(self.dispatcher, "V-21001")
        self.assertEqual(plan["state"], "berthed")
        holds = self.service.list_holds(self.dispatcher)
        self.assertEqual([(h["resource_type"], h["resource_id"]) for h in holds], [("berth", "B12")])
        self.service.depart_plan(self.dispatcher, "V-21001")
        self.assertEqual(self.service.list_holds(self.dispatcher), [])

    def test_closure_voids_unberthed_and_keeps_berthed_basis(self):
        self._seed()
        self.service.register_plan(self.dispatcher, "V-A", params(start_hour=6, end_hour=12))
        self.service.release_plan(self.dispatcher, "V-A")
        self.service.register_plan(self.dispatcher, "V-B", params(start_hour=14, end_hour=20))
        self.service.release_plan(self.dispatcher, "V-B")
        self.service.berth_plan(self.dispatcher, "V-B")

        result = self.service.publish_closure(self.dispatcher, {
            "notice_id": "NB-7", "segments": ["C1"], "start_hour": 8, "end_hour": 10,
            "reason": "临时封航",
        })
        # V-A 未靠泊且时间窗命中 -> 作废重排；V-B 已靠泊且不命中 -> 沿用原依据
        self.assertEqual(result["affected_plan_refs"], ["V-A"])
        plan_a = self.service.get_plan(self.dispatcher, "V-A")
        self.assertEqual(plan_a["revision"], 2)
        self.assertEqual(plan_a["state"], "blocked")
        self.assertTrue(any(r["code"] == "closure_active" for r in plan_a["reasons"]))
        old_holds = self.service.list_holds(self.dispatcher)
        self.assertEqual([h["plan_ref"] for h in old_holds], ["V-B"])

        # 封航解除等价的重排：改到不冲突时间窗后立即重新放行
        plan_a = self.service.revise_plan(self.dispatcher, "V-A", params(start_hour=20, end_hour=24))
        self.assertEqual(plan_a["revision"], 3)
        self.assertEqual(plan_a["state"], "released")

        # 已靠泊计划修改危险品类别/吃水等关键字段被拒绝
        from src.domain import Conflict
        with self.assertRaises(Conflict):
            self.service.revise_plan(self.dispatcher, "V-B", params(dg_class="2", start_hour=14, end_hour=20))

    def test_safety_basis_change_only_rechecks_unberthed(self):
        self._seed()
        self.service.register_plan(self.dispatcher, "V-A", params(start_hour=6, end_hour=10))
        self.service.release_plan(self.dispatcher, "V-A")
        self.service.register_plan(self.dispatcher, "V-B", params(start_hour=14, end_hour=20))
        self.service.release_plan(self.dispatcher, "V-B")
        self.service.berth_plan(self.dispatcher, "V-B")
        # 1类富余水深要求从1.0提到2.0：泊位12.0-吃水10.2=1.8不再达标，V-A作废重排；V-B已靠泊不动
        result = self.service.change_safety_basis(self.dispatcher, {"ukc_by_class": {"1": 2.0}})
        self.assertEqual(result["affected_plan_refs"], ["V-A"])
        plan_a = self.service.get_plan(self.dispatcher, "V-A")
        self.assertEqual(plan_a["state"], "blocked")
        self.assertTrue(any(r["code"] == "ukc_insufficient" for r in plan_a["reasons"]))
        plan_b = self.service.get_plan(self.dispatcher, "V-B")
        self.assertEqual(plan_b["state"], "berthed")
        self.assertEqual(plan_b["basis"]["required_ukc_m"], 1.0)

    def test_timeline_records_void_and_revision(self):
        self._seed()
        self.service.register_plan(self.dispatcher, "V-A", params())
        self.service.release_plan(self.dispatcher, "V-A")
        self.service.publish_closure(self.dispatcher, {
            "notice_id": "NB-9", "segments": ["C1"], "start_hour": 8, "end_hour": 10,
            "reason": "临时封航",
        })
        timeline = self.service.timeline(self.dispatcher, "V-A")
        types = [e["type"] for e in timeline["plan_events"]]
        self.assertIn("arrangement_voided", types)
        self.assertIn("plan_revised", types)
        self.assertIn("release_blocked", types)
        self.assertTrue(any(e["type"] == "closure_published" for e in timeline["system_events"]))


if __name__ == "__main__":
    unittest.main()
