"""放行判定规则：航道封闭段、泊位净空、护航拖轮的原子检查。

evaluate_release 是纯函数：依据当前台账快照给出
  ("granted", holds, basis) 或 ("blocked", reasons) 或 ("conflict", conflicts)
不产生任何副作用，调用方据此在一个事务内提交对应事件。
"""
from typing import Any, Dict, List, Tuple

from .domain import ValidationError, choice, integer, number, text, text_list
from .ledger import Hold, Ledger


PLAN_STATES = {"draft", "blocked", "released", "berthed", "completed", "void", "cancelled"}
PROTECTED_FIELDS = ("dg_class", "draft_m", "channel_id", "berth_id", "start_hour", "end_hour")


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """半开时间窗 [start, end) 是否重叠。"""
    return a_start < b_end and b_start < a_end


def validate_params(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    params = {
        "vessel": text(p, "vessel"),
        "dg_class": str(choice(p, "dg_class", [str(i) for i in range(1, 10)])),
        "draft_m": number(p, "draft_m", 0),
        "length_m": number(p, "length_m", 0),
        "channel_id": text(p, "channel_id"),
        "berth_id": text(p, "berth_id"),
        "start_hour": integer(p, "start_hour", 0, 23),
        "end_hour": integer(p, "end_hour", 1, 24),
    }
    if params["end_hour"] <= params["start_hour"]:
        raise ValidationError("end_hour必须晚于start_hour")
    if "required_tugs" in p and p["required_tugs"] is not None:
        params["required_tugs"] = integer(p, "required_tugs", 1, 10)
    else:
        params["required_tugs"] = None
    return params


def validate_resource(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    attrs: Dict[str, Any] = {
        "kind": choice(p, "kind", ["channel", "berth", "tug"]),
        "id": text(p, "id"),
    }
    if attrs["kind"] == "channel":
        attrs["depth_m"] = number(p, "depth_m", 0)
    elif attrs["kind"] == "berth":
        attrs["depth_m"] = number(p, "depth_m", 0)
        attrs["length_m"] = number(p, "length_m", 0)
    else:
        attrs["power_kn"] = number(p, "power_kn", 0)
    return attrs


def validate_notice(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    notice = {
        "notice_id": text(p, "notice_id"),
        "segments": text_list(p, "segments", minimum=1),
        "start_hour": integer(p, "start_hour", 0, 23),
        "end_hour": integer(p, "end_hour", 1, 24),
        "reason": text(p, "reason"),
    }
    if notice["end_hour"] <= notice["start_hour"]:
        raise ValidationError("封航时间窗无效")
    return notice


class DomainRules:
    def known_role(self, role: str) -> bool:
        return role in {"admin", "dispatcher", "port_controller"}

    def role_can_write(self, role: str) -> bool:
        return role in {"admin", "dispatcher", "port_controller"}

    # ---- 放行判定 ----
    def evaluate_release(self, ledger: Ledger, params: Dict[str, Any]) -> Tuple[str, Any]:
        reasons: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []

        required_holds = self._required_holds(params, ledger.required_tugs(params))
        active = ledger.active_holds()

        # 1) 资源占用冲突：同一航道区段 / 泊位（拖轮按具体拖轮）
        for need in required_holds:
            for hold in active:
                if (hold.resource_type, hold.resource_id) != (need["resource_type"], need["resource_id"]):
                    continue
                if not overlaps(need["start_hour"], need["end_hour"], hold.start_hour, hold.end_hour):
                    continue
                conflicts.append(self._conflict(hold, params))

        # 2) 缺资源：航道/泊位/拖轮未登记
        channel = ledger.resource("channel", params["channel_id"])
        if channel is None:
            reasons.append({"code": "channel_missing", "message": "航道区段未登记:%s" % params["channel_id"]})
        berth = ledger.resource("berth", params["berth_id"])
        if berth is None:
            reasons.append({"code": "berth_missing", "message": "泊位未登记:%s" % params["berth_id"]})

        # 3) 临时封航范围命中
        if channel is not None:
            for notice in ledger.notices:
                if params["channel_id"] in notice["segments"] and overlaps(
                    params["start_hour"], params["end_hour"], notice["start_hour"], notice["end_hour"]
                ):
                    reasons.append({
                        "code": "closure_active",
                        "message": "航道区段%s处于临时封航（%s）" % (params["channel_id"], notice["notice_id"]),
                        "notice_id": notice["notice_id"],
                    })

        # 4) 泊位净空：长度 + 船底富余水深
        if berth is not None:
            if float(berth.get("length_m", 0)) < params["length_m"]:
                reasons.append({"code": "berth_length", "message": "泊位长度不足，无法靠泊净空"})
            ukc = ledger.required_ukc(params)
            margin = float(berth["depth_m"]) - params["draft_m"]
            if margin < ukc:
                reasons.append({
                    "code": "ukc_insufficient",
                    "message": "船底富余水深不足:实际%.2f/要求%.2f" % (margin, ukc),
                    "actual_ukc_m": round(margin, 2),
                    "required_ukc_m": ukc,
                })

        # 5) 护航拖轮：数量是否足够
        tugs_needed = ledger.required_tugs(params)
        free_tugs = self._free_tugs(ledger, params, active)
        if len(free_tugs) < tugs_needed:
            reasons.append({
                "code": "escort_shortage",
                "message": "护航拖轮不足:需%s艘/可用%s艘" % (tugs_needed, len(free_tugs)),
                "required_tugs": tugs_needed,
                "available_tugs": len(free_tugs),
            })

        if conflicts:
            return "conflict", conflicts
        if reasons:
            return "blocked", reasons
        holds = self._build_holds(params, free_tugs[:tugs_needed])
        basis = {
            "dg_class": params["dg_class"],
            "required_ukc_m": ledger.required_ukc(params),
            "required_tugs": tugs_needed,
            "tug_ids": [h["resource_id"] for h in holds if h["resource_type"] == "tug"],
            "closure_notice_ids": [],
        }
        return "granted", {"holds": holds, "basis": basis}

    # ---- 靠泊时复验富余水深 ----
    def check_berth_ukc(self, ledger: Ledger, params: Dict[str, Any], actual_draft_m: float) -> None:
        berth = ledger.resource("berth", params["berth_id"])
        if berth is None:
            raise ValidationError("泊位未登记:%s" % params["berth_id"])
        required = ledger.required_ukc(params)
        if float(berth["depth_m"]) - actual_draft_m < required:
            raise ValidationError("实际吃水导致富余水深不足，不允许靠泊")

    @staticmethod
    def _required_holds(params: Dict[str, Any], tugs_needed: int) -> List[Dict[str, Any]]:
        holds = [
            {"resource_type": "channel", "resource_id": params["channel_id"]},
            {"resource_type": "berth", "resource_id": params["berth_id"]},
        ]
        for h in holds:
            h.update({"start_hour": params["start_hour"], "end_hour": params["end_hour"]})
        # 拖轮仅占进港航段（开始后的一个小时窗）
        for _ in range(tugs_needed):
            holds.append({
                "resource_type": "tug",
                "resource_id": "",
                "start_hour": params["start_hour"],
                "end_hour": min(params["start_hour"] + 1, params["end_hour"]),
            })
        return holds

    @staticmethod
    def _free_tugs(ledger: Ledger, params: Dict[str, Any], active: List[Hold]) -> List[str]:
        busy = set()
        for hold in active:
            if hold.resource_type != "tug":
                continue
            if overlaps(params["start_hour"], min(params["start_hour"] + 1, params["end_hour"]),
                        hold.start_hour, hold.end_hour):
                busy.add(hold.resource_id)
        return [tug for tug in ledger.tugs() if tug not in busy]

    @staticmethod
    def _build_holds(params: Dict[str, Any], tug_ids: List[str]) -> List[Dict[str, Any]]:
        holds = [
            {
                "resource_type": "channel",
                "resource_id": params["channel_id"],
                "start_hour": params["start_hour"],
                "end_hour": params["end_hour"],
            },
            {
                "resource_type": "berth",
                "resource_id": params["berth_id"],
                "start_hour": params["start_hour"],
                "end_hour": params["end_hour"],
            },
        ]
        for tug in tug_ids:
            holds.append({
                "resource_type": "tug",
                "resource_id": tug,
                "start_hour": params["start_hour"],
                "end_hour": min(params["start_hour"] + 1, params["end_hour"]),
            })
        return holds

    @staticmethod
    def _conflict(hold: Hold, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "resource_type": hold.resource_type,
            "resource_id": hold.resource_id,
            "window": {"start_hour": hold.start_hour, "end_hour": hold.end_hour},
            "held_by": {"plan_ref": hold.plan_ref, "revision": hold.revision},
        }
