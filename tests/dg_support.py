"""危险品船进港台账测试的公共构造。"""
from pathlib import Path

from src.dg_repository import DgRepository
from src.dg_service import DgService
from src.domain import Actor


DISPATCHER = Actor("dispatcher-li", "dispatcher")
DISPATCHER_B = Actor("dispatcher-wang", "dispatcher")
OUTSIDER = Actor("visitor", "outsider")


def build(tmpdir: str) -> DgService:
    return DgService(DgRepository(str(Path(tmpdir) / "dg.db")))


def seed_resources(service: DgService) -> None:
    service.register_resource(DISPATCHER, {"kind": "channel_segment", "code": "SEG-1", "attrs": {"depth_m": 12.0}})
    service.register_resource(DISPATCHER, {"kind": "channel_segment", "code": "SEG-2", "attrs": {"depth_m": 10.0}})
    service.register_resource(DISPATCHER, {"kind": "berth", "code": "BERTH-A", "attrs": {"length_m": 220, "depth_m": 11.5, "allowed_dg_classes": ["1", "3"]}})
    service.register_resource(DISPATCHER, {"kind": "berth", "code": "BERTH-B", "attrs": {"length_m": 150, "depth_m": 9.0, "allowed_dg_classes": ["1", "2", "4"]}})
    service.register_resource(DISPATCHER, {"kind": "tug", "code": "TUG-1", "attrs": {}})
    service.register_resource(DISPATCHER, {"kind": "tug", "code": "TUG-2", "attrs": {}})


def plan_data(**overrides):
    data = {
        "vessel": "安澜轮",
        "vessel_length_m": 180,
        "dangerous_class": "3",
        "draft_m": 9.0,
        "ukc_required_m": 0.5,
        "eta_hour": 8,
        "etd_hour": 20,
        "segments": ["SEG-1"],
        "berth": "BERTH-A",
    }
    data.update(overrides)
    return data


def occupancy_codes(service: DgService, plan_id: int = None):
    rows = service.occupancy(DISPATCHER)
    if plan_id is not None:
        rows = [row for row in rows if row["plan_id"] == plan_id]
    return sorted(row["resource_code"] for row in rows)
