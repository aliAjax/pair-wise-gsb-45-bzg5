import unittest

from src.ledger import DEFAULT_ESCORT_BY_CLASS, DEFAULT_UKC_BY_CLASS, Ledger
from src.rules import overlaps, validate_params


PARAMS = {
    "vessel": "HaiYun", "dg_class": "1", "draft_m": 10.2, "length_m": 180,
    "channel_id": "C1", "berth_id": "B12", "start_hour": 6, "end_hour": 12,
}


def ready_ledger():
    ledger = Ledger()
    ledger.apply({"event_id": "c1", "type": "resource_registered", "ref": "",
                  "kind": "channel", "id": "C1", "depth_m": 15.0})
    ledger.apply({"event_id": "b1", "type": "resource_registered", "ref": "",
                  "kind": "berth", "id": "B12", "depth_m": 12.0, "length_m": 220})
    for name in ("T1", "T2", "T3"):
        ledger.apply({"event_id": "t-" + name, "type": "resource_registered", "ref": "",
                      "kind": "tug", "id": name, "power_kn": 60})
    return ledger


class RulesTest(unittest.TestCase):
    def setUp(self):
        from src.rules import DomainRules
        self.rules = DomainRules()

    def test_defaults_by_dangerous_class(self):
        self.assertEqual(DEFAULT_UKC_BY_CLASS["1"], 1.0)
        self.assertEqual(DEFAULT_ESCORT_BY_CLASS["1"], 3)
        self.assertEqual(DEFAULT_ESCORT_BY_CLASS["9"], 1)

    def test_validate_params_window(self):
        params = validate_params(PARAMS)
        self.assertEqual(params["dg_class"], "1")
        bad = dict(PARAMS, end_hour=6)
        with self.assertRaises(Exception):
            validate_params(bad)

    def test_overlap_half_open(self):
        self.assertTrue(overlaps(6, 12, 11, 14))
        self.assertFalse(overlaps(6, 12, 12, 18))

    def test_release_grants_all_three_resources_atomically(self):
        verdict, detail = self.rules.evaluate_release(ready_ledger(), validate_params(PARAMS))
        self.assertEqual(verdict, "granted")
        kinds = sorted(h["resource_type"] for h in detail["holds"])
        self.assertEqual(kinds, ["berth", "channel", "tug", "tug", "tug"])
        self.assertEqual(detail["basis"]["required_ukc_m"], 1.0)

    def test_blocked_collects_every_missing_reason(self):
        # 不登记任何资源：航道/泊位缺失 + 拖轮不足，一次全部列出
        verdict, detail = self.rules.evaluate_release(Ledger(), validate_params(PARAMS))
        self.assertEqual(verdict, "blocked")
        codes = {r["code"] for r in detail}
        self.assertIn("channel_missing", codes)
        self.assertIn("berth_missing", codes)
        self.assertIn("escort_shortage", codes)

    def test_closure_and_ukc_shortage_are_blockers(self):
        ledger = ready_ledger()
        ledger.apply({"event_id": "n1", "type": "closure_published", "ref": "",
                      "notice_id": "NB-1", "segments": ["C1"],
                      "start_hour": 8, "end_hour": 10, "reason": "临时封航"})
        verdict, detail = self.rules.evaluate_release(ledger, validate_params(PARAMS))
        self.assertEqual(verdict, "blocked")
        self.assertIn("closure_active", {r["code"] for r in detail})

        shallow = ready_ledger()
        verdict, detail = self.rules.evaluate_release(
            shallow, validate_params(dict(PARAMS, draft_m=11.7)))
        self.assertEqual(verdict, "blocked")
        ukc = next(r for r in detail if r["code"] == "ukc_insufficient")
        self.assertLess(ukc["actual_ukc_m"], ukc["required_ukc_m"])

    def test_two_tugs_short_only(self):
        ledger = ready_ledger()
        # 占用一艘拖轮后，1类危险品仍需3艘 -> 缺2艘，占位信息不产生
        ledger.apply({"event_id": "p", "type": "plan_registered", "ref": "OTHER",
                      "params": validate_params(dict(PARAMS, channel_id="C2", berth_id="B9"))})
        ledger.apply({"event_id": "g", "type": "holds_granted", "ref": "OTHER", "revision": 1,
                      "holds": [{"resource_type": "tug", "resource_id": "T1",
                                 "start_hour": 6, "end_hour": 7}],
                      "basis": {}})
        verdict, detail = self.rules.evaluate_release(ledger, validate_params(PARAMS))
        self.assertEqual(verdict, "blocked")
        self.assertEqual(next(r["code"] for r in detail), "escort_shortage")


if __name__ == "__main__":
    unittest.main()
