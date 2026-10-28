from __future__ import annotations

import argparse
import base64
import io
import contextlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
os.environ["WB_CONFIG"] = os.devnull
for _key in ("WB_OWNER", "WB_OWNER_NAME", "WB_MACHINE", "WB_DATA_DIR", "WB_PORT", "WB_NOTIFY_CMD", "WB_YOLO"):
    os.environ.pop(_key, None)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import debt
import notify
import serve
import wake
import workboard


class WorkboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        shutil.copy2(REPO_ROOT / "tests" / "fixtures" / "board.json", self.tmp_path / "board.json")
        (self.tmp_path / "snapshots.json").write_text("{}", encoding="utf-8")
        (self.tmp_path / "BOARD.md").write_text("", encoding="utf-8")

        self._wb_globals = {
            "BOARD_DIR": workboard.BOARD_DIR,
            "BOARD_PATH": workboard.BOARD_PATH,
            "SNAPSHOTS_PATH": workboard.SNAPSHOTS_PATH,
            "BOARD_MD": workboard.BOARD_MD,
            "LOCK_PATH": workboard.LOCK_PATH,
            "WAKES_PATH": workboard.WAKES_PATH,
            "WAKES_LOCK_PATH": workboard.WAKES_LOCK_PATH,
        }
        workboard.BOARD_DIR = self.tmp_path
        workboard.BOARD_PATH = self.tmp_path / "board.json"
        workboard.SNAPSHOTS_PATH = self.tmp_path / "snapshots.json"
        workboard.BOARD_MD = self.tmp_path / "BOARD.md"
        workboard.LOCK_PATH = self.tmp_path / ".lock"
        workboard.WAKES_PATH = self.tmp_path / "wakes.json"
        workboard.WAKES_LOCK_PATH = self.tmp_path / ".wakes.lock"
        self._wake_env_backup = {
            key: os.environ.get(key)
            for key in ("WB_WAKE_HANDLE", "HERDR_PANE_ID", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID")
        }
        for key in self._wake_env_backup:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._wb_globals.items():
            setattr(workboard, key, value)
        for key, value in self._wake_env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def _start_server(self):
        web_dir = self.tmp_path / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        serve_globals = {
            "BOARD_DIR": serve.BOARD_DIR,
            "INDEX": serve.INDEX,
            "ALLOWED_ORIGINS": serve.ALLOWED_ORIGINS,
        }
        serve.BOARD_DIR = self.tmp_path
        serve.INDEX = web_dir / "index.html"
        server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        port = server.server_address[1]
        serve.ALLOWED_ORIGINS = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        return serve_globals, server, server_thread, port

    def _stop_server(self, serve_globals, server, server_thread):
        if server:
            server.shutdown()
            server.server_close()
        if server_thread:
            server_thread.join(timeout=5)
        for key, value in serve_globals.items():
            setattr(serve, key, value)

    def _request(self, port: int, method: str, path: str, body: bytes | str = b"", headers: dict | None = None):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(method, path, body=payload, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        headers_dict = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        conn.close()
        return status, raw, headers_dict

    def _enqueue_comment_wake(self, card_id: str, text: str = "Please check this") -> dict:
        _, wake_entry = workboard.add_comment_and_wake(card_id, text, "owner")
        assert wake_entry is not None
        return wake_entry

    def _set_wake_due(self, wake_id: str, due_at: str) -> None:
        with workboard.wakes_lock():
            queue = workboard.load_wakes()
            for entry in queue.get("wakes", []):
                if entry.get("id") == wake_id:
                    entry["due_at"] = due_at
                    break
            workboard.save_wakes(queue)

    def test_key_and_seq_assignment_on_create(self) -> None:
        first = workboard.create_card({"project_id": "sample-app", "title": "First"}, "agent-a")
        second = workboard.create_card({"project_id": "sample-app", "title": "Second"}, "agent-b")
        board = workboard.load_board()
        project = next(p for p in board["projects"] if p["id"] == "sample-app")

        self.assertIsInstance(project.get("key"), str)
        self.assertEqual(first["seq"] + 1, second["seq"])
        self.assertEqual(project["next_seq"], second["seq"] + 1)
        self.assertEqual(first["activity"][0]["type"], "created")
        self.assertEqual(first["activity"][0]["agent"], "agent-a")

    def test_migration_of_old_format_board(self) -> None:
        old_board = {
            "version": 1,
            "updated_at": "2026-10-01T00:00:00Z",
            "columns": [{"id": "backlog", "title": "Backlog"}, {"id": "in_progress", "title": "In progress"}, {"id": "done", "title": "Done"}],
            "machines": [{"id": "laptop", "title": "Laptop", "ssh": None}],
            "projects": [
                {"id": "storefront", "title": "Storefront", "path": None, "machine": "laptop", "parent_id": None},
                {"id": "workboard", "title": "Workboard", "path": None, "machine": "laptop", "parent_id": None},
                {"id": "build-ops", "title": "Build Ops", "path": None, "machine": "laptop", "parent_id": None},
                {"id": "ros-085", "title": "SHOP 085", "path": None, "machine": "laptop", "parent_id": "storefront"},
            ],
            "cards": [
                {
                    "id": "a",
                    "project_id": "workboard",
                    "title": "Later",
                    "column": "backlog",
                    "priority": "medium",
                    "owner_agent": "owner-a",
                    "last_agent": "owner-a",
                    "machine": "laptop",
                    "subproject": None,
                    "tags": [],
                    "links": [],
                    "notes": "",
                    "created_at": "2026-10-02T00:00:00Z",
                    "updated_at": "2026-10-02T00:00:00Z",
                },
                {
                    "id": "b",
                    "project_id": "workboard",
                    "title": "Earlier",
                    "column": "backlog",
                    "priority": "medium",
                    "owner_agent": None,
                    "last_agent": None,
                    "machine": "laptop",
                    "subproject": None,
                    "tags": [],
                    "links": [],
                    "notes": "",
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-01T00:00:00Z",
                },
            ],
        }
        (self.tmp_path / "board.json").write_text(json.dumps(old_board, indent=2) + "\n", encoding="utf-8")

        migrated = workboard.load_board()
        projects = {p["id"]: p for p in migrated["projects"]}
        cards = {c["id"]: c for c in migrated["cards"]}

        self.assertEqual(projects["storefront"]["key"], "STO")
        self.assertEqual(projects["workboard"]["key"], "WB")
        self.assertEqual(projects["build-ops"]["key"], "BO")
        self.assertEqual(projects["ros-085"]["key"], "R085")
        self.assertEqual(cards["b"]["seq"], 1)
        self.assertEqual(cards["a"]["seq"], 2)
        self.assertEqual(cards["b"]["activity"][0]["type"], "created")
        self.assertEqual(cards["b"]["activity"][0]["agent"], "unknown")
        self.assertEqual(projects["workboard"]["next_seq"], 3)

    def test_moved_updated_comment_events_and_noop_update(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Event test"}, "owner")
        baseline_count = len(card["activity"])

        no_change = workboard.update_card(card["id"], {"title": "Event test"}, "editor")
        self.assertEqual(len(no_change["activity"]), baseline_count)

        moved = workboard.update_card(card["id"], {"column": "in_progress"}, "editor")
        self.assertEqual(moved["activity"][-1]["type"], "moved")
        self.assertEqual(moved["activity"][-1]["from"], "backlog")
        self.assertEqual(moved["activity"][-1]["to"], "in_progress")

        updated = workboard.update_card(card["id"], {"notes": "new notes"}, "editor")
        self.assertEqual(updated["activity"][-1]["type"], "updated")
        self.assertEqual(updated["activity"][-1]["fields"], ["notes"])

        commented = workboard.add_comment(card["id"], "hello", "editor")
        self.assertEqual(commented["activity"][-1]["type"], "comment")
        self.assertEqual(commented["activity"][-1]["text"], "hello")

    def test_activity_capped_to_200(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Cap test"}, "owner")
        for idx in range(250):
            workboard.add_comment(card["id"], f"c{idx}", "owner")
        board = workboard.load_board()
        capped = workboard.find_card(board, card["id"])
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertEqual(len(capped["activity"]), 200)
        self.assertEqual(capped["activity"][0]["text"], "c50")
        self.assertEqual(capped["activity"][-1]["text"], "c249")

    def test_project_change_re_numbers_card(self) -> None:
        baseline = workboard.create_card({"project_id": "workboard", "title": "Already there"}, "agent")
        moving = workboard.create_card({"project_id": "sample-app", "title": "Move project"}, "agent")

        moved = workboard.update_card(moving["id"], {"project_id": "workboard"}, "agent")
        board = workboard.load_board()
        workboard_project = next(p for p in board["projects"] if p["id"] == "workboard")

        self.assertEqual(moved["project_id"], "workboard")
        self.assertEqual(moved["seq"], baseline["seq"] + 1)
        self.assertEqual(moved["activity"][-1]["type"], "updated")
        self.assertIn("project_id", moved["activity"][-1]["fields"])
        self.assertEqual(workboard_project["next_seq"], moved["seq"] + 1)

    def test_invalid_column_project_machine_rejected(self) -> None:
        with self.assertRaises(workboard.BoardValidationError):
            workboard.create_card(
                {"project_id": "sample-app", "title": "Bad column", "column": "nope"},
                "tester",
            )
        with self.assertRaises(workboard.BoardValidationError):
            workboard.create_card(
                {"project_id": "does-not-exist", "title": "Bad project"},
                "tester",
            )
        with self.assertRaises(workboard.BoardValidationError):
            workboard.create_card(
                {"project_id": "sample-app", "title": "Bad machine", "machine": "nope"},
                "tester",
            )

    def test_delete_and_unknown_id(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Delete me"}, "tester")
        removed = workboard.delete_card(card["id"])
        self.assertEqual(removed["id"], card["id"])
        self.assertIsNone(workboard.find_card(workboard.load_board(), card["id"]))
        with self.assertRaises(workboard.CardNotFoundError):
            workboard.update_card("missing-id", {"column": "done"}, "tester")
        with self.assertRaises(workboard.CardNotFoundError):
            workboard.delete_card("missing-id")

    def test_attachment_add_remove_and_delete_card_cleanup_and_safename(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Attach me"}, "tester")
        source = self.tmp_path / "source.txt"
        source.write_text("payload", encoding="utf-8")

        updated_card, attachment = workboard.add_attachment(
            card["id"],
            source,
            "tester",
            note="hello",
            name="../../etc/passwd",
        )
        self.assertEqual(attachment["name"], "passwd")
        target = workboard.attachment_path(card["id"], attachment["id"], attachment["name"])
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "payload")
        self.assertEqual(updated_card["activity"][-1]["type"], "attachment")

        detached_card, removed = workboard.remove_attachment(card["id"], attachment["id"], "tester")
        self.assertEqual(removed["id"], attachment["id"])
        self.assertFalse(target.exists())
        self.assertEqual(detached_card["activity"][-1]["type"], "attachment_removed")

        workboard.add_attachment(card["id"], source, "tester")
        card_dir = workboard.attachments_root() / card["id"]
        self.assertTrue(card_dir.exists())
        workboard.delete_card(card["id"])
        self.assertFalse(card_dir.exists())

    def test_attachment_size_limit(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Big file"}, "tester")
        large = self.tmp_path / "large.bin"
        with large.open("wb") as f:
            f.write(b"\0")
            f.seek(workboard.MAX_ATTACHMENT_BYTES)
            f.write(b"\0")
        with self.assertRaises(workboard.BoardValidationError):
            workboard.add_attachment(card["id"], large, "tester")

    def test_needs_open_answered_resolved_with_events(self) -> None:
        card = workboard.create_card({"project_id": "sample-app", "title": "Needful"}, "agent-a")
        updated_card, need = workboard.add_need(card["id"], "Please review this", "agent-a")
        self.assertEqual(need["status"], "open")
        self.assertEqual(updated_card["activity"][-1]["type"], "need")

        updated_card, need = workboard.answer_need(card["id"], need["id"], "Done, reviewed", "owner")
        self.assertEqual(need["status"], "answered")
        self.assertEqual(need["answered_by"], "owner")
        self.assertEqual(updated_card["activity"][-1]["type"], "need_answered")

        updated_card, need = workboard.set_need_status(card["id"], need["id"], "resolved", "agent-a")
        self.assertEqual(need["status"], "resolved")
        self.assertIsNotNone(need["resolved_at"])
        self.assertEqual(updated_card["activity"][-1]["type"], "need_resolved")

    def test_needs_command_filters_by_agent(self) -> None:
        first = workboard.create_card({"project_id": "sample-app", "title": "Card A"}, "owner")
        second = workboard.create_card({"project_id": "sample-app", "title": "Card B"}, "owner")
        workboard.add_need(first["id"], "agent-a need", "agent-a")
        workboard.add_need(second["id"], "agent-b need", "agent-b")
        workboard.add_need(second["id"], "agent-a answered", "agent-a")
        latest = workboard.load_board()
        second_latest = workboard.find_card(latest, second["id"])
        assert second_latest is not None
        need = next(n for n in second_latest["needs"] if n["text"] == "agent-a answered")
        workboard.answer_need(second["id"], need["id"], "Owner answer", "owner")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            workboard.cmd_needs(argparse.Namespace(agent="agent-a", all=False))
        output = buffer.getvalue()
        self.assertIn("agent-a need", output)
        self.assertIn("agent-a answered", output)
        self.assertIn("answer: Owner answer", output)
        self.assertNotIn("agent-b need", output)

    def test_concurrent_creates(self) -> None:
        errors: list[Exception] = []
        error_lock = threading.Lock()

        def writer(prefix: str) -> None:
            try:
                for idx in range(25):
                    workboard.create_card(
                        {
                            "id": f"{prefix}-{idx:02d}",
                            "project_id": "sample-app",
                            "title": f"{prefix} task {idx}",
                        },
                        f"agent-{prefix}",
                    )
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                with error_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        board = workboard.load_board()
        created = [c for c in board.get("cards", []) if c["id"].startswith(("t1-", "t2-"))]
        self.assertEqual(len(created), 50)
        self.assertEqual(len({c["id"] for c in created}), 50)

    def test_comment_endpoint_statuses(self) -> None:
        web_dir = self.tmp_path / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        serve_globals = {
            "BOARD_DIR": serve.BOARD_DIR,
            "INDEX": serve.INDEX,
            "ALLOWED_ORIGINS": serve.ALLOWED_ORIGINS,
        }
        server = None
        server_thread = None
        try:
            serve.BOARD_DIR = self.tmp_path
            serve.INDEX = web_dir / "index.html"
            server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            port = server.server_address[1]
            serve.ALLOWED_ORIGINS = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            def request(method: str, path: str, body: str = "", headers: dict | None = None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                raw = response.read()
                conn.close()
                return response.status, raw

            status, raw = request(
                "POST",
                "/api/cards",
                body=json.dumps({"project_id": "sample-app", "title": "API card"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            card_id = json.loads(raw.decode("utf-8"))["card"]["id"]

            status, _ = request(
                "POST",
                f"/api/cards/{card_id}/comments",
                body=json.dumps({"text": "looks good", "agent": "api"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)

            status, _ = request(
                "POST",
                f"/api/cards/{card_id}/comments",
                body=json.dumps({"text": ""}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)

            status, _ = request("DELETE", f"/api/cards/{card_id}")
            self.assertEqual(status, 200)

            status, _ = request(
                "POST",
                f"/api/cards/{card_id}/comments",
                body=json.dumps({"text": "missing card"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)

            status, _ = request(
                "POST",
                f"/api/cards/{card_id}/comments",
                body=json.dumps({"text": "forbidden"}),
                headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if server_thread:
                server_thread.join(timeout=5)
            for key, value in serve_globals.items():
                setattr(serve, key, value)

    def test_server_card_patch_origin_and_content_type_checks(self) -> None:
        web_dir = self.tmp_path / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        serve_globals = {
            "BOARD_DIR": serve.BOARD_DIR,
            "INDEX": serve.INDEX,
            "ALLOWED_ORIGINS": serve.ALLOWED_ORIGINS,
        }
        server = None
        server_thread = None
        try:
            serve.BOARD_DIR = self.tmp_path
            serve.INDEX = web_dir / "index.html"
            server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            port = server.server_address[1]
            serve.ALLOWED_ORIGINS = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            def request(method: str, path: str, body: str = "", headers: dict | None = None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                raw = response.read()
                conn.close()
                return response.status, raw

            status, raw = request(
                "POST",
                "/api/cards",
                body=json.dumps({"project_id": "sample-app", "title": "Write endpoint card"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            card_id = json.loads(raw.decode("utf-8"))["card"]["id"]

            status, _ = request(
                "PATCH",
                f"/api/cards/{card_id}",
                body=json.dumps({"column": "review"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)

            status, _ = request(
                "PATCH",
                f"/api/cards/{card_id}",
                body="{}",
                headers={"Content-Type": "text/plain"},
            )
            self.assertEqual(status, 415)

            status, _ = request(
                "POST",
                "/api/cards",
                body=json.dumps({"project_id": "sample-app", "title": "Blocked by origin"}),
                headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if server_thread:
                server_thread.join(timeout=5)
            for key, value in serve_globals.items():
                setattr(serve, key, value)

    def test_http_attachment_upload_get_and_traversal_404(self) -> None:
        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            status, raw, _ = self._request(
                port,
                "POST",
                "/api/cards",
                body=json.dumps({"project_id": "sample-app", "title": "HTTP Attach"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            card_id = json.loads(raw.decode("utf-8"))["card"]["id"]

            payload = b"%PDF-1.4\n%test\n"
            status, raw, _ = self._request(
                port,
                "POST",
                f"/api/cards/{card_id}/attachments",
                body=json.dumps(
                    {
                        "name": "report.pdf",
                        "data_base64": base64.b64encode(payload).decode("ascii"),
                        "agent": "api",
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            attachment_id = json.loads(raw.decode("utf-8"))["attachment"]["id"]

            status, body, headers = self._request(port, "GET", f"/attachments/{card_id}/{attachment_id}")
            self.assertEqual(status, 200)
            self.assertEqual(body, payload)
            self.assertEqual(headers.get("content-type"), "application/pdf")
            self.assertIn("inline; filename=\"report.pdf\"", headers.get("content-disposition", ""))

            for name in ("evil.html", "evil.svg"):
                status, raw, _ = self._request(
                    port,
                    "POST",
                    f"/api/cards/{card_id}/attachments",
                    body=json.dumps(
                        {"name": name, "data_base64": base64.b64encode(b"<script>1</script>").decode("ascii")}
                    ),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 201)
                evil_id = json.loads(raw.decode("utf-8"))["attachment"]["id"]
                status, _, headers = self._request(port, "GET", f"/attachments/{card_id}/{evil_id}")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "application/octet-stream")
                self.assertTrue(headers.get("content-disposition", "").startswith("attachment;"))

            status, _, _ = self._request(port, "GET", "/attachments/../../etc/passwd/anything")
            self.assertEqual(status, 404)
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_project_create_update_validation_and_api(self) -> None:
        proj = workboard.create_project({"id": "new-thing", "title": "New thing", "machine": "buildbox", "description": "**hi**"})
        self.assertEqual((proj["status"], proj["priority"], proj["machine"]), ("active", "medium", "buildbox"))
        self.assertTrue(proj["key"])
        with self.assertRaises(workboard.BoardValidationError):
            workboard.create_project({"id": "Bad Id", "title": "x"})
        with self.assertRaises(workboard.BoardValidationError):
            workboard.update_project("new-thing", {"status": "deleted"})
        with self.assertRaises(workboard.BoardValidationError):
            workboard.update_project("sample-app", {"parent_id": "sample-app"})
        child = workboard.create_project({"id": "child-thing", "title": "Child", "parent_id": "new-thing"})
        with self.assertRaises(workboard.BoardValidationError):
            workboard.update_project("new-thing", {"parent_id": child["id"]})
        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            json_headers = {"Content-Type": "application/json"}
            status, raw, _ = self._request(port, "PATCH", "/api/projects/new-thing",
                                           body=json.dumps({"status": "archived", "herdr_workspaces": ["w-label"]}), headers=json_headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["project"]["status"], "archived")
            status, _, _ = self._request(port, "PATCH", "/api/projects/nope", body="{}", headers=json_headers)
            self.assertEqual(status, 404)
            status, _, _ = self._request(port, "POST", "/api/projects", body=json.dumps({"id": "web-made", "title": "Web"}),
                                         headers={**json_headers, "Origin": "http://evil.example"})
            self.assertEqual(status, 403)
            status, _, _ = self._request(port, "POST", "/api/projects", body="id=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 415)
            status, raw, _ = self._request(port, "POST", "/api/projects", body=json.dumps({"id": "web-made", "title": "Web"}), headers=json_headers)
            self.assertEqual(status, 201)
            status, _, _ = self._request(port, "DELETE", "/api/projects/new-thing")
            self.assertEqual(status, 409)
            status, _, _ = self._request(port, "DELETE", "/api/projects/web-made")
            self.assertEqual(status, 200)
            status, _, _ = self._request(port, "DELETE", "/api/projects/web-made")
            self.assertEqual(status, 404)
            status, _, _ = self._request(port, "DELETE", "/api/projects/new-thing?mode=nuke")
            self.assertEqual(status, 400)
            status, _, _ = self._request(port, "DELETE", "/api/projects/new-thing?mode=move&to=child-thing")
            self.assertEqual(status, 409)
            status, raw, _ = self._request(port, "DELETE", "/api/projects/new-thing?mode=move&to=sample-app")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["moved_projects"], 1)
            board = workboard.load_board()
            self.assertEqual(next(p for p in board["projects"] if p["id"] == "child-thing")["parent_id"], "sample-app")
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_workspaces_validate_prune_and_api(self) -> None:
        workboard.create_project({"id": "ws-a", "title": "A"})
        workboard.create_project({"id": "ws-b", "title": "B"})
        saved = workboard.set_workspaces([{"title": "Client sites", "project_ids": ["ws-a", "ws-b", "ws-a"]}])
        self.assertEqual(saved, [{"id": "client-sites", "title": "Client sites", "project_ids": ["ws-a", "ws-b"]}])
        for bad in ("nope", [{"title": ""}], [{"title": "X", "project_ids": ["missing"]}], [{"title": "X"}, {"title": "x"}]):
            with self.assertRaises(workboard.BoardValidationError):
                workboard.set_workspaces(bad)
        workboard.delete_project("ws-b")
        self.assertEqual(workboard.load_board()["workspaces"][0]["project_ids"], ["ws-a"])
        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            json_headers = {"Content-Type": "application/json"}
            body = json.dumps({"workspaces": [{"id": "focus", "title": "Focus", "project_ids": ["ws-a"]}]})
            status, raw, _ = self._request(port, "PUT", "/api/workspaces", body=body, headers=json_headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["workspaces"][0]["id"], "focus")
            status, _, _ = self._request(port, "PUT", "/api/workspaces", body=body, headers={**json_headers, "Origin": "http://evil.example"})
            self.assertEqual(status, 403)
            status, _, _ = self._request(port, "PUT", "/api/workspaces", body=json.dumps({"workspaces": [{"title": "X", "project_ids": ["zz"]}]}), headers=json_headers)
            self.assertEqual(status, 400)
            status, _, _ = self._request(port, "PUT", "/api/other", body="{}", headers=json_headers)
            self.assertEqual(status, 404)
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_delete_project_move_and_all(self) -> None:
        workboard.create_project({"id": "old", "title": "Old"})
        workboard.create_project({"id": "old-kid", "title": "Old kid", "parent_id": "old"})
        workboard.create_project({"id": "dest", "title": "Dest"})
        first = workboard.create_card({"project_id": "old", "title": "A"}, "owner")
        workboard.create_card({"project_id": "old", "title": "B"}, "owner")
        workboard.create_card({"project_id": "dest", "title": "Already there"}, "owner")
        kid_card = workboard.create_card({"project_id": "old-kid", "title": "Kid card"}, "owner")
        with self.assertRaises(workboard.BoardValidationError):
            workboard.delete_project("old")
        with self.assertRaises(workboard.BoardValidationError):
            workboard.delete_project("old", mode="move", move_to="old-kid")
        summary = workboard.delete_project("old", mode="move", move_to="dest")
        self.assertEqual((summary["moved_cards"], summary["moved_projects"]), (2, 1))
        board = workboard.load_board()
        self.assertNotIn("old", {p["id"] for p in board["projects"]})
        moved = workboard.find_card(board, first["id"])
        self.assertEqual((moved["project_id"], moved["seq"]), ("dest", 2))
        self.assertEqual(moved["activity"][-1]["fields"], ["project_id"])

        att_dir = workboard.attachments_root() / kid_card["id"]
        att_dir.mkdir(parents=True)
        (att_dir / "x.txt").write_text("x")
        summary = workboard.delete_project("dest", mode="all")
        self.assertEqual(summary["deleted_projects"], ["dest", "old-kid"])
        self.assertEqual(summary["deleted_cards"], 4)
        board = workboard.load_board()
        self.assertFalse({"dest", "old-kid"} & {p["id"] for p in board["projects"]})
        self.assertFalse(any(c.get("project_id") in {"dest", "old-kid"} for c in board["cards"]))
        self.assertFalse(att_dir.exists())

    def test_http_need_patch_200_404_and_foreign_origin_403(self) -> None:
        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            status, raw, _ = self._request(
                port,
                "POST",
                "/api/cards",
                body=json.dumps({"project_id": "sample-app", "title": "HTTP Need"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            card_id = json.loads(raw.decode("utf-8"))["card"]["id"]

            status, raw, _ = self._request(
                port,
                "POST",
                f"/api/cards/{card_id}/needs",
                body=json.dumps({"text": "Need answer", "agent": "wb-api3"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            need_id = json.loads(raw.decode("utf-8"))["need"]["id"]

            status, raw, _ = self._request(
                port,
                "PATCH",
                f"/api/cards/{card_id}/needs/{need_id}",
                body=json.dumps({"answer": "Answered", "agent": "owner"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["need"]["status"], "answered")

            status, _, _ = self._request(
                port,
                "PATCH",
                f"/api/cards/{card_id}/needs/deadbeef",
                body=json.dumps({"answer": "nope"}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 404)

            status, _, _ = self._request(
                port,
                "PATCH",
                f"/api/cards/{card_id}/needs/{need_id}",
                body=json.dumps({"status": "resolved"}),
                headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_capture_wake_target_from_wb_wake_handle(self) -> None:
        payload = {
            "machine": "buildbox",
            "kind": "claude",
            "pane_id": "w1:p2",
            "workspace_id": "w1",
            "terminal_id": "term_123",
            "session_id": "sess_1",
            "cwd": "/tmp/demo",
        }
        os.environ["WB_WAKE_HANDLE"] = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        card = workboard.create_card({"project_id": "sample-app", "title": "Wake handle capture"}, "wb-agent")
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["wake_target"]["machine"], "buildbox")
        self.assertEqual(loaded["wake_target"]["kind"], "claude")
        self.assertEqual(loaded["wake_target"]["pane_id"], "w1:p2")
        self.assertEqual(loaded["wake_target"]["agent"], "wb-agent")
        self.assertIn("at", loaded["wake_target"])

    def test_capture_wake_target_from_herdr_get(self) -> None:
        os.environ["HERDR_PANE_ID"] = "w9:p12"
        stdout = json.dumps(
            {
                "result": {
                    "agent": {
                        "agent": "cursor",
                        "pane_id": "w9:p12",
                        "workspace_id": "w9",
                        "terminal_id": "term_abc",
                        "cwd": "/Users/demo/agent-workboard",
                        "agent_session": {"value": "sess_abc"},
                    }
                }
            }
        )
        completed = subprocess.CompletedProcess(["herdr"], 0, stdout=stdout, stderr="")
        with mock.patch("workboard.subprocess.run", return_value=completed):
            card = workboard.create_card({"project_id": "sample-app", "title": "Herdr capture"}, "wb-agent")
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["wake_target"]["machine"], "laptop")
        self.assertEqual(loaded["wake_target"]["kind"], "cursor")
        self.assertEqual(loaded["wake_target"]["terminal_id"], "term_abc")
        self.assertEqual(loaded["wake_target"]["session_id"], "sess_abc")

    def test_agent_comment_does_not_enqueue_but_owner_comment_does(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Wake queue checks"}, "owner")

        _, wake_entry = workboard.add_comment_and_wake(card["id"], "worker update", "wb-agent")
        self.assertIsNone(wake_entry)
        self.assertEqual(workboard.load_wakes()["wakes"], [])

        _, wake_entry = workboard.add_comment_and_wake(card["id"], "owner ping", "owner")
        assert wake_entry is not None
        self.assertEqual(wake_entry["reason"], "comment")
        self.assertEqual(len(workboard.load_wakes()["wakes"]), 1)

    def test_no_wake_flag_and_api_wake_false(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Wake toggle"}, "owner")
        _, wake_entry = workboard.add_comment_and_wake(card["id"], "no wake", "owner", wake=False)
        self.assertIsNone(wake_entry)
        self.assertEqual(workboard.load_wakes()["wakes"], [])

        with mock.patch.object(
            sys,
            "argv",
            ["workboard.py", "comment", card["id"], "-a", "owner", "-n", "cli no wake", "--no-wake"],
        ):
            self.assertEqual(workboard.main(), 0)
        self.assertEqual(workboard.load_wakes()["wakes"], [])

        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            status, raw, _ = self._request(
                port,
                "POST",
                f"/api/cards/{card['id']}/comments",
                body=json.dumps({"text": "api no wake", "agent": "owner", "wake": False}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            payload = json.loads(raw.decode("utf-8"))
            self.assertIn("wake", payload)
            self.assertIsNone(payload["wake"])
            self.assertEqual(workboard.load_wakes()["wakes"], [])

            _, need = workboard.add_need(card["id"], "Need API answer", "wb-worker")
            status, raw, _ = self._request(
                port,
                "PATCH",
                f"/api/cards/{card['id']}/needs/{need['id']}",
                body=json.dumps({"answer": "No wake answer", "agent": "owner", "wake": False}),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            payload = json.loads(raw.decode("utf-8"))
            self.assertIn("wake", payload)
            self.assertIsNone(payload["wake"])
            self.assertEqual(workboard.load_wakes()["wakes"], [])
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_need_answer_enqueues_wake_and_debounce_merges_texts(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Need answer wake"}, "owner")
        _, need = workboard.add_need(card["id"], "Which branch?", "wb-worker")
        _, _, wake_entry = workboard.answer_need_and_wake(card["id"], need["id"], "main", "owner")
        assert wake_entry is not None
        self.assertEqual(wake_entry["reason"], "need_answer")
        self.assertIn('Answer to your need "Which branch?": main', wake_entry["texts"][0])

        first = self._enqueue_comment_wake(card["id"], "first")
        second = self._enqueue_comment_wake(card["id"], "second")
        queue = workboard.load_wakes()["wakes"]
        pending = [entry for entry in queue if entry["status"] == "pending" and entry["card_id"] == card["id"]]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], second["id"])
        self.assertEqual(pending[0]["texts"][-2:], ["first", "second"])
        self.assertEqual(pending[0]["due_at"], second["due_at"])

    def test_key_lookup_works_for_mutations(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Key lookup"}, "owner")
        board = workboard.load_board()
        key = workboard.card_key(board, workboard.find_card(board, card["id"]))
        workboard.update_card(key, {"column": "in_progress"}, "owner")
        workboard.add_comment(key, "hello", "owner")
        _, need = workboard.add_need(key, "Need answer", "owner")
        workboard.set_need_status(key, need["id"], "resolved", "owner")
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["column"], "in_progress")
        self.assertEqual(loaded["activity"][-1]["type"], "need_resolved")

    def test_api_board_includes_wakes(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Board wakes"}, "owner")
        self._enqueue_comment_wake(card["id"], "wake me")
        serve_globals = server = server_thread = None
        try:
            serve_globals, server, server_thread, port = self._start_server()
            status, raw, _ = self._request(port, "GET", "/api/board")
            self.assertEqual(status, 200)
            payload = json.loads(raw.decode("utf-8"))
            self.assertIn("wakes", payload)
            self.assertTrue(any(entry["card_id"] == card["id"] for entry in payload["wakes"]))
        finally:
            if serve_globals is not None:
                self._stop_server(serve_globals, server, server_thread)

    def test_wake_dispatch_message_path(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Dispatch message"}, "owner")
        with workboard.board_lock():
            board = workboard.load_board()
            stored = workboard.find_card(board, card["id"])
            assert stored is not None
            stored["wake_target"] = {
                "machine": "laptop",
                "kind": "claude",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "terminal_id": "term_same",
                "session_id": "sess_1",
                "cwd": str(self.tmp_path),
                "agent": "wb-worker",
                "at": workboard.utc_now(),
            }
            workboard.save_board(board)
        wake_entry = self._enqueue_comment_wake(card["id"], "please respond")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")
        prompts: list[str] = []

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "agent", "get"]:
                return (
                    0,
                    json.dumps(
                        {
                            "result": {
                                "agent": {
                                    "agent": "claude",
                                    "agent_status": "idle",
                                    "pane_id": "w1:p1",
                                    "workspace_id": "w1",
                                    "terminal_id": "term_same",
                                    "cwd": str(self.tmp_path),
                                    "agent_session": {"value": "sess_1"},
                                }
                            }
                        }
                    ),
                    "",
                )
            if argv[:3] == ["herdr", "agent", "prompt"]:
                prompts.append(input_text or "")
                return 0, "", ""
            return 1, "", "unexpected"

        processed = wake.process_due(now="2030-01-01T00:00:00Z", runner=runner)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["status"], "delivered")
        self.assertEqual(processed[0]["method"], "message")
        self.assertEqual(len(prompts), 1)
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["activity"][-1]["type"], "wake")
        self.assertEqual(loaded["activity"][-1]["status"], "delivered")

    def test_wake_dispatch_blocked_retries_then_fails(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Dispatch blocked"}, "owner")
        with workboard.board_lock():
            board = workboard.load_board()
            stored = workboard.find_card(board, card["id"])
            assert stored is not None
            stored["wake_target"] = {
                "machine": "laptop",
                "kind": "claude",
                "pane_id": "w2:p2",
                "workspace_id": "w2",
                "terminal_id": "term_blocked",
                "session_id": "sess_blocked",
                "cwd": str(self.tmp_path),
                "agent": "wb-worker",
                "at": workboard.utc_now(),
            }
            workboard.save_board(board)
        wake_entry = self._enqueue_comment_wake(card["id"], "check blocker")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "agent", "get"]:
                return (
                    0,
                    json.dumps(
                        {
                            "result": {
                                "agent": {
                                    "agent": "claude",
                                    "agent_status": "blocked",
                                    "pane_id": "w2:p2",
                                    "workspace_id": "w2",
                                    "terminal_id": "term_blocked",
                                    "cwd": str(self.tmp_path),
                                    "agent_session": {"value": "sess_blocked"},
                                }
                            }
                        }
                    ),
                    "",
                )
            return 1, "", "unexpected"

        for idx in range(10):
            now = f"2030-01-01T00:{idx:02d}:00Z"
            wake.process_due(now=now, runner=runner)

        queue = workboard.load_wakes()["wakes"]
        saved = next(entry for entry in queue if entry["id"] == wake_entry["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["attempts"], 10)
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["activity"][-1]["status"], "failed")
        self.assertIn("waiting for input", loaded["activity"][-1]["text"])

    def test_wake_dispatch_dead_pane_resumes_session(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Dispatch resume"}, "owner")
        with workboard.board_lock():
            board = workboard.load_board()
            stored = workboard.find_card(board, card["id"])
            assert stored is not None
            stored["wake_target"] = {
                "machine": "laptop",
                "kind": "claude",
                "pane_id": "w3:p1",
                "workspace_id": "w3",
                "terminal_id": "term_old",
                "session_id": "sess_resume",
                "cwd": str(self.tmp_path),
                "agent": "wb-worker",
                "at": workboard.utc_now(),
            }
            workboard.save_board(board)
        wake_entry = self._enqueue_comment_wake(card["id"], "resume please")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "agent", "get"] and argv[3] == "w3:p1":
                return 1, "", "missing pane"
            if argv[:3] == ["herdr", "workspace", "list"]:
                return 0, json.dumps({"result": {"workspaces": [{"workspace_id": "w3", "label": "Workboard"}]}}), ""
            if argv[:3] == ["herdr", "tab", "create"]:
                return 0, json.dumps({"result": {"pane_id": "w3:p9", "tab_id": "w3:t9"}}), ""
            if argv[:3] == ["herdr", "agent", "start"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "prompt"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "get"] and argv[3] == "w3:p9":
                return (
                    0,
                    json.dumps(
                        {
                            "result": {
                                "agent": {
                                    "agent": "claude",
                                    "agent_status": "idle",
                                    "pane_id": "w3:p9",
                                    "workspace_id": "w3",
                                    "terminal_id": "term_new",
                                    "cwd": str(self.tmp_path),
                                    "agent_session": {"value": "sess_resume"},
                                }
                            }
                        }
                    ),
                    "",
                )
            return 1, "", "unexpected"

        processed = wake.process_due(now="2030-01-01T00:00:00Z", runner=runner)
        self.assertEqual(processed[0]["status"], "delivered")
        self.assertEqual(processed[0]["method"], "resume")

    def test_wake_dispatch_resume_failure_falls_back_to_spawn(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Resume then spawn"}, "owner")
        with workboard.board_lock():
            board = workboard.load_board()
            stored = workboard.find_card(board, card["id"])
            assert stored is not None
            stored["wake_target"] = {
                "machine": "laptop",
                "kind": "claude",
                "pane_id": "w4:p1",
                "workspace_id": "w4",
                "terminal_id": "term_old",
                "session_id": "sess_resume",
                "cwd": str(self.tmp_path),
                "agent": "wb-worker",
                "at": workboard.utc_now(),
            }
            workboard.save_board(board)
        wake_entry = self._enqueue_comment_wake(card["id"], "resume fallback")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")
        state = {"tab_calls": 0}

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "agent", "get"] and argv[3] == "w4:p1":
                return 1, "", "missing pane"
            if argv[:3] == ["herdr", "workspace", "list"]:
                return 0, json.dumps({"result": {"workspaces": [{"workspace_id": "w4", "label": "Workboard"}]}}), ""
            if argv[:3] == ["herdr", "tab", "create"]:
                state["tab_calls"] += 1
                pane = "w4:p8" if state["tab_calls"] == 1 else "w4:p9"
                tab = "w4:t8" if state["tab_calls"] == 1 else "w4:t9"
                return 0, json.dumps({"result": {"pane_id": pane, "tab_id": tab}}), ""
            if argv[:3] == ["herdr", "agent", "start"] and argv[3].startswith("wb-"):
                if "--resume" in argv or "sess_resume" in argv:
                    return 1, "", "resume start failed"
                return 0, "", ""
            if argv[:3] == ["herdr", "tab", "close"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "prompt"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "get"] and argv[3] == "w4:p9":
                return (
                    0,
                    json.dumps(
                        {
                            "result": {
                                "agent": {
                                    "agent": "opencode",
                                    "agent_status": "idle",
                                    "pane_id": "w4:p9",
                                    "workspace_id": "w4",
                                    "terminal_id": "term_spawn",
                                    "cwd": str(self.tmp_path),
                                }
                            }
                        }
                    ),
                    "",
                )
            return 1, "", "unexpected"

        processed = wake.process_due(now="2030-01-01T00:00:00Z", runner=runner)
        self.assertEqual(processed[0]["status"], "delivered")
        self.assertEqual(processed[0]["method"], "spawn")

    def test_wake_dispatch_spawn_falls_back_to_claude(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Spawn fallback"}, "owner")
        wake_entry = self._enqueue_comment_wake(card["id"], "spawn fallback")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")
        calls = {"start": 0}

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "workspace", "list"]:
                return 0, json.dumps({"result": {"workspaces": [{"workspace_id": "w5", "label": "Workboard"}]}}), ""
            if argv[:3] == ["herdr", "tab", "create"]:
                pane = "w5:p2" if calls["start"] == 0 else "w5:p3"
                tab = "w5:t2" if calls["start"] == 0 else "w5:t3"
                return 0, json.dumps({"result": {"pane_id": pane, "tab_id": tab}}), ""
            if argv[:3] == ["herdr", "agent", "start"]:
                calls["start"] += 1
                if calls["start"] == 1:
                    return 1, "", "opencode failed"
                return 0, "", ""
            if argv[:3] == ["herdr", "tab", "close"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "prompt"]:
                return 0, "", ""
            if argv[:3] == ["herdr", "agent", "get"]:
                return (
                    0,
                    json.dumps(
                        {
                            "result": {
                                "agent": {
                                    "agent": "claude",
                                    "agent_status": "idle",
                                    "pane_id": "w5:p3",
                                    "workspace_id": "w5",
                                    "terminal_id": "term_spawn",
                                    "cwd": str(self.tmp_path),
                                }
                            }
                        }
                    ),
                    "",
                )
            if argv[:2] == ["python3", "-c"]:
                return 0, "", ""
            return 1, "", "unexpected"

        processed = wake.process_due(now="2030-01-01T00:00:00Z", runner=runner)
        self.assertEqual(processed[0]["status"], "delivered")
        self.assertEqual(processed[0]["method"], "spawn")
        self.assertEqual(processed[0]["target"]["kind"], "claude")

    def test_wake_dispatch_all_spawn_kinds_fail_marks_failed_and_logs_activity(self) -> None:
        card = workboard.create_card({"project_id": "workboard", "title": "Spawn all fail"}, "owner")
        wake_entry = self._enqueue_comment_wake(card["id"], "all fail")
        self._set_wake_due(wake_entry["id"], "2000-01-01T00:00:00Z")

        def runner(machine, argv, input_text=None, timeout=0):
            if argv[:3] == ["herdr", "workspace", "list"]:
                return 0, json.dumps({"result": {"workspaces": [{"workspace_id": "w6", "label": "Workboard"}]}}), ""
            if argv[:3] == ["herdr", "tab", "create"]:
                return 0, json.dumps({"result": {"pane_id": "w6:p1", "tab_id": "w6:t1"}}), ""
            if argv[:3] == ["herdr", "agent", "start"]:
                return 1, "", "start failed"
            if argv[:3] == ["herdr", "tab", "close"]:
                return 0, "", ""
            if argv[:2] == ["python3", "-c"]:
                return 0, "", ""
            return 1, "", "unexpected"

        processed = wake.process_due(now="2030-01-01T00:00:00Z", runner=runner)
        self.assertEqual(processed[0]["status"], "failed")
        self.assertEqual(processed[0]["method"], "spawn")
        loaded = workboard.find_card(workboard.load_board(), card["id"])
        assert loaded is not None
        self.assertEqual(loaded["activity"][-1]["type"], "wake")
        self.assertEqual(loaded["activity"][-1]["status"], "failed")


class WakeRunnerTests(unittest.TestCase):
    def test_sub_project_inherits_parent_herdr_labels(self) -> None:
        board = {"projects": [
            {"id": "lum", "herdr_workspaces": ["~lum 1"]},
            {"id": "lum-e2e", "parent_id": "lum", "herdr_workspaces": []},
            {"id": "own", "parent_id": "lum", "herdr_workspaces": ["own-ws"]},
            {"id": "lonely"},
        ]}
        index = {p["id"]: p for p in board["projects"]}
        self.assertEqual(wake._project_labels(board, index["lum-e2e"]), ["~lum 1"])
        self.assertEqual(wake._project_labels(board, index["own"]), ["own-ws"])
        self.assertEqual(wake._project_labels(board, index["lonely"]), [])
        self.assertEqual(wake._project_labels(board, None), [])

    def test_local_prompt_passes_text_as_argument(self) -> None:
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(wake.subprocess, "run", return_value=done) as run:
            wake._default_runner("laptop", ["herdr", "agent", "prompt", "w9:p1"], input_text="hello owner", timeout=5)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-1], "hello owner")
        self.assertIsNone(run.call_args[1]["input"])

    def test_runner_timeout_returns_failure(self) -> None:
        with mock.patch.object(wake.subprocess, "run", side_effect=subprocess.TimeoutExpired("herdr", 5)):
            rc, _, err = wake._default_runner("laptop", ["herdr", "agent", "get", "w9:p1"], timeout=5)
        self.assertEqual(rc, 124)
        self.assertIn("TimeoutExpired", err)

    def test_find_live_agent_by_session_then_name(self) -> None:
        listing = json.dumps({"result": {"agents": [
            {"pane_id": "w1:p1", "name": "other", "agent_session": {"value": "s-1"}},
            {"pane_id": "w1:p2", "name": "wb-cx", "agent_session": None},
        ]}})
        runner = lambda machine, argv, input_text=None, timeout=10: (0, listing, "")
        self.assertEqual(wake._find_live_agent("laptop", {"session_id": "s-1", "agent": "wb-cx"}, runner)["pane_id"], "w1:p1")
        self.assertEqual(wake._find_live_agent("laptop", {"session_id": "nope", "agent": "wb-cx"}, runner)["pane_id"], "w1:p2")
        self.assertIsNone(wake._find_live_agent("laptop", {"session_id": None, "agent": "ghost"}, runner))

    def test_codex_sandbox_capture_uses_thread_id(self) -> None:
        env = {"CODEX_THREAD_ID": "thread-9"}
        with mock.patch.dict(os.environ, env, clear=True):
            target = workboard.detect_wake_target()
        self.assertEqual((target["kind"], target["session_id"], target["pane_id"]), ("codex", "thread-9", None))


class YoloArgsTests(unittest.TestCase):
    def test_every_spawned_kind_skips_approval_prompts(self) -> None:
        self.assertEqual(
            dict(wake.SPAWN_ORDER),
            {
                "opencode": ["--auto"],
                "claude": ["--dangerously-skip-permissions"],
                "codex": ["--dangerously-bypass-approvals-and-sandbox"],
                "cursor": ["--force", "--trust", "--sandbox", "disabled", "--approve-mcps"],
            },
        )

    def test_resumed_sessions_keep_yolo_mode(self) -> None:
        self.assertEqual(wake._resume_args("codex", "s1"), ["resume", "--dangerously-bypass-approvals-and-sandbox", "s1"])
        self.assertEqual(wake._resume_args("claude", "s1"), ["--resume", "s1", "--dangerously-skip-permissions"])
        self.assertEqual(wake._resume_args("opencode", "s1"), ["--session", "s1", "--auto"])
        self.assertEqual(wake._resume_args("hermes", "s1"), ["--resume", "s1", "--yolo"])
        self.assertEqual(wake._resume_args("cursor", "s1")[:2], ["--resume", "s1"])
        self.assertIn("--force", wake._resume_args("cursor", "s1"))


class NotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = mock.patch.object(workboard, "BOARD_DIR", Path(self.tmpdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        send = mock.patch.object(notify, "send", side_effect=lambda *args: self.sent.append(args))
        send.start()
        self.addCleanup(send.stop)

    @staticmethod
    def board(*activity: dict) -> dict:
        return {
            "projects": [{"id": "demo", "key": "DE"}],
            "cards": [{"id": "c1", "seq": 4, "project_id": "demo", "title": "Fix it", "activity": list(activity)}],
        }

    def test_only_agent_comments_after_owner_spoke_count_as_replies(self) -> None:
        board = self.board(
            {"at": "2026-10-27T08:00:00Z", "agent": "codex", "type": "comment", "text": "before Owner"},
            {"at": "2026-10-27T08:01:00Z", "agent": "owner", "type": "comment", "text": "why?"},
            {"at": "2026-10-27T08:02:00Z", "agent": "workboard", "type": "comment", "text": "system"},
            {"at": "2026-10-27T08:03:00Z", "agent": "codex", "type": "updated", "fields": ["status"]},
            {"at": "2026-10-27T08:04:00Z", "agent": "codex", "type": "comment", "text": "because"},
        )
        self.assertEqual([e["text"] for _c, e in notify.replies(board)], ["because"])

    def test_need_answer_counts_as_owner_speaking(self) -> None:
        board = self.board(
            {"at": "2026-10-27T08:01:00Z", "agent": "owner", "type": "need_answered", "answer": "yes"},
            {"at": "2026-10-27T08:02:00Z", "agent": "hermes", "type": "comment", "text": "done"},
        )
        self.assertEqual(len(notify.replies(board)), 1)

    def test_process_notifies_each_reply_once(self) -> None:
        owner = {"at": "2026-10-27T08:01:00Z", "agent": "owner", "type": "comment", "text": "explain"}
        reply = {"at": "2026-10-27T08:05:00Z", "agent": "opencode", "type": "comment", "text": "It **fails** because..."}
        self.assertEqual(notify.process(self.board(owner)), [])
        notify.save_state({"since": "2026-10-27T08:00:00Z"})
        notify.process(self.board(owner, reply))
        self.assertEqual(self.sent, [("opencode replied on DE-4", "Fix it", "It **fails** because...")])
        self.assertEqual(notify.plain(self.sent[0][2]), "It fails because...")
        same_second = {"at": "2026-10-27T08:05:00Z", "agent": "claude", "type": "comment", "text": "me too"}
        notify.process(self.board(owner, reply, same_second))
        notify.process(self.board(owner, reply, same_second))
        self.assertEqual([s[0] for s in self.sent], ["opencode replied on DE-4", "claude replied on DE-4"])

    def test_first_run_sets_watermark_without_backlog(self) -> None:
        board = self.board(
            {"at": "2026-10-27T08:01:00Z", "agent": "owner", "type": "comment", "text": "explain"},
            {"at": "2026-10-27T08:02:00Z", "agent": "codex", "type": "comment", "text": "old reply"},
        )
        notify.process(board)
        notify.process(board)
        self.assertEqual(self.sent, [])
        self.assertTrue(notify.load_state().get("since"))

    def test_many_replies_collapse_into_one_notification(self) -> None:
        events = [{"at": "2026-10-27T08:01:00Z", "agent": "owner", "type": "comment", "text": "go"}]
        events += [{"at": f"2026-10-27T08:0{i}:00Z", "agent": "codex", "type": "comment", "text": str(i)} for i in range(2, 7)]
        notify.save_state({"since": "2026-10-27T08:00:00Z"})
        notify.process(self.board(*events))
        self.assertEqual(self.sent, [("5 replies on the board", "codex", "On DE-4")])


class DebtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def make_repo(self) -> Path:
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "node_modules" / "x").mkdir(parents=True)
        (repo / "src" / "a.ts").write_text(
            "// TODO: split this\nconst a = 1; // @ts-ignore\n/* FIXME broken */\nconst s = \"// TODO in a string\";\n",
            encoding="utf-8",
        )
        (repo / "src" / "big.py").write_text("x = 1\n" * 900, encoding="utf-8")
        for i in range(8):
            (repo / "src" / f"m{i}.js").write_text("export const v = 1;\n", encoding="utf-8")
        (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
        (repo / "node_modules" / "x" / "i.js").write_text("// TODO vendored\n", encoding="utf-8")
        (repo / "notes.md").write_text("TODO: not code\n", encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q"], ["add", "-A", "-f"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)
        return repo

    def test_scan_counts_signals_and_ignores_vendored_and_docs(self) -> None:
        r = debt.scan(str(self.make_repo()))
        self.assertEqual(r["markers"], {"TODO": 1, "XXX": 0, "FIXME": 1, "HACK": 0})
        self.assertEqual(r["suppressions"], 1)
        self.assertEqual(r["big_files"], 1)
        self.assertEqual((r["source_files"], r["test_files"]), (10, 1))
        self.assertEqual(r["missing_tests"], 1)
        self.assertEqual(r["minutes"], 15 + 30 + 10 + 120 + 30)
        self.assertEqual(r["grade"], debt.grade_for(r["per_kloc"]))

    def test_scan_project_caches_until_tree_changes_and_skips_non_repos(self) -> None:
        repo = self.make_repo()
        cache: dict = {}
        first = debt.scan_project(str(repo), cache)
        self.assertIs(debt.scan_project(str(repo), cache), first)
        (repo / "src" / "a.ts").write_text("// HACK\n", encoding="utf-8")
        self.assertEqual(debt.scan_project(str(repo), cache)["markers"]["HACK"], 1)
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(debt.scan_project(str(plain), cache), {"skipped": "not a git repo"})
        self.assertEqual(debt.scan_project(str(self.root / "missing"), cache), {"error": "path not found"})

    def test_grade_boundaries(self) -> None:
        self.assertEqual([debt.grade_for(x) for x in (0, 29.9, 30, 59, 60, 119, 120, 239, 240)], list("AABBCCDDE"))

    def test_merge_keeps_one_history_point_per_day(self) -> None:
        board = {"projects": [{"id": "demo"}]}
        res = {"grade": "B", "minutes": 90, "loc": 2000, "per_kloc": 45.0}
        with mock.patch.object(workboard, "BOARD_DIR", self.root), mock.patch.object(workboard, "LOCK_PATH", self.root / ".lock"):
            workboard.merge_debt(board, {"demo": res, "gone": res}, {})
            store = workboard.merge_debt(board, {"demo": {**res, "minutes": 60}}, {"buildbox": "ssh failed"})
        self.assertEqual(list(store["projects"]), ["demo"])
        self.assertEqual([p["minutes"] for p in store["history"]["demo"]], [60])
        self.assertEqual(store["errors"], {"buildbox": "ssh failed"})


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.saved = {k: getattr(workboard, k) for k in ("BOARD_DIR", "BOARD_PATH", "SNAPSHOTS_PATH", "BOARD_MD", "LOCK_PATH")}
        workboard.BOARD_DIR = self.tmp_path
        workboard.BOARD_PATH = self.tmp_path / "board.json"
        workboard.SNAPSHOTS_PATH = self.tmp_path / "snapshots.json"
        workboard.BOARD_MD = self.tmp_path / "BOARD.md"
        workboard.LOCK_PATH = self.tmp_path / ".lock"

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            setattr(workboard, key, value)
        self.tmpdir.cleanup()

    def run_cli(self, *argv: str) -> int:
        with mock.patch.object(sys, "argv", ["workboard.py", *argv]), contextlib.redirect_stdout(io.StringIO()):
            return workboard.main()

    def test_config_file_sets_defaults_but_env_wins(self) -> None:
        cfg = self.tmp_path / "config.env"
        cfg.write_text("# comment\nexport WB_TEST_A='from file'\nWB_TEST_B=file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WB_TEST_B": "env"}):
            workboard._load_config(cfg)
            self.assertEqual(os.environ["WB_TEST_A"], "from file")
            self.assertEqual(os.environ["WB_TEST_B"], "env")
        os.environ.pop("WB_TEST_A", None)

    def test_init_then_machine_commands(self) -> None:
        self.assertEqual(self.run_cli("init", "--machine", "studio", "--title", "Studio"), 0)
        self.assertEqual(self.run_cli("init"), 1)
        self.assertEqual(self.run_cli("machine-add", "gpu-box", "--title", "GPU box", "--ssh", "gpu"), 0)
        board = workboard.load_board()
        self.assertEqual(workboard.local_machine_id(board), "studio")
        self.assertEqual(workboard.machine_ssh("gpu-box", board), "gpu")
        self.assertIsNone(workboard.machine_ssh("studio", board))
        self.assertEqual(board["machines"][1]["icon"], "server")
        self.assertEqual(self.run_cli("machine-rm", "gpu-box"), 0)
        self.assertEqual([m["id"] for m in workboard.load_board()["machines"]], ["studio"])

    def test_init_demo_builds_a_consistent_board(self) -> None:
        self.assertEqual(self.run_cli("init", "--demo"), 0)
        board = workboard.load_board()
        ids = {p["id"] for p in board["projects"]}
        self.assertTrue(all(c["project_id"] in ids for c in board["cards"]))
        self.assertTrue(any(n["status"] == "open" for c in board["cards"] for n in c["needs"]))
        self.assertEqual(set(json.loads(workboard.SNAPSHOTS_PATH.read_text())), {m["id"] for m in board["machines"]})
        self.assertTrue((self.tmp_path / "debt.json").exists())

    def test_notify_command_gets_message_on_stdin(self) -> None:
        out = self.tmp_path / "out.txt"
        script = self.tmp_path / "hook.sh"
        script.write_text(f'#!/bin/sh\n{{ echo "$WB_TITLE"; cat; }} > {out}\n', encoding="utf-8")
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"WB_NOTIFY_CMD": str(script)}), mock.patch.object(notify, "send_mac"), \
                mock.patch.object(notify.threading, "Thread", side_effect=lambda target, daemon: mock.Mock(start=target)):
            notify.send("codex replied on APP-1", "Fix login", "Done, see `auth.ts`")
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("codex replied on APP-1\n"))
        self.assertIn("Done, see auth.ts", text)


if __name__ == "__main__":
    unittest.main()
