"""危险品船进港台账：用例编排、权限检查与作废重排。"""
from typing import Any, Dict, List, Optional

from . import dg_domain, dg_rules
from .dg_repository import DgRepository
from .domain import Actor, Conflict, PermissionDenied, text


class DgService:
    def __init__(self, repository: DgRepository) -> None:
        self.repository = repository

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require_read(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        if actor.role != "admin" and actor.role not in dg_domain.READ_ROLES:
            raise PermissionDenied("角色无权访问该服务")
        return actor

    def _require_write(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        if actor.role != "admin" and actor.role not in dg_domain.WRITE_ROLES:
            raise PermissionDenied("角色无权执行该操作")
        return actor

    # ---- 资源登记 ----

    def register_resource(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_write(actor)
        resource = dg_domain.validate_resource(data or {})
        return self.repository.register_resource(resource["kind"], resource["code"], resource["attrs"], actor.user_id)

    def list_resources(self, actor: Actor, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.list_resources(kind=kind)

    # ---- 计划 ----

    def create_plan(self, actor: Actor, reference: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_write(actor)
        reference = text({"reference": reference}, "reference")
        payload = dg_domain.validate_plan(data or {})
        return self.repository.create_plan(reference, payload, actor.user_id)

    def submit(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        """放行前一起预占：全部满足→reserved；缺项→pending并留下待补原因；占用冲突→保留草稿并记录冲突。"""
        actor = self._require_write(actor)
        outcome = self.repository.attempt_preoccupy(int(plan_id), dg_rules.evaluate_availability, actor.user_id)
        if outcome["result"] == "conflict":
            conflicts = self.repository.record_conflict(outcome["plan"], outcome["overlaps"], actor.user_id)
            return {"plan": self.repository.get_plan(plan_id), "outcome": "conflict", "conflicts": conflicts}
        if outcome["result"] == "pending":
            return {"plan": outcome["plan"], "outcome": "pending", "reasons": outcome["reasons"]}
        return {"plan": outcome["plan"], "outcome": "reserved"}

    def release(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        actor = self._require_write(actor)
        return self.repository.release_plan(int(plan_id), actor.user_id)

    def berth(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        actor = self._require_write(actor)
        return self.repository.berth_plan(int(plan_id), actor.user_id)

    def depart(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        actor = self._require_write(actor)
        return self.repository.depart_plan(int(plan_id), actor.user_id)

    def modify_plan(self, actor: Actor, plan_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        """船底富余水深、危险品类别等修改：未靠泊立即作废重排，已靠泊沿用原依据。"""
        actor = self._require_write(actor)
        changes = dg_domain.validate_plan_changes(changes or {})
        plan = self.repository.get_plan(int(plan_id))
        state = plan["state"]
        if state == "departed":
            raise Conflict("计划已离泊，不能修改")
        if state == "berthed":
            self.repository.record_basis_retained(plan["id"], {"trigger": "plan_modify", "requested_changes": changes}, actor.user_id)
            return {"plan": self.repository.get_plan(plan_id), "outcome": "basis_retained"}
        merged = dg_domain.validate_plan({**plan["payload"], **changes})
        if state == "draft":
            return {"plan": self.repository.update_plan_payload(plan["id"], merged, actor.user_id), "outcome": "modified"}
        outcome = self.repository.void_and_reevaluate(plan["id"], dg_rules.evaluate_availability, "计划参数修改", actor.user_id, new_payload=merged)
        return {"plan": outcome["plan"], "outcome": outcome["result"], "reasons": outcome.get("reasons", [])}

    # ---- 封航通知 ----

    def issue_closure(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_write(actor)
        closure_data = dg_domain.validate_closure(data or {})
        closure = self.repository.create_closure(closure_data, actor.user_id)
        affected = self._replan_for_closure(closure, actor, "封航通知%s生效" % closure["notice_no"])
        return {"closure": closure, "affected": affected}

    def modify_closure(self, actor: Actor, closure_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_write(actor)
        changes = dg_domain.validate_closure_changes(changes or {})
        old = self.repository.get_closure(int(closure_id))
        merged = dg_domain.validate_closure({key: changes.get(key, old[key]) for key in ("notice_no", "segments", "start_hour", "end_hour", "reason")})
        closure = self.repository.update_closure(int(closure_id), merged, actor.user_id)
        affected = self._replan_for_closure(closure, actor, "封航通知%s范围修改" % closure["notice_no"], extra_segments=old["segments"], extra_window=(old["start_hour"], old["end_hour"]))
        return {"closure": closure, "affected": affected}

    def lift_closure(self, actor: Actor, closure_id: int) -> Dict[str, Any]:
        actor = self._require_write(actor)
        closure = self.repository.lift_closure(int(closure_id), actor.user_id)
        affected = []
        for plan in self.repository.list_plans(state="pending", limit=500):
            outcome = self.repository.void_and_reevaluate(plan["id"], dg_rules.evaluate_availability, "封航通知%s解除" % closure["notice_no"], actor.user_id)
            affected.append(self._affected_entry(plan, outcome))
        return {"closure": closure, "affected": affected}

    def list_closures(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.list_closures(status=status)

    def _replan_for_closure(self, closure: Dict[str, Any], actor: Actor, reason: str, extra_segments: Optional[List[str]] = None, extra_window: Optional[Any] = None) -> List[Dict[str, Any]]:
        """封航范围变化后：未靠泊安排立即作废重排，已靠泊沿用原依据。"""
        results = []
        segments = set(closure["segments"]) | set(extra_segments or [])
        for plan in self.repository.list_plans(limit=500):
            payload = plan["payload"]
            hit = dg_rules.closure_hits(payload, segments, closure["start_hour"], closure["end_hour"])
            if not hit and extra_window:
                hit = dg_rules.closure_hits(payload, segments, extra_window[0], extra_window[1])
            if not hit:
                continue
            if plan["state"] in ("reserved", "released", "pending"):
                outcome = self.repository.void_and_reevaluate(plan["id"], dg_rules.evaluate_availability, reason, actor.user_id)
                results.append(self._affected_entry(plan, outcome))
            elif plan["state"] == "berthed":
                self.repository.record_basis_retained(plan["id"], {"trigger": "closure", "notice_no": closure["notice_no"]}, actor.user_id)
                results.append({"plan_id": plan["id"], "reference": plan["reference"], "outcome": "basis_retained"})
        return results

    @staticmethod
    def _affected_entry(plan: Dict[str, Any], outcome: Dict[str, Any]) -> Dict[str, Any]:
        return {"plan_id": plan["id"], "reference": plan["reference"], "outcome": outcome["result"], "reasons": outcome.get("reasons", [])}

    # ---- 恢复 ----

    def recover(self, actor: Actor) -> Dict[str, Any]:
        actor = self._require_write(actor)
        return self.repository.recover_occupancy(actor.user_id)

    # ---- 只读视图 ----

    def list_plans(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.list_plans(state=state, limit=limit)

    def get_plan(self, actor: Actor, plan_id: int) -> Dict[str, Any]:
        self._require_read(actor)
        return self.repository.get_plan(int(plan_id))

    def occupancy(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.occupancy()

    def list_conflicts(self, actor: Actor) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.list_conflicts()

    def ledger(self, actor: Actor, limit: int = 500) -> List[Dict[str, Any]]:
        self._require_read(actor)
        return self.repository.ledger(limit=limit)
