"""危险品船进港台账：SQLite 表结构、事务与台账重放恢复。

dg_ledger 是完整台账（每次变更与状态同事务写入），dg_reservations 是占位视图；
写盘失败或占位数据丢失后，可用 recover_occupancy 从台账重放重建，
重放按 (event_id, resource_code) 唯一约束去重，不会重复占位。
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .dg_domain import RESOURCE_KIND_LABELS, overlap_reasons
from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DgRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dg_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dg_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    attrs TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dg_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES dg_plans(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    resource_kind TEXT NOT NULL,
                    resource_code TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, resource_code)
                );
                CREATE TABLE IF NOT EXISTS dg_closures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_no TEXT NOT NULL UNIQUE,
                    segments TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dg_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES dg_plans(id) ON DELETE CASCADE,
                    resource_kind TEXT NOT NULL,
                    resource_code TEXT NOT NULL,
                    holder_plan_id INTEGER,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dg_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    plan_id INTEGER,
                    payload TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dg_reservations_code ON dg_reservations(resource_code, status);
                CREATE INDEX IF NOT EXISTS idx_dg_reservations_plan ON dg_reservations(plan_id, status);
                CREATE INDEX IF NOT EXISTS idx_dg_plans_state ON dg_plans(state);
                """
            )

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _resource_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["attrs"] = json.loads(item["attrs"])
        return item

    @staticmethod
    def _closure_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["segments"] = json.loads(item["segments"])
        return item

    def _fetch_plan(self, connection: sqlite3.Connection, plan_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM dg_plans WHERE id=?", (plan_id,)).fetchone()
        return self._plan_row(row) if row is not None else None

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_plan(connection, plan_id)
        if row is None:
            raise NotFound("计划不存在")
        return row

    def list_plans(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM dg_plans WHERE state=? ORDER BY id LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM dg_plans ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [self._plan_row(row) for row in rows]

    def _append_ledger(self, connection: sqlite3.Connection, kind: str, plan_id: Optional[int], payload: Dict[str, Any], actor_id: str, event_id: Optional[str] = None) -> str:
        event_id = event_id or uuid.uuid4().hex
        connection.execute(
            "INSERT INTO dg_ledger(event_id,kind,plan_id,payload,actor_id,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, kind, plan_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, _now()),
        )
        return event_id

    def _set_plan_state(self, connection: sqlite3.Connection, current: Dict[str, Any], state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        connection.execute(
            "UPDATE dg_plans SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, int(current["version"]) + 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, _now(), current["id"]),
        )
        return self._fetch_plan(connection, current["id"])

    def _resource_map(self, connection: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM dg_resources").fetchall()
        return {row["code"]: {"kind": row["kind"], "attrs": json.loads(row["attrs"])} for row in rows}

    def _active_reservations(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT r.*, p.reference AS plan_reference FROM dg_reservations r JOIN dg_plans p ON p.id=r.plan_id WHERE r.status='active'"
        ).fetchall()
        return [dict(row) for row in rows]

    def _active_closures(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM dg_closures WHERE status='active'").fetchall()
        return [self._closure_row(row) for row in rows]

    def _insert_reservations(self, connection: sqlite3.Connection, plan_id: int, event_id: str, assignments: Dict[str, Any], start_hour: int, end_hour: int, now: str) -> List[Dict[str, Any]]:
        entries = [("channel_segment", code) for code in assignments["segments"]]
        if assignments.get("berth"):
            entries.append(("berth", assignments["berth"]))
        entries += [("tug", code) for code in assignments["tugs"]]
        rows = []
        for kind, code in entries:
            connection.execute(
                "INSERT INTO dg_reservations(plan_id,event_id,resource_kind,resource_code,start_hour,end_hour,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, event_id, kind, code, start_hour, end_hour, "active", now),
            )
            rows.append({"resource_kind": kind, "resource_code": code, "start_hour": start_hour, "end_hour": end_hour})
        return rows

    # ---- 资源登记 ----

    def register_resource(self, kind: str, code: str, attrs: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO dg_resources(kind,code,attrs,created_at) VALUES(?,?,?,?)",
                    (kind, code, json.dumps(attrs, ensure_ascii=False, sort_keys=True), _now()),
                )
                self._append_ledger(connection, "resource_registered", None, {"kind": kind, "code": code, "attrs": attrs}, actor_id)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源编码已存在") from exc
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM dg_resources WHERE code=?", (code,)).fetchone()
        return self._resource_row(row)

    def list_resources(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                rows = connection.execute("SELECT * FROM dg_resources WHERE kind=? ORDER BY code", (kind,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM dg_resources ORDER BY code").fetchall()
        return [self._resource_row(row) for row in rows]

    # ---- 计划 ----

    def create_plan(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO dg_plans(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, "draft", 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                plan_id = int(cursor.lastrowid)
                self._append_ledger(connection, "plan_created", plan_id, {"reference": reference, "payload": payload}, actor_id)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self.get_plan(plan_id)

    def update_plan_payload(self, plan_id: int, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] != "draft":
                connection.rollback()
                raise Conflict("仅草稿状态可直接修改")
            updated = self._set_plan_state(connection, current, "draft", payload, actor_id)
            self._append_ledger(connection, "plan_modified", plan_id, {"payload": payload}, actor_id)
            connection.commit()
            return updated

    def attempt_preoccupy(self, plan_id: int, evaluator: Callable[..., Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        """放行前一起预占：检查与落库在同一事务，要么全部占位，要么一项不占。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] not in ("draft", "pending"):
                connection.rollback()
                raise Conflict("当前状态不允许预占")
            payload = current["payload"]
            outcome = evaluator(payload, self._resource_map(connection), self._active_reservations(connection), self._active_closures(connection))
            if outcome["overlaps"]:
                connection.rollback()
                return {"result": "conflict", "plan": current, "overlaps": outcome["overlaps"]}
            if outcome["missing"]:
                new_payload = dict(payload)
                new_payload.pop("assignments", None)
                new_payload["pending_reasons"] = outcome["missing"]
                updated = self._set_plan_state(connection, current, "pending", new_payload, actor_id)
                self._append_ledger(connection, "pending", plan_id, {"reasons": outcome["missing"]}, actor_id)
                connection.commit()
                return {"result": "pending", "reasons": outcome["missing"], "plan": updated}
            now = _now()
            event_id = uuid.uuid4().hex
            ledger_reservations = self._insert_reservations(connection, plan_id, event_id, outcome["assignments"], int(payload["eta_hour"]), int(payload["etd_hour"]), now)
            new_payload = dict(payload)
            new_payload.pop("pending_reasons", None)
            new_payload["assignments"] = outcome["assignments"]
            updated = self._set_plan_state(connection, current, "reserved", new_payload, actor_id)
            self._append_ledger(connection, "preoccupied", plan_id, {"reservations": ledger_reservations}, actor_id, event_id=event_id)
            connection.commit()
            return {"result": "reserved", "plan": updated}

    def record_conflict(self, plan: Dict[str, Any], overlaps: List[Dict[str, Any]], actor_id: str) -> List[Dict[str, Any]]:
        """落后者留下草稿和冲突：计划状态不变，冲突逐条落库并记台账。"""
        now = _now()
        rows = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in overlaps:
                detail = "%s%s已被计划%s占用" % (RESOURCE_KIND_LABELS[item["resource_kind"]], item["resource_code"], item["holder_reference"])
                cursor = connection.execute(
                    "INSERT INTO dg_conflicts(plan_id,resource_kind,resource_code,holder_plan_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                    (plan["id"], item["resource_kind"], item["resource_code"], item["holder_plan_id"], detail, now),
                )
                rows.append({"id": int(cursor.lastrowid), "plan_id": plan["id"], "resource_kind": item["resource_kind"], "resource_code": item["resource_code"], "holder_plan_id": item["holder_plan_id"], "detail": detail, "created_at": now})
            self._append_ledger(connection, "conflict_recorded", plan["id"], {"overlaps": overlaps}, actor_id)
            connection.commit()
        return rows

    def release_plan(self, plan_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] != "reserved":
                connection.rollback()
                if current["state"] in ("draft", "pending"):
                    raise Conflict("未完成一起预占，不能放行")
                raise Conflict("当前状态不允许放行")
            updated = self._set_plan_state(connection, current, "released", current["payload"], actor_id)
            self._append_ledger(connection, "released", plan_id, {"assignments": current["payload"].get("assignments", {})}, actor_id)
            connection.commit()
            return updated

    def berth_plan(self, plan_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] != "released":
                connection.rollback()
                raise Conflict("当前状态不允许靠泊")
            payload = dict(current["payload"])
            reservations = [item for item in self._active_reservations(connection) if item["plan_id"] == plan_id]
            basis = {
                "ukc_required_m": payload["ukc_required_m"],
                "dangerous_class": payload["dangerous_class"],
                "segments": list(payload["segments"]),
                "berth": payload["berth"],
                "reservations": [{"resource_kind": item["resource_kind"], "resource_code": item["resource_code"], "start_hour": item["start_hour"], "end_hour": item["end_hour"]} for item in reservations],
                "active_closures": [item["notice_no"] for item in self._active_closures(connection)],
                "berthed_by": actor_id,
            }
            payload["basis"] = basis
            updated = self._set_plan_state(connection, current, "berthed", payload, actor_id)
            self._append_ledger(connection, "berthed", plan_id, {"basis": basis}, actor_id)
            connection.commit()
            return updated

    def depart_plan(self, plan_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] != "berthed":
                connection.rollback()
                raise Conflict("当前状态不允许离泊")
            connection.execute("UPDATE dg_reservations SET status='released' WHERE plan_id=? AND status='active'", (plan_id,))
            updated = self._set_plan_state(connection, current, "departed", current["payload"], actor_id)
            self._append_ledger(connection, "departed", plan_id, {}, actor_id)
            connection.commit()
            return updated

    def void_and_reevaluate(self, plan_id: int, evaluator: Callable[..., Dict[str, Any]], reason: str, actor_id: str, new_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """未靠泊安排立即作废重排：同一事务内先释放原占位，再按新依据重评。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            if current["state"] not in ("reserved", "released", "pending"):
                connection.rollback()
                raise Conflict("当前状态不允许重排")
            now = _now()
            active = connection.execute("SELECT resource_code FROM dg_reservations WHERE plan_id=? AND status='active'", (plan_id,)).fetchall()
            if active:
                connection.execute("UPDATE dg_reservations SET status='released' WHERE plan_id=? AND status='active'", (plan_id,))
            self._append_ledger(connection, "voided", plan_id, {"reason": reason, "released": [row["resource_code"] for row in active]}, actor_id)
            payload = dict(new_payload) if new_payload is not None else dict(current["payload"])
            outcome = evaluator(payload, self._resource_map(connection), self._active_reservations(connection), self._active_closures(connection))
            reasons = list(outcome["missing"]) + overlap_reasons(outcome["overlaps"])
            if reasons:
                payload.pop("assignments", None)
                payload["pending_reasons"] = reasons
                updated = self._set_plan_state(connection, current, "pending", payload, actor_id)
                self._append_ledger(connection, "pending", plan_id, {"reasons": reasons}, actor_id)
                connection.commit()
                return {"result": "pending", "reasons": reasons, "plan": updated}
            event_id = uuid.uuid4().hex
            ledger_reservations = self._insert_reservations(connection, plan_id, event_id, outcome["assignments"], int(payload["eta_hour"]), int(payload["etd_hour"]), now)
            payload.pop("pending_reasons", None)
            payload["assignments"] = outcome["assignments"]
            updated = self._set_plan_state(connection, current, "reserved", payload, actor_id)
            self._append_ledger(connection, "preoccupied", plan_id, {"reservations": ledger_reservations}, actor_id, event_id=event_id)
            connection.commit()
            return {"result": "reserved", "plan": updated}

    def record_basis_retained(self, plan_id: int, info: Dict[str, Any], actor_id: str) -> None:
        """已靠泊沿用原依据：安排不动，只把变更请求与靠泊依据记入台账。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._fetch_plan(connection, plan_id)
            if current is None:
                connection.rollback()
                raise NotFound("计划不存在")
            self._append_ledger(connection, "basis_retained", plan_id, {"info": info, "basis": current["payload"].get("basis", {})}, actor_id)
            connection.commit()

    # ---- 封航通知 ----

    def create_closure(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO dg_closures(notice_no,segments,start_hour,end_hour,reason,status,version,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'active',1,?,?,?)",
                    (data["notice_no"], json.dumps(data["segments"], ensure_ascii=False), data["start_hour"], data["end_hour"], data["reason"], actor_id, now, now),
                )
                closure_id = int(cursor.lastrowid)
                self._append_ledger(connection, "closure_issued", None, {"closure_id": closure_id, **data}, actor_id)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("通知编号已存在") from exc
        return self.get_closure(closure_id)

    def get_closure(self, closure_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM dg_closures WHERE id=?", (closure_id,)).fetchone()
        if row is None:
            raise NotFound("封航通知不存在")
        return self._closure_row(row)

    def list_closures(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM dg_closures WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM dg_closures ORDER BY id").fetchall()
        return [self._closure_row(row) for row in rows]

    def update_closure(self, closure_id: int, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM dg_closures WHERE id=?", (closure_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("封航通知不存在")
            before = self._closure_row(row)
            if before["status"] != "active":
                connection.rollback()
                raise Conflict("通知已解除，不能修改")
            connection.execute(
                "UPDATE dg_closures SET segments=?,start_hour=?,end_hour=?,reason=?,version=?,updated_at=? WHERE id=?",
                (json.dumps(data["segments"], ensure_ascii=False), data["start_hour"], data["end_hour"], data["reason"], int(before["version"]) + 1, _now(), closure_id),
            )
            self._append_ledger(connection, "closure_modified", None, {"closure_id": closure_id, "before": before, "after": data}, actor_id)
            connection.commit()
        return self.get_closure(closure_id)

    def lift_closure(self, closure_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM dg_closures WHERE id=?", (closure_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("封航通知不存在")
            closure = self._closure_row(row)
            if closure["status"] != "active":
                connection.rollback()
                raise Conflict("通知已解除")
            connection.execute("UPDATE dg_closures SET status='lifted',version=?,updated_at=? WHERE id=?", (int(closure["version"]) + 1, _now(), closure_id))
            self._append_ledger(connection, "closure_lifted", None, {"closure_id": closure_id, "notice_no": closure["notice_no"]}, actor_id)
            connection.commit()
        return self.get_closure(closure_id)

    # ---- 台账视图与恢复 ----

    def occupancy(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.resource_kind, r.resource_code, r.start_hour, r.end_hour, r.plan_id, p.reference AS plan_reference, p.payload AS plan_payload "
                "FROM dg_reservations r JOIN dg_plans p ON p.id=r.plan_id WHERE r.status='active' ORDER BY r.resource_code, r.start_hour"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["vessel"] = json.loads(item.pop("plan_payload")).get("vessel", "")
            result.append(item)
        return result

    def list_conflicts(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM dg_conflicts ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def ledger(self, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM dg_ledger ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def recover_occupancy(self, actor_id: str) -> Dict[str, Any]:
        """写盘失败后从完整台账重放重建占位；按事件幂等，重放不重复占位。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM dg_reservations")
            events = connection.execute("SELECT * FROM dg_ledger ORDER BY id").fetchall()
            replayed = 0
            for event in events:
                kind = event["kind"]
                payload = json.loads(event["payload"])
                if kind == "preoccupied":
                    for item in payload["reservations"]:
                        connection.execute(
                            "INSERT OR IGNORE INTO dg_reservations(plan_id,event_id,resource_kind,resource_code,start_hour,end_hour,status,created_at) VALUES(?,?,?,?,?,?,'active',?)",
                            (event["plan_id"], event["event_id"], item["resource_kind"], item["resource_code"], item["start_hour"], item["end_hour"], event["created_at"]),
                        )
                    replayed += 1
                elif kind in ("voided", "departed"):
                    connection.execute("UPDATE dg_reservations SET status='released' WHERE plan_id=? AND status='active'", (event["plan_id"],))
            active = connection.execute("SELECT COUNT(*) AS total FROM dg_reservations WHERE status='active'").fetchone()["total"]
            self._append_ledger(connection, "recovered", None, {"events_replayed": replayed, "active_reservations": active}, actor_id)
            connection.commit()
        return {"events_replayed": replayed, "active_reservations": active}
