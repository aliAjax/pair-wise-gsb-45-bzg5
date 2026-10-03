"""两位调度员同时提交同一航道区段或泊位：先到的一版生效，落后者留下草稿和冲突。"""
import tempfile
import threading
import unittest

from tests.dg_support import DISPATCHER, DISPATCHER_B, build, occupancy_codes, plan_data, seed_resources


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build(self.temp.name)
        seed_resources(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def _two_plans(self):
        first = self.service.create_plan(DISPATCHER, "DG-201", plan_data())
        second = self.service.create_plan(DISPATCHER_B, "DG-202", plan_data(vessel="海川轮"))
        return first, second

    def test_sequential_first_wins_loser_keeps_draft_and_conflict(self):
        first, second = self._two_plans()
        self.assertEqual(self.service.submit(DISPATCHER, first["id"])["outcome"], "reserved")
        result = self.service.submit(DISPATCHER_B, second["id"])
        self.assertEqual(result["outcome"], "conflict")
        loser = result["plan"]
        self.assertEqual(loser["state"], "draft")
        details = sorted(item["detail"] for item in result["conflicts"])
        self.assertEqual(details, ["泊位BERTH-A已被计划DG-201占用", "航道区段SEG-1已被计划DG-201占用"])
        conflicts = self.service.list_conflicts(DISPATCHER)
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(item["plan_id"] == second["id"] and item["holder_plan_id"] == first["id"] for item in conflicts))
        self.assertEqual(occupancy_codes(self.service), ["BERTH-A", "SEG-1", "TUG-1"])
        self.assertEqual(occupancy_codes(self.service, second["id"]), [])
        kinds = [event["kind"] for event in self.service.ledger(DISPATCHER)]
        self.assertIn("conflict_recorded", kinds)

    def test_concurrent_submit_only_one_wins(self):
        first, second = self._two_plans()
        barrier = threading.Barrier(2)
        results = {}
        errors = {}

        def worker(key, actor, plan_id):
            try:
                barrier.wait(timeout=10)
                results[key] = self.service.submit(actor, plan_id)
            except Exception as exc:  # pragma: no cover - 失败时输出诊断
                errors[key] = exc

        threads = [
            threading.Thread(target=worker, args=("first", DISPATCHER, first["id"])),
            threading.Thread(target=worker, args=("second", DISPATCHER_B, second["id"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, {})
        outcomes = sorted(item["outcome"] for item in results.values())
        self.assertEqual(outcomes, ["conflict", "reserved"])
        winner_id = first["id"] if results["first"]["outcome"] == "reserved" else second["id"]
        loser_id = second["id"] if winner_id == first["id"] else first["id"]
        self.assertEqual(self.service.get_plan(DISPATCHER, winner_id)["state"], "reserved")
        self.assertEqual(self.service.get_plan(DISPATCHER, loser_id)["state"], "draft")
        conflicts = self.service.list_conflicts(DISPATCHER)
        self.assertTrue(conflicts)
        self.assertTrue(all(item["plan_id"] == loser_id and item["holder_plan_id"] == winner_id for item in conflicts))
        occupancy = self.service.occupancy(DISPATCHER)
        self.assertEqual(len([row for row in occupancy if row["resource_code"] == "BERTH-A"]), 1)
        self.assertEqual(len([row for row in occupancy if row["resource_code"] == "SEG-1"]), 1)

    def test_loser_can_resubmit_after_resources_free(self):
        first, second = self._two_plans()
        self.service.submit(DISPATCHER, first["id"])
        self.assertEqual(self.service.submit(DISPATCHER_B, second["id"])["outcome"], "conflict")
        self.service.release(DISPATCHER, first["id"])
        self.service.berth(DISPATCHER, first["id"])
        self.service.depart(DISPATCHER, first["id"])
        result = self.service.submit(DISPATCHER_B, second["id"])
        self.assertEqual(result["outcome"], "reserved")
        self.assertEqual(occupancy_codes(self.service, second["id"]), ["BERTH-A", "SEG-1", "TUG-1"])


if __name__ == "__main__":
    unittest.main()
