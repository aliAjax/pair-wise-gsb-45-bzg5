"""写盘失败后可从完整台账恢复，重放不重复占位。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.dg_repository import DgRepository
from src.dg_service import DgService

from tests.dg_support import DISPATCHER, build, occupancy_codes, plan_data, seed_resources


class FlakyConnection:
    """模拟写盘失败：前 fail_next_commits 次 commit 抛 OSError。"""

    def __init__(self, real, repository):
        self._real = real
        self._repository = repository

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._real.commit()
        else:
            self._real.rollback()
        return False

    def commit(self):
        if self._repository.fail_next_commits > 0:
            self._repository.fail_next_commits -= 1
            raise OSError("模拟写盘失败")
        self._real.commit()

    def rollback(self):
        self._real.rollback()


class FlakyRepository(DgRepository):
    def __init__(self, db_path):
        self.fail_next_commits = 0
        super().__init__(db_path)

    def _connect(self):
        return FlakyConnection(super()._connect(), self)


def occupancy_snapshot(service):
    return sorted((row["plan_id"], row["resource_code"], row["start_hour"], row["end_hour"]) for row in service.occupancy(DISPATCHER))


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "dg.db")
        self.service = build(self.temp.name)
        seed_resources(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_recover_rebuilds_occupancy_and_replay_is_idempotent(self):
        first = self.service.create_plan(DISPATCHER, "DG-301", plan_data(dangerous_class="1", segments=["SEG-1", "SEG-2"]))
        self.assertEqual(self.service.submit(DISPATCHER, first["id"])["outcome"], "reserved")
        second = self.service.create_plan(DISPATCHER, "DG-302", plan_data(vessel="晨光轮", eta_hour=21, etd_hour=24))
        self.assertEqual(self.service.submit(DISPATCHER, second["id"])["outcome"], "reserved")
        before = occupancy_snapshot(self.service)
        self.assertEqual(len(before), 8)

        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM dg_reservations")
        connection.commit()
        connection.close()
        self.assertEqual(occupancy_snapshot(self.service), [])

        first_recover = self.service.recover(DISPATCHER)
        self.assertEqual(first_recover["active_reservations"], len(before))
        self.assertEqual(occupancy_snapshot(self.service), before)
        second_recover = self.service.recover(DISPATCHER)
        self.assertEqual(second_recover["active_reservations"], len(before))
        self.assertEqual(occupancy_snapshot(self.service), before)

        third = self.service.create_plan(DISPATCHER, "DG-303", plan_data(vessel="远航轮"))
        result = self.service.submit(DISPATCHER, third["id"])
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(occupancy_snapshot(self.service), before)

    def test_failed_write_leaves_ledger_consistent(self):
        repository = FlakyRepository(str(Path(self.temp.name) / "flaky.db"))
        service = DgService(repository)
        seed_resources(service)
        plan = service.create_plan(DISPATCHER, "DG-311", plan_data())
        repository.fail_next_commits = 1
        with self.assertRaises(OSError):
            service.submit(DISPATCHER, plan["id"])
        self.assertEqual(service.get_plan(DISPATCHER, plan["id"])["state"], "draft")
        self.assertEqual(service.occupancy(DISPATCHER), [])
        self.assertEqual([event for event in service.ledger(DISPATCHER) if event["kind"] == "preoccupied"], [])
        result = service.submit(DISPATCHER, plan["id"])
        self.assertEqual(result["outcome"], "reserved")
        self.assertEqual(occupancy_codes(service, plan["id"]), ["BERTH-A", "SEG-1", "TUG-1"])


if __name__ == "__main__":
    unittest.main()
