"""用例编排：权限检查、放行预占、作废重排与查询视图。

所有写操作都走 repository.command —— 单事务内回放台账、判定、追加事件，
因此两位调度员同时争用同一航道区段/泊位时，提交顺序决定唯一生效版本。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError
from .ledger import (
    BERTED, BLOCKED, DRAFT, RELEASED, UNBERTHED_STATES, Ledger,
)
from .repository import Emitter, Repository
from .rules import (
    PROTECTED_FIELDS, DomainRules, validate_notice, validate_params, validate_resource,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require_write(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role) or not self.rules.role_can_write(actor.role):
            raise PermissionDenied("角色无权执行调度写操作")

    # ---- 视图 ----
    @staticmethod
    def _view(rev: Any) -> Dict[str, Any]:
        return {
            "plan_ref": rev.plan_ref,
            "revision": rev.revision,
            "state": rev.state,
            "params": rev.params,
            "reasons": rev.reasons,
            "conflicts": rev.conflicts,
            "holds": [h.as_dict() for h in rev.granted_holds],
            "basis": rev.basis,
            "created_by": rev.actor_id,
        }

    def get_plan(self, actor: Actor, ref: str) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.get_plan(ref)

    def list_plans(self, actor: Actor, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_plans(state=state, limit=limit)

    def list_holds(self, actor: Actor, resource_type: str = None, resource_id: str = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_holds(resource_type, resource_id)

    def list_resources(self, actor: Actor, kind: str = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_resources(kind)

    def list_notices(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        return self.repository.list_notices()

    def safety_basis(self, actor: Actor) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.safety_basis()

    def timeline(self, actor: Actor, ref: str) -> Dict[str, Any]:
        self._actor(actor)
        return self.audit.timeline(ref)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        self._actor(actor)
        return self.repository.stats()

    def rebuild(self, actor: Actor) -> Dict[str, Any]:
        self._require_write(self._actor(actor))
        count = self.repository.rebuild()
        return {"replayed_events": count, "message": "台账投影已从完整事件重建"}

    # ---- 资源 / 封航 / 安全基准 ----
    def register_resource(self, actor: Actor, payload: Dict[str, Any], request_id: str = None) -> Dict[str, Any]:
        self._require_write(self._actor(actor))
        attrs = validate_resource(payload)
        kind, rid = attrs.pop("kind"), attrs.pop("id")

        def build(ledger: Ledger, emitter: Emitter) -> None:
            if ledger.resource(kind, rid) is not None:
                raise Conflict("资源已登记:%s/%s" % (kind, rid))
            body = dict(attrs)
            body.update({"kind": kind, "id": rid})
            emitter.emit(ledger, "resource_registered", "", body)

        ledger, _, _ = self.repository.command(actor.user_id, request_id, build)
        view = {"kind": kind, "id": rid}
        view.update(ledger.resource(kind, rid) or {})
        return view

    def publish_closure(self, actor: Actor, payload: Dict[str, Any], request_id: str = None) -> Dict[str, Any]:
        """临时封航通知一到：通知入台账，受影响的未靠泊安排立即作废并重排。"""
        self._require_write(self._actor(actor))
        notice = validate_notice(payload)

        def build(ledger: Ledger, emitter: Emitter) -> None:
            if any(n["notice_id"] == notice["notice_id"] for n in ledger.notices):
                raise Conflict("封航通知编号已存在:%s" % notice["notice_id"])
            emitter.emit(ledger, "closure_published", "", dict(notice))
            for rev in list(ledger.current_revisions()):
                if rev.state != RELEASED:
                    continue
                if rev.params["channel_id"] not in notice["segments"]:
                    continue
                if not (rev.params["start_hour"] < notice["end_hour"]
                        and notice["start_hour"] < rev.params["end_hour"]):
                    continue
                self._void_and_reissue(
                    ledger, emitter, rev,
                    "临时封航通知%s命中航道区段%s，安排作废重排" % (notice["notice_id"], notice["segments"]),
                    actor.user_id,
                )

        ledger, events, idempotent = self.repository.command(actor.user_id, request_id, build)
        return {
            "notice": next((n for n in ledger.notices if n["notice_id"] == notice["notice_id"]), notice),
            "affected_plan_refs": sorted({e["ref"] for e in events if e["type"] == "arrangement_voided"}),
            "idempotent_replay": idempotent,
        }

    def change_safety_basis(self, actor: Actor, payload: Dict[str, Any], request_id: str = None) -> Dict[str, Any]:
        """富余水深/护航基准修改后，不再满足依据的未靠泊安排立即作废重排；已靠泊沿用原依据。"""
        self._require_write(self._actor(actor))
        payload = payload or {}
        ukc = self._basis_map(payload.get("ukc_by_class"), float)
        escort = self._basis_map(payload.get("escort_by_class"), int)
        if not ukc and not escort:
            raise ValidationError("至少提供ukc_by_class或escort_by_class")

        def build(ledger: Ledger, emitter: Emitter) -> None:
            emitter.emit(
                ledger, "safety_basis_changed", "",
                {"ukc_by_class": ukc, "escort_by_class": escort},
            )
            for rev in list(ledger.current_revisions()):
                if rev.state != RELEASED:
                    continue
                verdict, detail = self.rules.evaluate_release(ledger, rev.params)
                if verdict != "granted":
                    reason = ";".join(r["message"] for r in detail) if verdict == "blocked" else "资源被占用"
                    self._void_and_reissue(
                        ledger, emitter, rev, "安全基准变更后不再满足: %s" % reason, actor.user_id
                    )

        ledger, events, idempotent = self.repository.command(actor.user_id, request_id, build)
        return {
            "basis": {"ukc_by_class": ledger.ukc_by_class, "escort_by_class": ledger.escort_by_class},
            "affected_plan_refs": sorted({e["ref"] for e in events if e["type"] == "arrangement_voided"}),
            "idempotent_replay": idempotent,
        }

    # ---- 计划 ----
    def register_plan(self, actor: Actor, plan_ref: str, payload: Dict[str, Any], request_id: str = None) -> Dict[str, Any]:
        self._require_write(self._actor(actor))
        params = validate_params(payload)
        plan_ref = plan_ref.strip()
        if not plan_ref:
            raise ValidationError("plan_ref不能为空")

        def build(ledger: Ledger, emitter: Emitter) -> None:
            if plan_ref in ledger.latest:
                raise Conflict("计划编号已存在:%s" % plan_ref)
            emitter.emit(ledger, "plan_registered", plan_ref, {"params": params})

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    def release_plan(self, actor: Actor, plan_ref: str, request_id: str = None) -> Dict[str, Any]:
        """放行前一起预占航道区段、泊位净空和护航拖轮；缺哪项停住并留待补原因。"""
        self._require_write(self._actor(actor))

        def build(ledger: Ledger, emitter: Emitter) -> None:
            rev = ledger.current(plan_ref)
            if rev.state not in (DRAFT, BLOCKED):
                raise Conflict("仅草稿/待补计划可以申请放行，当前状态:%s" % rev.state)
            verdict, detail = self.rules.evaluate_release(ledger, rev.params)
            if verdict == "granted":
                emitter.emit(ledger, "holds_granted", plan_ref, {
                    "revision": rev.revision, "holds": detail["holds"], "basis": detail["basis"],
                })
            elif verdict == "blocked":
                emitter.emit(ledger, "release_blocked", plan_ref, {
                    "revision": rev.revision, "reasons": detail,
                })
            else:
                # 两位调度员争用同一航道区段/泊位：落后者保留为草稿并记录冲突
                emitter.emit(ledger, "conflict_draft_kept", plan_ref, {
                    "revision": rev.revision, "conflicts": detail,
                })

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    def revise_plan(self, actor: Actor, plan_ref: str, payload: Dict[str, Any], request_id: str = None) -> Dict[str, Any]:
        """修改进港参数：未靠泊安排立即作废重排；已靠泊沿用原依据，拒绝关键字段修改。"""
        self._require_write(self._actor(actor))
        updates = validate_params(payload)

        def build(ledger: Ledger, emitter: Emitter) -> None:
            rev = ledger.current(plan_ref)
            if rev.state == BERTED:
                changed = [key for key in PROTECTED_FIELDS if rev.params.get(key) != updates.get(key)]
                if changed:
                    raise Conflict("已靠泊计划沿用原依据，禁止修改关键字段:%s" % ",".join(changed))
                raise Conflict("已靠泊计划无需重排")
            if rev.state not in UNBERTHED_STATES:
                raise Conflict("当前状态不允许修改:%s" % rev.state)
            # 未靠泊：当前修订立即作废，出新修订并按新依据重新判定
            self._void_and_reissue(
                ledger, emitter, rev, "调度员修改进港参数，未靠泊安排作废重排",
                actor.user_id, override_params=updates,
            )

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    def berth_plan(self, actor: Actor, plan_ref: str, actual_draft_m: float = None,
                   request_id: str = None) -> Dict[str, Any]:
        self._require_write(self._actor(actor))

        def build(ledger: Ledger, emitter: Emitter) -> None:
            rev = ledger.current(plan_ref)
            if rev.state != RELEASED:
                raise Conflict("仅已放行计划可以靠泊，当前状态:%s" % rev.state)
            actual = float(actual_draft_m) if actual_draft_m is not None else float(rev.params["draft_m"])
            self.rules.check_berth_ukc(ledger, rev.params, actual)
            emitter.emit(ledger, "vessel_berthed", plan_ref, {
                "revision": rev.revision, "actual_draft_m": actual,
            })

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    def depart_plan(self, actor: Actor, plan_ref: str, request_id: str = None) -> Dict[str, Any]:
        self._require_write(self._actor(actor))

        def build(ledger: Ledger, emitter: Emitter) -> None:
            rev = ledger.current(plan_ref)
            if rev.state != BERTED:
                raise Conflict("仅已靠泊计划可以离泊，当前状态:%s" % rev.state)
            emitter.emit(ledger, "vessel_departed", plan_ref, {"revision": rev.revision})

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    def cancel_plan(self, actor: Actor, plan_ref: str, request_id: str = None) -> Dict[str, Any]:
        self._require_write(self._actor(actor))

        def build(ledger: Ledger, emitter: Emitter) -> None:
            rev = ledger.current(plan_ref)
            emitter.emit(ledger, "plan_cancelled", plan_ref, {"revision": rev.revision})

        ledger, _, idempotent = self.repository.command(actor.user_id, request_id, build)
        view = self._view(ledger.current(plan_ref))
        view["idempotent_replay"] = idempotent
        return view

    # ---- 内部编排 ----
    def _void_and_reissue(self, ledger: Ledger, emitter: Emitter, rev: Any,
                          reason: str, actor_id: str,
                          override_params: Dict[str, Any] = None) -> None:
        """作废当前修订 -> 出新修订（草稿）-> 按新依据重新判定一次（自动重排）。"""
        emitter.emit(ledger, "arrangement_voided", rev.plan_ref, {
            "revision": rev.revision, "reason": reason,
        })
        new_rev = self._emit_revision(ledger, emitter, rev, actor_id, override_params)
        verdict, detail = self.rules.evaluate_release(ledger, new_rev.params)
        if verdict == "granted":
            emitter.emit(ledger, "holds_granted", rev.plan_ref, {
                "revision": new_rev.revision, "holds": detail["holds"], "basis": detail["basis"],
            })
        elif verdict == "blocked":
            emitter.emit(ledger, "release_blocked", rev.plan_ref, {
                "revision": new_rev.revision, "reasons": detail,
            })
        else:
            emitter.emit(ledger, "conflict_draft_kept", rev.plan_ref, {
                "revision": new_rev.revision, "conflicts": detail,
            })

    @staticmethod
    def _emit_revision(ledger: Ledger, emitter: Emitter, rev: Any,
                       actor_id: str, override_params: Dict[str, Any] = None) -> Any:
        number = rev.revision + 1
        params = dict(override_params) if override_params is not None else dict(rev.params)
        emitter.emit(ledger, "plan_revised", rev.plan_ref, {
            "revision": number, "params": params, "actor_id": actor_id,
        })
        return ledger.current(rev.plan_ref)

    @staticmethod
    def _basis_map(value: Any, cast: type) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict) or not value:
            raise ValidationError("安全基准必须是非空对象")
        try:
            return {str(key): cast(val) for key, val in value.items()}
        except (TypeError, ValueError) as exc:
            raise ValidationError("安全基准数值无效") from exc
