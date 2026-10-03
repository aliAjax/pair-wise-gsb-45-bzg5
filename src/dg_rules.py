"""危险品船进港台账：预占可行性评估与封航影响判断（纯函数）。"""
from typing import Any, Dict, List


def windows_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and a_end > b_start


def evaluate_availability(payload: Dict[str, Any], resources: Dict[str, Dict[str, Any]], reservations: List[Dict[str, Any]], closures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """放行前一起预占：航道区段、泊位、护航拖轮在同一时间窗内一起检查。

    返回 {"missing": [...], "overlaps": [...], "assignments": {...}}：
    - missing：能力与可用性缺口（未登记、封航、水深、长度、类别、拖轮数量），对应"待补原因"；
    - overlaps：与他计划有效预占的时间窗冲突，对应"先到先占"的冲突；
    - assignments：全部满足时各项资源的占用方案，缺任何一项都不会落盘。
    """
    missing: List[str] = []
    overlaps: List[Dict[str, Any]] = []
    assignments: Dict[str, Any] = {"segments": [], "berth": None, "tugs": []}
    start, end = int(payload["eta_hour"]), int(payload["etd_hour"])
    draft = float(payload["draft_m"])
    ukc = float(payload["ukc_required_m"])

    def occupied_by(code: str):
        for item in reservations:
            if item["resource_code"] == code and windows_overlap(start, end, int(item["start_hour"]), int(item["end_hour"])):
                return item
        return None

    for code in payload["segments"]:
        resource = resources.get(code)
        if resource is None or resource["kind"] != "channel_segment":
            missing.append("航道区段%s未登记" % code)
            continue
        closure = next((c for c in closures if code in c["segments"] and windows_overlap(start, end, int(c["start_hour"]), int(c["end_hour"]))), None)
        if closure is not None:
            missing.append("航道区段%s在封航范围内（通知%s）" % (code, closure["notice_no"]))
            continue
        margin = float(resource["attrs"].get("depth_m", 0)) - draft
        if margin < ukc:
            missing.append("航道区段%s富余水深不足：需要%.2f米，实际余%.2f米" % (code, ukc, margin))
            continue
        holder = occupied_by(code)
        if holder is not None:
            overlaps.append({"resource_kind": "channel_segment", "resource_code": code, "holder_plan_id": holder["plan_id"], "holder_reference": holder["plan_reference"]})
            continue
        assignments["segments"].append(code)

    berth_code = payload["berth"]
    berth = resources.get(berth_code)
    if berth is None or berth["kind"] != "berth":
        missing.append("泊位%s未登记" % berth_code)
    else:
        attrs = berth["attrs"]
        berth_ok = True
        if float(attrs.get("length_m", 0)) < float(payload["vessel_length_m"]):
            missing.append("泊位%s长度不足：需要%.0f米，实际%.0f米" % (berth_code, float(payload["vessel_length_m"]), float(attrs.get("length_m", 0))))
            berth_ok = False
        margin = float(attrs.get("depth_m", 0)) - draft
        if margin < ukc:
            missing.append("泊位%s富余水深不足：需要%.2f米，实际余%.2f米" % (berth_code, ukc, margin))
            berth_ok = False
        if payload["dangerous_class"] not in attrs.get("allowed_dg_classes", []):
            missing.append("泊位%s不接受危险品类别%s" % (berth_code, payload["dangerous_class"]))
            berth_ok = False
        if berth_ok:
            holder = occupied_by(berth_code)
            if holder is not None:
                overlaps.append({"resource_kind": "berth", "resource_code": berth_code, "holder_plan_id": holder["plan_id"], "holder_reference": holder["plan_reference"]})
            else:
                assignments["berth"] = berth_code

    needed = int(payload["tugs_required"])
    free_tugs = sorted(code for code, resource in resources.items() if resource["kind"] == "tug" and occupied_by(code) is None)
    if len(free_tugs) < needed:
        missing.append("护航拖轮不足：需要%d艘，可用%d艘" % (needed, len(free_tugs)))
    else:
        assignments["tugs"] = free_tugs[:needed]

    return {"missing": missing, "overlaps": overlaps, "assignments": assignments}


def closure_hits(payload: Dict[str, Any], segments: List[str], start_hour: int, end_hour: int) -> bool:
    """计划是否落在封航范围内（区段相交且时间窗重叠）。"""
    return bool(set(payload["segments"]) & set(segments)) and windows_overlap(int(payload["eta_hour"]), int(payload["etd_hour"]), int(start_hour), int(end_hour))
