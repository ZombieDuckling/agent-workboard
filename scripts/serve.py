#!/usr/bin/env python3
"""Serve the workboard web view on 127.0.0.1 only.

GET  /                 web/index.html
GET  /api/board        board.json + snapshots.json in one response
GET  /attachments/<card_id>/<attachment_id>  attachment bytes
POST /api/cards        create a card
PATCH /api/cards/<id>  update a card
DELETE /api/cards/<id> delete a card
POST /api/cards/<id>/comments  append card comment event
POST /api/cards/<id>/attachments  upload attachment bytes as base64
DELETE /api/cards/<id>/attachments/<attachment_id>  remove attachment
POST /api/cards/<id>/needs  create need
PATCH /api/cards/<id>/needs/<need_id> answer or set status
POST /api/projects     create a project
DELETE /api/projects/<id>  delete an empty project; ?mode=move&to=<id> moves its cards and
                           sub-projects first, ?mode=all deletes its sub-projects and cards too
PATCH /api/projects/<id>  edit a project (title, key, path, machine, parent_id, herdr_workspaces, description, status, priority)
PUT /api/workspaces    replace the dashboard workspaces [{id, title, project_ids}]
POST /api/sync         run `workboard.py sync` (re-scan every machine)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import notify
import workboard
import wake

BOARD_DIR = workboard.BOARD_DIR
INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"
WORKBOARD = Path(__file__).resolve().parent / "workboard.py"
HOST, PORT = "127.0.0.1", workboard.WEB_PORT
MAX_BODY_BYTES = 64 * 1024
MAX_ATTACHMENT_BODY_BYTES = 36 * 1024 * 1024
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}
sync_lock = threading.Lock()


def read_json(name: str) -> dict:
    path = BOARD_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def wake_dispatch_loop() -> None:
    while True:
        try:
            wake.process_due()
        except Exception as exc:  # pragma: no cover - defensive logging in daemon loop
            print(f"wake dispatcher error: {exc}", file=sys.stderr, flush=True)
        try:
            notify.process()
        except Exception as exc:  # pragma: no cover - defensive logging in daemon loop
            print(f"reply notifier error: {exc}", file=sys.stderr, flush=True)
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def path_no_query(self) -> str:
        return self.path.split("?", 1)[0]

    def split_path(self, path: str) -> list[str]:
        if not path.startswith("/"):
            return []
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def card_id_from_path(self, path: str) -> str | None:
        parts = self.split_path(path)
        if len(parts) == 3 and parts[:2] == ["api", "cards"] and parts[2]:
            return parts[2]
        return None

    def comment_card_id_from_path(self, path: str) -> str | None:
        parts = self.split_path(path)
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "comments" and parts[2]:
            return parts[2]
        return None

    def attachment_get_ids(self, path: str) -> tuple[str, str] | None:
        parts = self.split_path(path)
        if len(parts) == 3 and parts[0] == "attachments" and parts[1] and parts[2]:
            return parts[1], parts[2]
        return None

    def attachment_post_card_id(self, path: str) -> str | None:
        parts = self.split_path(path)
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "attachments" and parts[2]:
            return parts[2]
        return None

    def attachment_delete_ids(self, path: str) -> tuple[str, str] | None:
        parts = self.split_path(path)
        if (
            len(parts) == 5
            and parts[:2] == ["api", "cards"]
            and parts[3] == "attachments"
            and parts[2]
            and parts[4]
        ):
            return parts[2], parts[4]
        return None

    def needs_post_card_id(self, path: str) -> str | None:
        parts = self.split_path(path)
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "needs" and parts[2]:
            return parts[2]
        return None

    def needs_patch_ids(self, path: str) -> tuple[str, str] | None:
        parts = self.split_path(path)
        if len(parts) == 5 and parts[:2] == ["api", "cards"] and parts[3] == "needs" and parts[2] and parts[4]:
            return parts[2], parts[4]
        return None

    def reject_foreign_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self.send_json(403, {"error": "forbidden origin"})
            return True
        return False

    def read_json_body(self, max_bytes: int = MAX_BODY_BYTES) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("unsupported content type")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise RuntimeError("missing content length")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise RuntimeError("invalid content length") from exc
        if length < 0:
            raise RuntimeError("invalid content length")
        if length > max_bytes:
            raise OverflowError("request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("invalid json") from exc
        if not isinstance(data, dict):
            raise RuntimeError("json body must be an object")
        return data

    def send_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data: dict) -> None:
        self.send_body(status, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        path = self.path_no_query()
        attachment_ids = self.attachment_get_ids(path)
        if path == "/":
            self.send_body(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/board":
            self.send_json(
                200,
                {
                    "board": read_json("board.json"),
                    "snapshots": read_json("snapshots.json"),
                    "wakes": workboard.wakes_for_api(),
                    "debt": read_json("debt.json"),
                    "owner": {"id": workboard.OWNER, "name": workboard.OWNER_NAME},
                },
            )
        elif attachment_ids:
            card_id, attachment_id = attachment_ids
            result = workboard.card_attachment(card_id, attachment_id)
            if not result:
                self.send_json(404, {"error": "not found"})
                return
            _, attachment, file_path = result
            media_type = attachment.get("type") or "application/octet-stream"
            lower = str(attachment.get("name", "")).lower()
            if lower.endswith((".md", ".markdown", ".txt")):
                media_type = "text/plain; charset=utf-8"
            # Same-origin HTML/SVG could script the board API, so only these render inline.
            inline = media_type.split(";")[0] in INLINE_TYPES
            if not inline:
                media_type = "application/octet-stream"
            body = file_path.read_bytes()
            disposition = "inline" if inline else "attachment"
            headers = {
                "Content-Disposition": f'{disposition}; filename="{attachment.get("name", "attachment")}"',
                "X-Content-Type-Options": "nosniff",
            }
            self.send_body(200, body, media_type, headers)
        else:
            self.send_json(404, {"error": "not found"})

    def handle_project_write(self, project_id: str | None) -> None:
        if self.reject_foreign_origin():
            return
        try:
            body = self.read_json_body()
        except ValueError:
            self.send_json(415, {"error": "content type must be application/json"})
            return
        except OverflowError:
            self.send_json(413, {"error": "request body too large"})
            return
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        try:
            if project_id is None:
                project = workboard.create_project(body)
                self.send_json(201, {"ok": True, "project": project})
            else:
                project = workboard.update_project(project_id, body)
                self.send_json(200, {"ok": True, "project": project})
        except workboard.CardNotFoundError:
            self.send_json(404, {"error": "project not found"})
        except workboard.BoardValidationError as exc:
            self.send_json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        path = self.path_no_query()
        if path == "/api/projects":
            self.handle_project_write(None)
            return
        if path == "/api/cards":
            if self.reject_foreign_origin():
                return
            try:
                body = self.read_json_body()
            except ValueError:
                self.send_json(415, {"error": "content type must be application/json"})
                return
            except OverflowError:
                self.send_json(413, {"error": "request body too large"})
                return
            except RuntimeError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            agent = body.pop("agent", workboard.OWNER)
            if not isinstance(agent, str) or not agent:
                self.send_json(400, {"error": "agent must be a non-empty string"})
                return
            try:
                card = workboard.create_card(body, agent)
            except workboard.BoardValidationError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {"ok": True, "card": card})
            return

        attachment_card_id = self.attachment_post_card_id(path)
        if attachment_card_id:
            if self.reject_foreign_origin():
                return
            try:
                body = self.read_json_body(max_bytes=MAX_ATTACHMENT_BODY_BYTES)
            except ValueError:
                self.send_json(415, {"error": "content type must be application/json"})
                return
            except OverflowError:
                self.send_json(413, {"error": "request body too large"})
                return
            except RuntimeError as exc:
                self.send_json(400, {"error": str(exc)})
                return

            agent = body.get("agent", workboard.OWNER)
            name = body.get("name")
            note = body.get("note", "")
            data_base64 = body.get("data_base64")
            if not isinstance(agent, str) or not agent:
                self.send_json(400, {"error": "agent must be a non-empty string"})
                return
            if not isinstance(name, str) or not name:
                self.send_json(400, {"error": "name must be a non-empty string"})
                return
            if not isinstance(data_base64, str) or not data_base64:
                self.send_json(400, {"error": "data_base64 must be a non-empty string"})
                return

            try:
                raw = base64.b64decode(data_base64, validate=True)
            except ValueError:
                self.send_json(400, {"error": "invalid base64 data"})
                return
            if len(raw) > workboard.MAX_ATTACHMENT_BYTES:
                self.send_json(413, {"error": "attachment exceeds 25 MB limit"})
                return

            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(raw)
                    temp_path = Path(tmp.name)
                card, attachment = workboard.add_attachment(
                    attachment_card_id,
                    temp_path,
                    agent=agent,
                    note=note,
                    name=name,
                )
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "card not found"})
                return
            except workboard.BoardValidationError as exc:
                if "25 MB" in str(exc):
                    self.send_json(413, {"error": str(exc)})
                else:
                    self.send_json(400, {"error": str(exc)})
                return
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            self.send_json(201, {"ok": True, "card": card, "attachment": attachment})
            return

        comment_card_id = self.comment_card_id_from_path(path)
        if comment_card_id:
            if self.reject_foreign_origin():
                return
            try:
                body = self.read_json_body()
            except ValueError:
                self.send_json(415, {"error": "content type must be application/json"})
                return
            except OverflowError:
                self.send_json(413, {"error": "request body too large"})
                return
            except RuntimeError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            agent = body.get("agent", workboard.OWNER)
            if not isinstance(agent, str) or not agent:
                self.send_json(400, {"error": "agent must be a non-empty string"})
                return
            wake_enabled = body.get("wake", True)
            if not isinstance(wake_enabled, bool):
                self.send_json(400, {"error": "wake must be a boolean"})
                return
            try:
                card, wake_entry = workboard.add_comment_and_wake(
                    comment_card_id,
                    body.get("text"),
                    agent,
                    wake=wake_enabled,
                )
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "card not found"})
                return
            except workboard.BoardValidationError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {"ok": True, "card": card, "wake": wake_entry})
            return

        needs_card_id = self.needs_post_card_id(path)
        if needs_card_id:
            if self.reject_foreign_origin():
                return
            try:
                body = self.read_json_body()
            except ValueError:
                self.send_json(415, {"error": "content type must be application/json"})
                return
            except OverflowError:
                self.send_json(413, {"error": "request body too large"})
                return
            except RuntimeError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            agent = body.get("agent", workboard.OWNER)
            if not isinstance(agent, str) or not agent:
                self.send_json(400, {"error": "agent must be a non-empty string"})
                return
            try:
                card, need = workboard.add_need(needs_card_id, body.get("text"), agent)
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "card not found"})
                return
            except workboard.BoardValidationError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(201, {"ok": True, "card": card, "need": need})
            return

        if path != "/api/sync":
            self.send_json(404, {"error": "not found"})
            return

        if self.reject_foreign_origin():
            return
        try:
            self.read_json_body()
        except ValueError:
            self.send_json(415, {"error": "content type must be application/json"})
            return
        except OverflowError:
            self.send_json(413, {"error": "request body too large"})
            return
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        if not sync_lock.acquire(blocking=False):
            self.send_json(409, {"error": "sync already running"})
            return
        try:
            result = subprocess.run(
                [sys.executable, str(WORKBOARD), "sync"],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            self.send_json(
                200 if result.returncode == 0 else 502,
                {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()},
            )
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "sync timed out"})
        finally:
            sync_lock.release()

    def do_PUT(self) -> None:
        if self.path_no_query() != "/api/workspaces":
            self.send_json(404, {"error": "not found"})
            return
        if self.reject_foreign_origin():
            return
        try:
            body = self.read_json_body()
        except ValueError:
            self.send_json(415, {"error": "content type must be application/json"})
            return
        except OverflowError:
            self.send_json(413, {"error": "request body too large"})
            return
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        try:
            workspaces = workboard.set_workspaces(body.get("workspaces"))
        except workboard.BoardValidationError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.send_json(200, {"ok": True, "workspaces": workspaces})

    def do_PATCH(self) -> None:
        path = self.path_no_query()
        project_match = re.fullmatch(r"/api/projects/([a-z0-9][a-z0-9-]{0,47})", path)
        if project_match:
            self.handle_project_write(project_match.group(1))
            return
        need_ids = self.needs_patch_ids(path)
        if need_ids:
            if self.reject_foreign_origin():
                return
            card_id, need_id = need_ids
            try:
                body = self.read_json_body()
            except ValueError:
                self.send_json(415, {"error": "content type must be application/json"})
                return
            except OverflowError:
                self.send_json(413, {"error": "request body too large"})
                return
            except RuntimeError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            agent = body.get("agent", workboard.OWNER)
            if not isinstance(agent, str) or not agent:
                self.send_json(400, {"error": "agent must be a non-empty string"})
                return
            wake_enabled = body.get("wake", True)
            if not isinstance(wake_enabled, bool):
                self.send_json(400, {"error": "wake must be a boolean"})
                return
            if "answer" not in body and "status" not in body:
                self.send_json(400, {"error": "body must include answer or status"})
                return
            card = None
            need = None
            wake_entry = None
            try:
                if "answer" in body:
                    card, need, wake_entry = workboard.answer_need_and_wake(
                        card_id,
                        need_id,
                        body.get("answer"),
                        agent,
                        wake=wake_enabled,
                    )
                if "status" in body:
                    card, need = workboard.set_need_status(card_id, need_id, body.get("status"), agent)
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "card not found"})
                return
            except workboard.NeedNotFoundError:
                self.send_json(404, {"error": "need not found"})
                return
            except workboard.BoardValidationError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, {"ok": True, "card": card, "need": need, "wake": wake_entry})
            return

        card_id = self.card_id_from_path(path)
        if not card_id:
            self.send_json(404, {"error": "not found"})
            return
        if self.reject_foreign_origin():
            return
        try:
            body = self.read_json_body()
        except ValueError:
            self.send_json(415, {"error": "content type must be application/json"})
            return
        except OverflowError:
            self.send_json(413, {"error": "request body too large"})
            return
        except RuntimeError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        agent = body.pop("agent", workboard.OWNER)
        if not isinstance(agent, str) or not agent:
            self.send_json(400, {"error": "agent must be a non-empty string"})
            return
        try:
            card = workboard.update_card(card_id, body, agent)
        except workboard.CardNotFoundError:
            self.send_json(404, {"error": "card not found"})
            return
        except workboard.BoardValidationError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.send_json(200, {"ok": True, "card": card})

    def do_DELETE(self) -> None:
        path = self.path_no_query()
        project_match = re.fullmatch(r"/api/projects/([a-z0-9][a-z0-9-]{0,47})", path)
        if project_match:
            if self.reject_foreign_origin():
                return
            query = parse_qs(urlsplit(self.path).query)
            mode = (query.get("mode") or ["empty"])[0]
            move_to = (query.get("to") or [None])[0]
            if mode not in workboard.PROJECT_DELETE_MODES:
                self.send_json(400, {"error": f"mode must be one of {', '.join(workboard.PROJECT_DELETE_MODES)}"})
                return
            try:
                summary = workboard.delete_project(project_match.group(1), mode=mode, move_to=move_to)
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "project not found"})
                return
            except workboard.BoardValidationError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            self.send_json(200, {"ok": True, **summary})
            return
        attachment_ids = self.attachment_delete_ids(path)
        if attachment_ids:
            if self.reject_foreign_origin():
                return
            card_id, attachment_id = attachment_ids
            agent = self.headers.get("X-Agent", workboard.OWNER)
            if not agent:
                agent = workboard.OWNER
            try:
                card, removed = workboard.remove_attachment(card_id, attachment_id, agent)
            except workboard.CardNotFoundError:
                self.send_json(404, {"error": "card not found"})
                return
            except workboard.AttachmentNotFoundError:
                self.send_json(404, {"error": "attachment not found"})
                return
            self.send_json(200, {"ok": True, "card": card, "attachment": removed})
            return

        card_id = self.card_id_from_path(path)
        if not card_id:
            self.send_json(404, {"error": "not found"})
            return
        if self.reject_foreign_origin():
            return
        try:
            workboard.delete_card(card_id)
        except workboard.CardNotFoundError:
            self.send_json(404, {"error": "card not found"})
            return
        self.send_json(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    threading.Thread(target=wake_dispatch_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Workboard on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
