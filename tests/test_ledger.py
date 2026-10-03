import unittest

from src.ledger import BERTED, BLOCKED, DRAFT, RELEASED, Ledger, LedgerError


PARAMS = {
    "vessel": "HaiYun", "dg_class": "1", "draft_m": 10.2, "length_m": 180,
    "channel_id": "C1", "berth_id": "B12", "start_hour": 6, "end_hour": 12,
    "required_tugs": None,
}

GRANT = {
    "revision": 1,
    "holds": [
        {"resource_type": "channel", "resource_id": "C1", "start_hour": 6, "end_hour": 12},
        {"resource_type": "berth", "resource_id": "B12", "start_hour": 6, "end_hour": 12},
        {"resource_type": "tug", "resource_id": "T1", "start_hour": 6, "end_hour": 7},
    ],
    "basis": {"required_ukc_m": 1.0, "required_tugs": 3},
}


def granted_ledger():
    ledger = Ledger()
    ledger.apply({"event_id": "e1", "type": "plan_registered", "ref": "V1", "actor_id": "a",
                  "params": dict(PARAMS)})
    ledger.apply({"event_id": "e2", "type": "holds_granted", "ref": "V1", "actor_id": "a", **GRANT})
    return ledger


class LedgerTest(unittest.TestCase):
    def test_replay_is_idempotent_by_event_id(self):
        events = [
            {"event_id": "e1", "type": "plan_registered", "ref": "V1", "actor_id": "a",
             "params": dict(PARAMS)},
            {"event_id": "e2", "type": "holds_granted", "ref": "V1", "actor_id": "a", **GRANT},
        ]
        ledger = Ledger.replay(events * 3)
        self.assertEqual(ledger.current("V1").state, RELEASED)
        self.assertEqual(len(ledger.effective_holds(ledger.current("V1"))), 3)

    def test_void_releases_holds_and_revision_starts_draft(self):
        ledger = granted_ledger()
        ledger.apply({"event_id": "e3", "type": "arrangement_voided", "ref": "V1",
                      "revision": 1, "reason": "封航"})
        ledger.apply({"event_id": "e4", "type": "plan_revised", "ref": "V1",
                      "revision": 2, "params": dict(PARAMS), "actor_id": "a"})
        rev = ledger.current("V1")
        self.assertEqual(rev.revision, 2)
        self.assertEqual(rev.state, DRAFT)
        self.assertEqual(ledger.active_holds(), [])

    def test_berthed_keeps_only_berth_hold(self):
        ledger = granted_ledger()
        ledger.apply({"event_id": "e3", "type": "vessel_berthed", "ref": "V1",
                      "revision": 1, "actual_draft_m": 10.4})
        rev = ledger.current("V1")
        self.assertEqual(rev.state, BERTED)
        holds = ledger.effective_holds(rev)
        self.assertEqual([h.resource_type for h in holds], ["berth"])

    def test_illegal_transition_raises(self):
        ledger = granted_ledger()
        with self.assertRaises(LedgerError):
            ledger.apply({"event_id": "x", "type": "release_blocked", "ref": "V1",
                          "revision": 1, "reasons": []})

    def test_closure_and_safety_basis_accumulate(self):
        ledger = Ledger()
        ledger.apply({"event_id": "n1", "type": "closure_published", "ref": "",
                      "notice_id": "N1", "segments": ["C1"],
                      "start_hour": 8, "end_hour": 10, "reason": "r"})
        ledger.apply({"event_id": "s1", "type": "safety_basis_changed", "ref": "",
                      "ukc_by_class": {"3": 1.5}, "escort_by_class": {}})
        self.assertEqual(ledger.notices[0]["notice_id"], "N1")
        self.assertEqual(ledger.ukc_by_class["3"], 1.5)
        blocked = BLOCKED  # 导出符号检查
        self.assertEqual(blocked, "blocked")


if __name__ == "__main__":
    unittest.main()
