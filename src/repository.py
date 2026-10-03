"""SQLite 台账存储：ledger_events 为唯一事实源。

- 每个命令在单连接、BEGIN IMMEDIATE 事务内：回放全部事件 -> 归约判定 -> 追加事件 -> 重建投影。
- 写盘失败（含模拟崩溃）时整事务回滚，事件不落地；带幂等键重试可安全重放。
- rebuild() 可从完整台账事件重建全部投影，重放天然去重（event_id 幂等）。
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import NotFound
from .ledger import Ledger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 测试钩子：提交前抛错，模拟写盘失败/进程崩溃
        self.crash_before_commit = False
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    ref TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotent_requests (
                    request_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT
                );
                -- 以下均为台账事件的投影，可随时整体重建
                CREATE TABLE IF NOT EXISTS plan_revisions (
                    plan_ref TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    params TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    conflicts TEXT NOT NULL,
                    holds TEXT NOT NULL,
                    basis TEXT,
                    latest INTEGER NOT NULL,
                    PRIMARY KEY (plan_ref, revision)
                );
                CREATE TABLE IF NOT EXISTS active_holds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    plan_ref TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    attrs TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
                CREATE TABLE IF NOT EXISTS notices (
                    notice_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS safety_basis (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    ukc_by_class TEXT NOT NULL,
                    escort_by_class TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_holds_resource ON active_holds(resource_type, resource_id);
                CREATE INDEX IF NOT EXISTS idx_holds_plan ON active_holds(plan_ref);
                CREATE INDEX IF NOT EXISTS idx_events_ref ON ledger_events(ref, seq);
                """
            )

    # ---- 基础读写 ----
    _ENVELOPE_KEYS = ("event_id", "type", "ref", "actor_id", "created_at")

    def _load_events(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM ledger_events ORDER BY seq").fetchall()
        events = []
        for row in rows:
            event = {
                "event_id": row["event_id"],
                "type": row["type"],
                "ref": row["ref"],
                "actor_id": row["actor_id"],
                "created_at": row["created_at"],
            }
            event.update(json.loads(row["payload"]))
            events.append(event)
        return events

    def load_ledger(self) -> Ledger:
        with self._connect() as connection:
            return Ledger.replay(self._load_events(connection))

    # ---- 投影物化 ----
    def _materialize(self, connection: sqlite3.Connection, ledger: Ledger) -> None:
        connection.execute("DELETE FROM plan_revisions")
        connection.execute("DELETE FROM active_holds")
        connection.execute("DELETE FROM resources")
        connection.execute("DELETE FROM notices")
        connection.execute("DELETE FROM safety_basis")

        latest_numbers: Dict[str, int] = {}
        for (kind, rid), attrs in ledger.resources.items():
            connection.execute(
                "INSERT INTO resources(kind,id,attrs) VALUES(?,?,?)",
                (kind, rid, json.dumps(attrs, ensure_ascii=False, sort_keys=True)),
            )
        for notice in ledger.notices:
            payload = dict(notice)
            notice_id = payload.pop("notice_id")
            connection.execute(
                "INSERT INTO notices(notice_id,payload) VALUES(?,?)",
                (notice_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        connection.execute(
            "INSERT OR REPLACE INTO safety_basis(id,ukc_by_class,escort_by_class) VALUES(1,?,?)",
            (
                json.dumps(ledger.ukc_by_class, ensure_ascii=False, sort_keys=True),
                json.dumps(ledger.escort_by_class, ensure_ascii=False, sort_keys=True),
            ),
        )
        for (ref, number), rev in ledger.revisions.items():
            if ledger.latest.get(ref) == number:
                latest_numbers[ref] = number
            connection.execute(
                "INSERT INTO plan_revisions(plan_ref,revision,state,params,actor_id,reasons,conflicts,holds,basis,latest)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ref,
                    number,
                    rev.state,
                    json.dumps(rev.params, ensure_ascii=False, sort_keys=True),
                    rev.actor_id,
                    json.dumps(rev.reasons, ensure_ascii=False, sort_keys=True),
                    json.dumps(rev.conflicts, ensure_ascii=False, sort_keys=True),
                    json.dumps([h.as_dict() for h in rev.granted_holds], ensure_ascii=False, sort_keys=True),
                    json.dumps(rev.basis, ensure_ascii=False, sort_keys=True) if rev.basis else None,
                    1 if ledger.latest.get(ref) == number else 0,
                ),
            )
        for rev in ledger.current_revisions():
            for hold in ledger.effective_holds(rev):
                connection.execute(
                    "INSERT INTO active_holds(resource_type,resource_id,start_hour,end_hour,plan_ref,revision)"
                    " VALUES(?,?,?,?,?,?)",
                    (hold.resource_type, hold.resource_id, hold.start_hour, hold.end_hour,
                     hold.plan_ref, hold.revision),
                )

    def rebuild(self) -> int:
        """从完整台账事件重建投影。重复执行结果一致、不产生重复占位。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ledger = Ledger.replay(self._load_events(connection))
            self._materialize(connection, ledger)
            connection.commit()
        return len(ledger.events)

    # ---- 命令提交 ----
    def command(
        self,
        actor_id: str,
        request_id: Optional[str],
        build: Callable[[Ledger, "Emitter"], None],
    ) -> Tuple[Ledger, List[Dict[str, Any]], bool]:
        """串行化提交一个命令。

        build(ledger, emitter) 内部用 emitter.emit(...) 追加事件；事件立即应用到
        归约器（作废/重修订后可继续判定），事务提交前全部可回滚。
        返回 (归约后台账, 本次追加事件, 是否幂等重放命中)。
        """
        request_id = request_id or ("auto-" + uuid.uuid4().hex)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM idempotent_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["status"] != "committed":
                    connection.execute(
                        "UPDATE idempotent_requests SET status='committed' WHERE request_id=?",
                        (request_id,),
                    )
                ledger = Ledger.replay(self._load_events(connection))
                result = json.loads(row["result"]) if row["result"] else None
                connection.commit()
                return ledger, self._result_events(ledger, result), True
            ledger = Ledger.replay(self._load_events(connection))
            emitter = Emitter(actor_id)
            build(ledger, emitter)

            now = _now()
            for event in emitter.events:
                body = {k: v for k, v in event.items() if k not in self._ENVELOPE_KEYS}
                connection.execute(
                    "INSERT INTO ledger_events(event_id,type,ref,actor_id,payload,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        event["event_id"], event["type"], event["ref"], event["actor_id"],
                        json.dumps(body, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )

            self._materialize(connection, ledger)
            connection.execute(
                "INSERT INTO idempotent_requests(request_id,created_at,status,result) VALUES(?,?,?,?)",
                (request_id, now, "committed", json.dumps(
                    {"event_ids": [e["event_id"] for e in emitter.events]}, ensure_ascii=False)),
            )
            if self.crash_before_commit:
                raise sqlite3.OperationalError("模拟写盘失败：提交前崩溃")
            connection.commit()
            return ledger, list(emitter.events), False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _result_events(ledger: Ledger, result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not result:
            return []
        wanted = set(result.get("event_ids", []))
        return [e for e in ledger.events if e.get("event_id") in wanted]

    # ---- 只读查询（走投影/事件）----
    @staticmethod
    def _revision_row(row: sqlite3.Row) -> Dict[str, Any]:
        basis = json.loads(row["basis"]) if row["basis"] else None
        return {
            "plan_ref": row["plan_ref"],
            "revision": int(row["revision"]),
            "state": row["state"],
            "params": json.loads(row["params"]),
            "reasons": json.loads(row["reasons"]),
            "conflicts": json.loads(row["conflicts"]),
            "holds": json.loads(row["holds"]),
            "basis": basis,
            "created_by": row["actor_id"],
        }

    def get_plan(self, ref: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM plan_revisions WHERE plan_ref=? AND latest=1", (ref,)
            ).fetchone()
        if row is None:
            raise NotFound("计划不存在:%s" % ref)
        return self._revision_row(row)

    def list_plans(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM plan_revisions WHERE latest=1 AND state=? ORDER BY plan_ref LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM plan_revisions WHERE latest=1 ORDER BY plan_ref LIMIT ?", (limit,)
                ).fetchall()
        return [self._revision_row(row) for row in rows]

    def list_holds(self, resource_type: Optional[str] = None, resource_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM active_holds WHERE 1=1"
        args: List[Any] = []
        if resource_type:
            sql += " AND resource_type=?"
            args.append(resource_type)
        if resource_id:
            sql += " AND resource_id=?"
            args.append(resource_id)
        sql += " ORDER BY start_hour, plan_ref"
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def list_resources(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                rows = connection.execute("SELECT * FROM resources WHERE kind=? ORDER BY id", (kind,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM resources ORDER BY kind,id").fetchall()
        items = []
        for row in rows:
            item = {"kind": row["kind"], "id": row["id"]}
            item.update(json.loads(row["attrs"]))
            items.append(item)
        return items

    def list_notices(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM notices ORDER BY notice_id").fetchall()
        items = []
        for row in rows:
            item = {"notice_id": row["notice_id"]}
            item.update(json.loads(row["payload"]))
            items.append(item)
        return items

    def safety_basis(self) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM safety_basis WHERE id=1").fetchone()
        if row is None:
            base = Ledger()
            return {"ukc_by_class": base.ukc_by_class, "escort_by_class": base.escort_by_class}
        return {
            "ukc_by_class": json.loads(row["ukc_by_class"]),
            "escort_by_class": json.loads(row["escort_by_class"]),
        }

    def plan_timeline(self, ref: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM plan_revisions WHERE plan_ref=? LIMIT 1", (ref,)
            ).fetchone()
            if exists is None:
                raise NotFound("计划不存在:%s" % ref)
            rows = connection.execute(
                "SELECT seq,event_id,type,actor_id,payload,created_at FROM ledger_events WHERE ref=? ORDER BY seq",
                (ref,),
            ).fetchall()
            system = connection.execute(
                "SELECT seq,event_id,type,ref,actor_id,payload,created_at FROM ledger_events"
                " WHERE type IN ('closure_published','safety_basis_changed') ORDER BY seq"
            ).fetchall()
        items = []
        for row in rows:
            item = {
                "seq": row["seq"], "event_id": row["event_id"], "type": row["type"],
                "actor_id": row["actor_id"], "created_at": row["created_at"],
            }
            item.update(json.loads(row["payload"]))
            items.append(item)
        sys_items = []
        for row in system:
            item = {
                "seq": row["seq"], "event_id": row["event_id"], "type": row["type"],
                "ref": row["ref"], "actor_id": row["actor_id"], "created_at": row["created_at"],
            }
            item.update(json.loads(row["payload"]))
            sys_items.append(item)
        return {"plan_events": items, "system_events": sys_items}

    def stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS total FROM plan_revisions WHERE latest=1 GROUP BY state"
            ).fetchall()
            event_count = connection.execute("SELECT COUNT(*) AS c FROM ledger_events").fetchone()["c"]
        return {"plans_by_state": {row["state"]: int(row["total"]) for row in rows}, "events": int(event_count)}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


class Emitter:
    """build 回调的事件构造器：emit 即分配 event_id、立即应用到归约器并暂存待落盘。

    作废->重修订->重新放行这类多步编排可在同一事务内连续 emit，
    归约器按 event_id 去重，保证整段重放幂等。
    """

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id
        self.events: List[Dict[str, Any]] = []

    def emit(self, ledger: Ledger, etype: str, ref: str,
             payload: Dict[str, Any], event_id: Optional[str] = None) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "event_id": event_id or uuid.uuid4().hex,
            "type": etype,
            "ref": ref,
            "actor_id": self.actor_id,
        }
        for key, value in payload.items():
            event[key] = value
        ledger.apply(event)
        self.events.append(event)
        return event
