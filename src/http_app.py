"""HTTP 接口（标准库实现，零第三方依赖）。

身份通过请求头传递（演示/内网部署约定）：
    X-User-Id / X-User-Role(dispatcher|crew|admin) / X-Crew-Id / X-User-Name
移动端幂等：请求体或头 X-Client-Id + X-Client-Seq；可选 base_seq 表达所基于的事件序号。

启动：python -m src [--host 0.0.0.0] [--port 8080] [--db data/facility.db]
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import DomainError
from .security import Identity
from .service import FacilityService

_ERROR_STATUS = {
    "not_found": 404,
    "permission_denied": 403,
    "stale_update": 409,
    "invalid_transition": 422,
    "merge_error": 422,
    "duplicate_client_event": 200,
    "domain_error": 400,
}


class FacilityHTTPHandler(BaseHTTPRequestHandler):
    service: FacilityService = None  # 由 make_server 注入
    server_version = "FacilityDispatch/1.0"

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        if self.server.debug:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON", "domain_error")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象", "domain_error")
        return data

    def _identity(self) -> Identity:
        return Identity.from_headers({k: v for k, v in self.headers.items()})

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: DomainError) -> None:
        status = _ERROR_STATUS.get(exc.code, 400)
        self._send(status, {"error": exc.code, "message": str(exc)})

    def _idempotency(self, body: dict[str, Any]) -> tuple[str | None, int | None]:
        client_id = body.pop("client_id", None) or self.headers.get("X-Client-Id")
        client_seq = body.pop("client_seq", None)
        if client_seq is None and self.headers.get("X-Client-Seq"):
            client_seq = self.headers["X-Client-Seq"]
        if client_seq is not None:
            client_seq = int(client_seq)
        return client_id, client_seq

    def _view(self, ticket: Any, identity: Identity) -> dict[str, Any]:
        from .dto import ticket_to_dict

        return ticket_to_dict(ticket, identity, now=self.service.clock())

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            identity = self._identity()

            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            if path == "/api/crews":
                self._send(200, {"crews": self.service.store.list_crews()})
                return
            if path == "/api/dashboard":
                self._send(200, self.service.dashboard(identity))
                return
            if path == "/api/tickets":
                self._list_tickets(identity)
                return

            m = re.fullmatch(r"/api/tickets/([A-Za-z0-9_]+)", path)
            if m:
                self._send(200, self.service.get_ticket(m.group(1), identity))
                return
            m = re.fullmatch(r"/api/tickets/([A-Za-z0-9_]+)/history", path)
            if m:
                self._send(200, {"events": self.service.history(m.group(1), identity)})
                return
            m = re.fullmatch(r"/api/sources/([^/]+)", path)
            if m:
                self._send(200, self.service.trace_source(m.group(1), identity))
                return
            self._send(404, {"error": "not_found", "message": f"无此路径：{path}"})
        except DomainError as exc:
            self._error(exc)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            identity = self._identity()
            body = self._read_json()
            svc = self.service

            if path == "/api/crews":
                crew = svc.register_crew(
                    body["crew_id"], body["name"], body["region"],
                    body.get("categories", []), body.get("active", True),
                )
                self._send(201, crew)
                return

            if path == "/api/reports":
                cid, cseq = self._idempotency(body)
                result = svc.report(
                    location=body["location"],
                    category=body.get("category", "other"),
                    risk=body.get("risk", 0),
                    photo_summary=body.get("photo_summary", ""),
                    reporter_name=body.get("reporter_name", ""),
                    reporter_contact=body.get("reporter_contact", ""),
                    region=body.get("region"),
                    identity=identity,
                    source_id=body.get("source_id"),
                    client_id=cid, client_seq=cseq,
                    auto_dispatch=body.get("auto_dispatch", True),
                )
                out = {
                    "ticket": self._view(result["ticket"], identity),
                    "deduped": result["deduped"],
                    "dispatch_error": result.get("dispatch_error"),
                }
                if result.get("event") is not None:
                    out["event_seq"] = result["event"].seq
                self._send(201 if not result["deduped"] else 200, out)
                return

            if path == "/api/merge":
                primary = svc.merge(
                    body["source_ticket_id"], body["primary_ticket_id"],
                    identity, body.get("reason", ""),
                )
                self._send(200, self._view(primary, identity))
                return

            if path == "/api/sweep-overdue":
                marked = svc.sweep_overdue(identity)
                self._send(200, {"marked": [t.ticket_id for t in marked]})
                return

            m = re.fullmatch(r"/api/tickets/([A-Za-z0-9_]+)/(\w[\w-]*)", path)
            if m:
                self._ticket_action(m.group(1), m.group(2), body, identity)
                return

            self._send(404, {"error": "not_found", "message": f"无此路径：{path}"})
        except DomainError as exc:
            self._error(exc)
        except (KeyError, ValueError) as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "message": str(exc)})

    def _ticket_action(self, ticket_id: str, action: str,
                       body: dict[str, Any], identity: Identity) -> None:
        svc = self.service

        if action == "supplement":
            t = svc.supplement(
                ticket_id, identity,
                photo_summary=body.get("photo_summary"),
                reporter_contact=body.get("reporter_contact"),
                risk=body.get("risk"),
                note=body.get("note", ""),
            )
            self._send(200, self._view(t, identity))
            return

        if action == "dispatch":
            t = svc.dispatch(ticket_id, identity,
                             crew_id=body.get("crew_id"),
                             reason=body.get("reason", "manual_dispatch"))
            self._send(200, self._view(t, identity))
            return

        if action == "transfer":
            t = svc.transfer(ticket_id, identity, body["to_crew"],
                             body.get("reason", ""))
            self._send(200, self._view(t, identity))
            return

        if action == "pause":
            t = svc.pause(ticket_id, identity, body.get("reason", ""))
            self._send(200, self._view(t, identity))
            return

        if action == "resume":
            t = svc.resume(ticket_id, identity)
            self._send(200, self._view(t, identity))
            return

        if action == "progress":
            cid, cseq = self._idempotency(body)
            if not cid or cseq is None:
                raise DomainError("进度上报必须携带 client_id 与 client_seq", "domain_error")
            result = svc.post_progress(
                ticket_id, identity,
                status_text=body.get("status_text", ""),
                phase=body.get("phase"),
                percent=body.get("percent", 0),
                client_id=cid, client_seq=cseq,
                base_seq=body.get("base_seq"),
            )
            self._send(200, {
                "ticket": self._view(result["ticket"], identity),
                "event_seq": result["event"].seq,
                "idempotent": result.get("idempotent", False),
            })
            return

        if action == "submit":
            cid, cseq = self._idempotency(body)
            t = svc.submit_for_acceptance(
                ticket_id, identity, summary=body.get("summary", ""),
                client_id=cid, client_seq=cseq,
            )
            self._send(200, self._view(t, identity))
            return

        if action == "review":
            t = svc.review_acceptance(
                ticket_id, identity,
                passed=bool(body.get("passed", False)),
                opinion=body.get("opinion", ""),
            )
            self._send(200, self._view(t, identity))
            return

        if action == "close-duplicate":
            t = svc.close_duplicate(
                ticket_id, body["primary_ticket_id"], identity,
                body.get("reason", ""),
            )
            self._send(200, self._view(t, identity))
            return

        self._send(404, {"error": "not_found", "message": f"无此操作：{action}"})

    def _list_tickets(self, identity: Identity) -> None:
        from .dto import ticket_to_dict

        out = [
            ticket_to_dict(t, identity, now=self.service.clock())
            for t in self.service.list_tickets(identity)
        ]
        self._send(200, {"tickets": out})


def make_server(host: str, port: int, db_path: str,
                clock: Callable[[], float] | None = None,
                debug: bool = False) -> ThreadingHTTPServer:
    service = FacilityService(db_path=db_path, clock=clock)

    handler = FacilityHTTPHandler
    handler.service = service

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.debug = debug  # type: ignore[attr-defined]
    httpd.service = service  # type: ignore[attr-defined]
    return httpd
