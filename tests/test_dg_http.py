"""危险品台账 HTTP 接口冒烟测试。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import build_dg_service, build_service
from src.http_api import create_server


HEADERS = {"X-User-Id": "dispatcher-li", "X-Role": "dispatcher", "Content-Type": "application/json"}


class DgHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(cls.temp.name) / "http.db")
        cls.server = create_server("127.0.0.1", 0, build_service(db_path), Path(__file__).resolve().parent.parent / "static", build_dg_service(db_path))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        cls.temp.cleanup()

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, headers=HEADERS, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_plan_flow_over_http(self):
        status, _ = self._call("POST", "/api/dg/resources", {"kind": "channel_segment", "code": "SEG-1", "attrs": {"depth_m": 12.0}})
        self.assertEqual(status, 201)
        status, _ = self._call("POST", "/api/dg/resources", {"kind": "berth", "code": "BERTH-A", "attrs": {"length_m": 220, "depth_m": 11.5, "allowed_dg_classes": ["3"]}})
        self.assertEqual(status, 201)
        status, _ = self._call("POST", "/api/dg/resources", {"kind": "tug", "code": "TUG-1", "attrs": {}})
        self.assertEqual(status, 201)
        status, plan = self._call("POST", "/api/dg/plans", {"reference": "DG-HTTP-1", "data": {"vessel": "安澜轮", "vessel_length_m": 180, "dangerous_class": "3", "draft_m": 9.0, "ukc_required_m": 0.5, "eta_hour": 8, "etd_hour": 20, "segments": ["SEG-1"], "berth": "BERTH-A"}})
        self.assertEqual(status, 201)
        self.assertEqual(plan["state"], "draft")
        status, outcome = self._call("POST", "/api/dg/plans/%d/submit" % plan["id"])
        self.assertEqual(status, 200)
        self.assertEqual(outcome["outcome"], "reserved")
        status, occupancy = self._call("GET", "/api/dg/occupancy")
        self.assertEqual(len(occupancy["items"]), 3)
        status, released = self._call("POST", "/api/dg/plans/%d/release" % plan["id"])
        self.assertEqual(released["state"], "released")
        status, closure = self._call("POST", "/api/dg/closures", {"notice_no": "NT-HTTP-1", "segments": ["SEG-1"], "start_hour": 10, "end_hour": 18, "reason": "临时封航"})
        self.assertEqual(status, 201)
        self.assertEqual(closure["affected"][0]["outcome"], "pending")
        status, plan = self._call("GET", "/api/dg/plans/%d" % plan["id"])
        self.assertEqual(plan["state"], "pending")
        status, ledger = self._call("GET", "/api/dg/ledger")
        self.assertTrue(any(event["kind"] == "voided" for event in ledger["items"]))


if __name__ == "__main__":
    unittest.main()
