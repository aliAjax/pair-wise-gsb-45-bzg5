"""危险品船进港台账：领域常量、输入校验与派生规则。"""
from typing import Any, Dict, List

from .domain import ValidationError, choice, integer, number, text, text_list


DG_CLASSES = [str(i) for i in range(1, 10)]
RESOURCE_KINDS = ["channel_segment", "berth", "tug"]
RESOURCE_KIND_LABELS = {"channel_segment": "航道区段", "berth": "泊位", "tug": "护航拖轮"}
HIGH_ESCORT_CLASSES = {"1", "2", "7"}
PLAN_STATES = ["draft", "pending", "reserved", "released", "berthed", "departed"]
READ_ROLES = {"dispatcher", "port_controller"}
WRITE_ROLES = {"dispatcher"}
PLAN_CHANGE_KEYS = {"vessel", "vessel_length_m", "dangerous_class", "draft_m", "ukc_required_m", "eta_hour", "etd_hour", "segments", "berth"}
CLOSURE_CHANGE_KEYS = {"segments", "start_hour", "end_hour", "reason"}


def tugs_required_for(dangerous_class: str) -> int:
    """高危类别（爆炸品/气体/放射性）需两艘护航拖轮，其余一艘。"""
    return 2 if dangerous_class in HIGH_ESCORT_CLASSES else 1


def overlap_reasons(overlaps: List[Dict[str, Any]]) -> List[str]:
    """系统重排时把占用冲突改写为待补原因。"""
    return ["%s%s被计划%s占用" % (RESOURCE_KIND_LABELS[item["resource_kind"]], item["resource_code"], item["holder_reference"]) for item in overlaps]


def validate_plan(data: Dict[str, Any]) -> Dict[str, Any]:
    p = {
        "vessel": text(data, "vessel"),
        "vessel_length_m": number(data, "vessel_length_m", 1),
        "dangerous_class": choice(data, "dangerous_class", DG_CLASSES),
        "draft_m": number(data, "draft_m", 0.1),
        "ukc_required_m": number(data, "ukc_required_m", 0),
        "eta_hour": integer(data, "eta_hour", 0, 23),
        "etd_hour": integer(data, "etd_hour", 1, 24),
        "segments": text_list(data, "segments", 1),
        "berth": text(data, "berth"),
    }
    if p["etd_hour"] <= p["eta_hour"]:
        raise ValidationError("etd_hour必须晚于eta_hour")
    p["tugs_required"] = tugs_required_for(p["dangerous_class"])
    return p


def validate_plan_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("修改内容不能为空")
    unknown = sorted(set(changes) - PLAN_CHANGE_KEYS)
    if unknown:
        raise ValidationError("不支持的修改项：%s" % ",".join(unknown))
    return dict(changes)


def validate_closure_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("修改内容不能为空")
    unknown = sorted(set(changes) - CLOSURE_CHANGE_KEYS)
    if unknown:
        raise ValidationError("不支持的修改项：%s" % ",".join(unknown))
    return dict(changes)


def validate_resource(data: Dict[str, Any]) -> Dict[str, Any]:
    kind = choice(data, "kind", RESOURCE_KINDS)
    code = text(data, "code")
    attrs = data.get("attrs", {})
    if not isinstance(attrs, dict):
        raise ValidationError("attrs必须是对象")
    cleaned: Dict[str, Any] = {}
    if kind == "channel_segment":
        cleaned["depth_m"] = number(attrs, "depth_m", 0)
    elif kind == "berth":
        cleaned["length_m"] = number(attrs, "length_m", 1)
        cleaned["depth_m"] = number(attrs, "depth_m", 0)
        classes = attrs.get("allowed_dg_classes", [])
        if not isinstance(classes, list) or any(item not in DG_CLASSES for item in classes):
            raise ValidationError("allowed_dg_classes必须是危险品类别列表")
        cleaned["allowed_dg_classes"] = list(classes)
    return {"kind": kind, "code": code, "attrs": cleaned}


def validate_closure(data: Dict[str, Any]) -> Dict[str, Any]:
    c = {
        "notice_no": text(data, "notice_no"),
        "segments": text_list(data, "segments", 1),
        "start_hour": integer(data, "start_hour", 0, 23),
        "end_hour": integer(data, "end_hour", 1, 24),
        "reason": text(data, "reason"),
    }
    if c["end_hour"] <= c["start_hour"]:
        raise ValidationError("end_hour必须晚于start_hour")
    return c
