"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


PLAN_RE = re.compile(r"^/api/plans/([^/]+)$")
PLAN_ACTION_RE = re.compile(r"^/api/plans/([^/]+)/(release|revise|berth|depart|cancel|audit)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "port-ledger/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _request_id(self) -> str:
            return self.headers.get("X-Idempotency-Key", "").strip() or None

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "port-ledger", "database": service.repository.health()})
                    return
                if path == "/":
                    self._send(200, (static_dir / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                actor = self._actor()
                if path == "/api/plans":
                    self._send(200, {"items": service.list_plans(
                        actor, state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["200"])[0]))})
                    return
                if path == "/api/holds":
                    self._send(200, {"items": service.list_holds(
                        actor, resource_type=query.get("resource_type", [None])[0],
                        resource_id=query.get("resource_id", [None])[0])})
                    return
                if path == "/api/resources":
                    self._send(200, {"items": service.list_resources(actor, kind=query.get("kind", [None])[0])})
                    return
                if path == "/api/notices":
                    self._send(200, {"items": service.list_notices(actor)})
                    return
                if path == "/api/safety-basis":
                    self._send(200, service.safety_basis(actor))
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return
                match = PLAN_ACTION_RE.match(path)
                if match and match.group(2) == "audit":
                    self._send(200, service.timeline(actor, unquote(match.group(1))))
                    return
                match = PLAN_RE.match(path)
                if match:
                    self._send(200, service.get_plan(actor, unquote(match.group(1))))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                actor = self._actor()
                rid = self._request_id()
                if path == "/api/resources":
                    self._send(201, service.register_resource(actor, body, rid))
                    return
                if path == "/api/closures":
                    self._send(201, service.publish_closure(actor, body, rid))
                    return
                if path == "/api/safety-basis":
                    self._send(200, service.change_safety_basis(actor, body, rid))
                    return
                if path == "/api/plans":
                    self._send(201, service.register_plan(actor, body.get("plan_ref", ""), body.get("params", {}), rid))
                    return
                if path == "/api/rebuild":
                    self._send(200, service.rebuild(actor))
                    return
                match = PLAN_ACTION_RE.match(path)
                if match:
                    ref = unquote(match.group(1))
                    action = match.group(2)
                    if action == "release":
                        self._send(200, service.release_plan(actor, ref, rid))
                    elif action == "revise":
                        self._send(200, service.revise_plan(actor, ref, body.get("params", {}), rid))
                    elif action == "berth":
                        self._send(200, service.berth_plan(actor, ref, body.get("actual_draft_m"), rid))
                    elif action == "depart":
                        self._send(200, service.depart_plan(actor, ref, rid))
                    else:
                        self._send(200, service.cancel_plan(actor, ref, rid))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
