import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.ledger import Ledger


def params(**overrides):
    base = {
        "vessel": "HaiYun", "dg_class": "1", "draft_m": 10.2, "length_m": 180,
        "channel_id": "C1", "berth_id": "B12", "start_hour": 6, "end_hour": 12,
    }
    base.update(overrides)
    return base


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        actor = Actor("ops", "dispatcher")
        self.service.register_resource(actor, {"kind": "channel", "id": "C1", "depth_m": 15.0})
        self.service.register_resource(actor, {"kind": "berth", "id": "B12", "depth_m": 12.0, "length_m": 220})
        for tug in ("T1", "T2", "T3", "T4", "T5", "T6"):
            self.service.register_resource(actor, {"kind": "tug", "id": tug, "power_kn": 60})

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_denied_and_duplicate_plan(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_plan(Actor("x", "outsider"), "V-1", params())
        self.service.register_plan(Actor("a", "dispatcher"), "V-1", params())
        with self.assertRaises(Conflict):
            self.service.register_plan(Actor("a", "dispatcher"), "V-1", params())

    def test_concurrent_same_channel_first_wins_loser_keeps_draft(self):
        self.service.register_plan(Actor("d1", "dispatcher"), "V-1", params())
        self.service.register_plan(Actor("d2", "dispatcher"), "V-2", params())

        barrier = threading.Barrier(2)
        results = {}

        def submit(key, ref):
            barrier.wait()
            try:
                results[key] = self.service.release_plan(Actor(key, "dispatcher"), ref)
            except Exception as exc:  # noqa: BLE001
                results[key] = exc

        t1 = threading.Thread(target=submit, args=("d1", "V-1"))
        t2 = threading.Thread(target=submit, args=("d2", "V-2"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        states = {key: value["state"] for key, value in results.items()}
        self.assertIn("released", states.values())
        self.assertIn("draft", states.values())
        winner = next(k for k, v in states.items() if v == "released")
        loser = next(k for k, v in states.items() if v == "draft")
        loser_plan = results[loser]
        self.assertEqual(loser_plan["conflicts"][0]["held_by"]["plan_ref"], "V-%s" % (1 if winner == "d1" else 2))
        # 只有胜出者占位，落后者没有任何预占
        holders = {h["plan_ref"] for h in self.service.list_holds(Actor("a", "dispatcher"))}
        self.assertEqual(holders, {"V-%s" % (1 if winner == "d1" else 2)})
        # 落后者补料后可在自己的草稿上重新放行（让出后）
        self.service.cancel_plan(Actor(winner, "dispatcher"), "V-%s" % (1 if winner == "d1" else 2))
        again = self.service.release_plan(Actor(loser, "dispatcher"), "V-%s" % (2 if loser == "d2" else 1))
        self.assertEqual(again["state"], "released")

    def test_idempotent_key_does_not_double_hold(self):
        self.service.register_plan(Actor("a", "dispatcher"), "V-7", params())
        first = self.service.release_plan(Actor("a", "dispatcher"), "V-7", request_id="req-7")
        self.assertEqual(first["state"], "released")
        # 同一幂等键重试：返回同一结果，事件/占位不增加
        second = self.service.release_plan(Actor("a", "dispatcher"), "V-7", request_id="req-7")
        self.assertEqual(second["idempotent_replay"], True)
        self.assertEqual(second["revision"], first["revision"])
        holds = self.service.list_holds(Actor("a", "dispatcher"))
        self.assertEqual(len(holds), 5)

    def test_write_failure_rolls_back_then_retry_succeeds(self):
        self.service.register_plan(Actor("a", "dispatcher"), "V-8", params())
        self.service.repository.crash_before_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            self.service.release_plan(Actor("a", "dispatcher"), "V-8", request_id="req-8")
        # 失败后没有占位、事件未落地
        self.assertEqual(self.service.list_holds(Actor("a", "dispatcher")), [])
        plan = self.service.get_plan(Actor("a", "dispatcher"), "V-8")
        self.assertEqual(plan["state"], "draft")

        self.service.repository.crash_before_commit = False
        ok = self.service.release_plan(Actor("a", "dispatcher"), "V-8", request_id="req-8")
        self.assertEqual(ok["state"], "released")
        self.assertEqual(len(self.service.list_holds(Actor("a", "dispatcher"))), 5)

    def test_rebuild_from_journal_is_stable_and_dedupes(self):
        self.service.register_plan(Actor("a", "dispatcher"), "V-9", params(start_hour=6, end_hour=10))
        self.service.release_plan(Actor("a", "dispatcher"), "V-9")
        self.service.publish_closure(Actor("a", "dispatcher"), {
            "notice_id": "NB-X", "segments": ["C1"], "start_hour": 7, "end_hour": 9, "reason": "封航",
        })
        before = sorted(h["plan_ref"] + str(h["resource_id"]) for h in self.service.list_holds(Actor("a", "dispatcher")))
        count1 = self.service.rebuild(Actor("a", "dispatcher"))["replayed_events"]
        count2 = self.service.rebuild(Actor("a", "dispatcher"))["replayed_events"]
        self.assertEqual(count1, count2)
        after = sorted(h["plan_ref"] + str(h["resource_id"]) for h in self.service.list_holds(Actor("a", "dispatcher")))
        self.assertEqual(before, after)

        # 用全新仓库从同一份事件日志重放，结果一致且不重复占位
        ledger = Ledger.replay  # 模块级可用性检查
        from src.repository import Repository
        fresh_ledger = Repository(self.db_path).load_ledger()
        self.assertEqual(len(fresh_ledger.events), count1)
        self.assertEqual(fresh_ledger.current("V-9").state, "blocked")


if __name__ == "__main__":
    unittest.main()
