"""危险品船进港调度台账：事件溯源的内存归约器（reducer）。

台账只追加事件（ledger_events），当前状态全部由事件顺序归约得到：
计划修订链、有效占位、待补原因、冲突草稿、封航通知、安全基准。
归约器对同一 event_id 幂等，因此整段台账重放不会重复占位。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# 状态：草稿 / 待补 / 已放行（占位中）/ 已靠泊 / 已完成 / 已作废 / 已取消
DRAFT = "draft"
BLOCKED = "blocked"
RELEASED = "released"
BERTED = "berthed"
COMPLETED = "completed"
VOID = "void"
CANCELLED = "cancelled"

UNBERTHED_STATES = {DRAFT, BLOCKED, RELEASED}

# 各危险品类别（IMDG 1~9）要求的船底富余水深（米）
DEFAULT_UKC_BY_CLASS: Dict[str, float] = {str(i): 0.5 for i in range(1, 10)}
DEFAULT_UKC_BY_CLASS.update({"1": 1.0, "2": 0.9, "7": 0.8})

# 各危险品类别要求的护航拖轮数量
DEFAULT_ESCORT_BY_CLASS: Dict[str, int] = {str(i): 1 for i in range(1, 10)}
DEFAULT_ESCORT_BY_CLASS.update({"1": 3, "2": 2, "7": 2})


class LedgerError(ValueError):
    """事件违反台账约束（属于编程/并发判定错误）。"""


@dataclass
class Hold:
    resource_type: str  # channel / berth / tug
    resource_id: str
    start_hour: int
    end_hour: int
    plan_ref: str = ""
    revision: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "plan_ref": self.plan_ref,
            "revision": self.revision,
        }


@dataclass
class Revision:
    plan_ref: str
    revision: int
    state: str
    params: Dict[str, Any]
    actor_id: str
    reasons: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    granted_holds: List[Hold] = field(default_factory=list)
    basis: Optional[Dict[str, Any]] = None


class Ledger:
    def __init__(self) -> None:
        self.resources: Dict[tuple, Dict[str, Any]] = {}
        self.notices: List[Dict[str, Any]] = []
        self.ukc_by_class: Dict[str, float] = dict(DEFAULT_UKC_BY_CLASS)
        self.escort_by_class: Dict[str, int] = dict(DEFAULT_ESCORT_BY_CLASS)
        self.revisions: Dict[tuple, Revision] = {}
        self.latest: Dict[str, int] = {}
        self.events: List[Dict[str, Any]] = []
        self._applied: set = set()

    # ---- 归约入口 ----
    _ENVELOPE_KEYS = ("event_id", "type", "ref", "actor_id", "created_at")

    def apply(self, event: Dict[str, Any]) -> None:
        event_id = event.get("event_id")
        if event_id in self._applied:
            # 重放同一事件不产生任何副作用
            return
        self._applied.add(event_id)
        self.events.append(event)
        etype = event["type"]
        ref = event.get("ref")
        # 载荷为扁平字段（去掉信封键）；历史嵌套 payload 也兼容
        p = {k: v for k, v in event.items() if k not in self._ENVELOPE_KEYS and k != "payload"}
        if event.get("payload") and isinstance(event["payload"], dict):
            p.update(event["payload"])
        handler = getattr(self, "_on_" + etype, None)
        if handler is None:
            raise LedgerError("未知台账事件: %s" % etype)
        handler(ref or "", p)

    @classmethod
    def replay(cls, events: List[Dict[str, Any]]) -> "Ledger":
        ledger = cls()
        for event in events:
            ledger.apply(event)
        return ledger

    # ---- 计划类事件 ----
    def _on_plan_registered(self, ref: str, p: Dict[str, Any]) -> None:
        if ref in self.latest:
            raise LedgerError("计划编号已存在: %s" % ref)
        self.latest[ref] = 1
        self.revisions[(ref, 1)] = Revision(ref, 1, DRAFT, dict(p["params"]), p.get("actor_id", ""))

    def _rev(self, ref: str, number: int) -> Revision:
        rev = self.revisions.get((ref, number))
        if rev is None:
            raise LedgerError("计划修订不存在: %s#%s" % (ref, number))
        return rev

    def _on_holds_granted(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state not in (DRAFT, BLOCKED):
            raise LedgerError("仅草稿/待补计划可以放行占位")
        rev.state = RELEASED
        rev.reasons = []
        rev.conflicts = []
        rev.granted_holds = [self._hold(ref, p["revision"], h) for h in p["holds"]]
        rev.basis = dict(p.get("basis") or {})

    def _on_release_blocked(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state not in (DRAFT, BLOCKED):
            raise LedgerError("仅草稿/待补计划可以重新判缺")
        rev.state = BLOCKED
        rev.reasons = list(p.get("reasons") or [])
        rev.conflicts = []

    def _on_conflict_draft_kept(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state not in (DRAFT, BLOCKED, RELEASED):
            raise LedgerError("当前状态不允许保留冲突草稿")
        rev.state = DRAFT
        rev.reasons = []
        rev.conflicts = list(p.get("conflicts") or [])

    def _on_vessel_berthed(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state != RELEASED:
            raise LedgerError("仅已放行计划可以靠泊")
        rev.state = BERTED
        # 靠泊后航道与拖轮占位自然释放，靠泊依据冻结
        if p.get("actual_draft_m") is not None:
            rev.params = dict(rev.params)
            rev.params["actual_draft_m"] = p["actual_draft_m"]

    def _on_vessel_departed(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state != BERTED:
            raise LedgerError("仅已靠泊计划可以离泊")
        rev.state = COMPLETED

    def _on_arrangement_voided(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        rev.state = VOID
        rev.reasons = [{"code": "voided", "message": p.get("reason", "安排作废")}]
        rev.conflicts = []
        rev.granted_holds = []

    def _on_plan_revised(self, ref: str, p: Dict[str, Any]) -> None:
        number = int(p["revision"])
        if (ref, number) in self.revisions:
            raise LedgerError("修订版本已存在")
        self.latest[ref] = number
        self.revisions[(ref, number)] = Revision(
            ref, number, DRAFT, dict(p["params"]), p.get("actor_id", "")
        )

    def _on_plan_cancelled(self, ref: str, p: Dict[str, Any]) -> None:
        rev = self._rev(ref, p["revision"])
        if rev.state not in (DRAFT, BLOCKED, RELEASED):
            raise LedgerError("已靠泊计划不能取消")
        rev.state = CANCELLED
        rev.granted_holds = []

    # ---- 资源 / 通知 / 安全基准事件 ----
    def _on_resource_registered(self, ref: str, p: Dict[str, Any]) -> None:
        key = (p["kind"], p["id"])
        if key in self.resources:
            raise LedgerError("资源已登记: %s/%s" % key)
        attrs = dict(p)
        attrs.pop("kind", None)
        attrs.pop("id", None)
        self.resources[key] = attrs

    def _on_closure_published(self, ref: str, p: Dict[str, Any]) -> None:
        if any(n["notice_id"] == p["notice_id"] for n in self.notices):
            raise LedgerError("封航通知编号已存在: %s" % p["notice_id"])
        self.notices.append(dict(p))

    def _on_safety_basis_changed(self, ref: str, p: Dict[str, Any]) -> None:
        for key, value in (p.get("ukc_by_class") or {}).items():
            self.ukc_by_class[str(key)] = float(value)
        for key, value in (p.get("escort_by_class") or {}).items():
            self.escort_by_class[str(key)] = int(value)

    @staticmethod
    def _hold(ref: str, revision: int, data: Dict[str, Any]) -> Hold:
        return Hold(
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            start_hour=int(data["start_hour"]),
            end_hour=int(data["end_hour"]),
            plan_ref=ref,
            revision=revision,
        )

    # ---- 查询 ----
    def current(self, ref: str) -> Revision:
        number = self.latest.get(ref)
        if number is None:
            raise LedgerError("计划不存在: %s" % ref)
        return self._rev(ref, number)

    def current_revisions(self) -> List[Revision]:
        return [self.revisions[(ref, number)] for ref, number in self.latest.items()]

    @staticmethod
    def effective_holds(rev: Revision) -> List[Hold]:
        """某修订当前真正生效的占位：靠泊后只剩泊位占用。"""
        if rev.state == RELEASED:
            return list(rev.granted_holds)
        if rev.state == BERTED:
            return [h for h in rev.granted_holds if h.resource_type == "berth"]
        return []

    def active_holds(self, exclude_ref: str = "") -> List[Hold]:
        holds: List[Hold] = []
        for rev in self.current_revisions():
            if rev.plan_ref == exclude_ref:
                continue
            holds.extend(self.effective_holds(rev))
        return holds

    def resource(self, kind: str, resource_id: str) -> Optional[Dict[str, Any]]:
        return self.resources.get((kind, resource_id))

    def tugs(self) -> List[str]:
        return sorted(rid for (kind, rid) in self.resources if kind == "tug")

    def required_tugs(self, params: Dict[str, Any]) -> int:
        if params.get("required_tugs") is not None:
            return int(params["required_tugs"])
        return int(self.escort_by_class.get(str(params["dg_class"]), 1))

    def required_ukc(self, params: Dict[str, Any]) -> float:
        return float(self.ukc_by_class.get(str(params["dg_class"]), 0.5))
