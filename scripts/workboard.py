#!/usr/bin/env python3
"""Cross-agent Kanban: one JSON source of truth (board.json) shared by every coding agent and machine."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import mimetypes
import os
import shutil
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("WB_CONFIG") or Path.home() / ".config" / "workboard" / "config.env").expanduser()


def _load_config(path: Path) -> None:
    """KEY=VALUE lines (shell-compatible) become environment defaults; real env vars win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_config(CONFIG_PATH)

BOARD_DIR = Path(os.environ.get("WB_DATA_DIR") or Path(__file__).resolve().parent.parent).expanduser()
OWNER = (os.environ.get("WB_OWNER") or "owner").strip().lower()
OWNER_NAME = (os.environ.get("WB_OWNER_NAME") or OWNER.capitalize()).strip()
WEB_PORT = int(os.environ.get("WB_PORT") or 4747)
BOARD_PATH = BOARD_DIR / "board.json"
SNAPSHOTS_PATH = BOARD_DIR / "snapshots.json"
BOARD_MD = BOARD_DIR / "BOARD.md"
LOCK_PATH = BOARD_DIR / ".lock"
WAKES_PATH = BOARD_DIR / "wakes.json"
WAKES_LOCK_PATH = BOARD_DIR / ".wakes.lock"
COLLECTOR = Path(__file__).resolve().parent / "collect.py"
DEBT_SCANNER = Path(__file__).resolve().parent / "debt.py"
DEBT_MAX_AGE_SECONDS = 1800
DEBT_HISTORY_DAYS = 120
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
CARD_UPDATE_FIELDS = {
    "title",
    "column",
    "notes",
    "project_id",
    "machine",
    "subproject",
    "priority",
    "owner_agent",
}
CARD_CREATE_FIELDS = CARD_UPDATE_FIELDS | {"id"}
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")
COMMENT_MAX_LEN = 4000
ACTIVITY_LIMIT = 200
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES = 36 * 1024 * 1024
NEED_STATUS = {"open", "answered", "resolved"}
NEED_TEXT_MAX_LEN = 4000
ATTACHMENT_NAME_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._ -")
MD2PDF = Path(__file__).resolve().parent / "md2pdf"
ATTACHMENT_URL_BASE = f"http://127.0.0.1:{WEB_PORT}/attachments"
WAKE_DEBOUNCE_SECONDS = 20
WAKE_RETRY_SECONDS = 60
WAKE_HISTORY_LIMIT = 200


class BoardValidationError(ValueError):
    pass


class CardNotFoundError(KeyError):
    pass


class AttachmentNotFoundError(KeyError):
    pass


class NeedNotFoundError(KeyError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def board_lock():
    """Serialise writers: agents on every machine and the sync job share these files."""
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


@contextmanager
def _path_lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(lock, flags)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _wake_due_at(now_iso: str, seconds: int = WAKE_DEBOUNCE_SECONDS) -> str:
    base = now_iso if isinstance(now_iso, str) and now_iso else utc_now()
    dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
    return (dt + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise_wakes(data: dict) -> dict:
    wakes = data.get("wakes")
    if not isinstance(wakes, list):
        data["wakes"] = []
        wakes = data["wakes"]
    if len(wakes) > WAKE_HISTORY_LIMIT:
        data["wakes"] = wakes[-WAKE_HISTORY_LIMIT:]
    return data


def load_wakes() -> dict:
    data = read_json(WAKES_PATH, {"wakes": []})
    return _normalise_wakes(data)


def save_wakes(data: dict) -> None:
    write_json(WAKES_PATH, _normalise_wakes(data))


@contextmanager
def wakes_lock():
    with _path_lock(WAKES_LOCK_PATH):
        yield


def is_owner(agent: str | None) -> bool:
    """The human who owns the board (WB_OWNER); their comments wake agents."""
    return isinstance(agent, str) and agent.strip().lower() == OWNER


def board_machines(board: dict | None = None) -> list[dict]:
    if board is None:
        board = read_json(BOARD_PATH, {})
    return [m for m in board.get("machines") or [] if isinstance(m, dict) and m.get("id")]


def local_machine_id(board: dict | None = None) -> str:
    """This machine's id: WB_MACHINE, else the first machine in board.json without an ssh host."""
    env = os.environ.get("WB_MACHINE")
    if env:
        return env
    for machine in board_machines(board):
        if not machine.get("ssh"):
            return machine["id"]
    return "local"


def machine_ssh(machine_id: str | None, board: dict | None = None) -> str | None:
    """SSH host for a machine, or None when it is the machine the board lives on."""
    for machine in board_machines(board):
        if machine["id"] == machine_id:
            return machine.get("ssh") or None
    return None


def _extract_wake_target_from_herdr(pane_id: str) -> dict | None:
    herdr_bin = shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
    try:
        result = subprocess.run(
            [herdr_bin, "agent", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    info = payload.get("result", {}).get("agent")
    if not isinstance(info, dict):
        return None
    session = info.get("agent_session")
    session_id = session.get("value") if isinstance(session, dict) else None
    kind = info.get("agent") if isinstance(info.get("agent"), str) else None
    return {
        "machine": local_machine_id(),
        "kind": kind,
        "pane_id": info.get("pane_id"),
        "workspace_id": info.get("workspace_id"),
        "terminal_id": info.get("terminal_id"),
        "session_id": session_id,
        "cwd": info.get("cwd"),
    }


def _guess_agent_kind() -> str | None:
    env = os.environ
    if env.get("CURSOR_AGENT") == "1":
        return "cursor"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("CLAUDECODE") == "1":
        return "claude"
    if any(k.startswith("OPENCODE") for k in env):
        return "opencode"
    return None


def detect_wake_target() -> dict | None:
    encoded = os.environ.get("WB_WAKE_HANDLE")
    if encoded:
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
            payload = json.loads(decoded)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            return payload

    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        from_herdr = _extract_wake_target_from_herdr(pane_id)
        if from_herdr:
            return from_herdr
        # Sandboxed agents (Codex) cannot reach the herdr socket; the dispatcher re-checks the pane at wake time.
        return {
            "machine": local_machine_id(),
            "kind": _guess_agent_kind(),
            "pane_id": pane_id,
            "workspace_id": os.environ.get("HERDR_WORKSPACE_ID"),
            "terminal_id": None,
            "session_id": os.environ.get("CURSOR_CONVERSATION_ID") if os.environ.get("CURSOR_AGENT") == "1" else None,
            "cwd": os.getcwd(),
        }

    if os.environ.get("CURSOR_AGENT") == "1":
        conversation_id = os.environ.get("CURSOR_CONVERSATION_ID")
        if conversation_id:
            return {
                "machine": local_machine_id(),
                "kind": "cursor",
                "pane_id": None,
                "workspace_id": None,
                "terminal_id": None,
                "session_id": conversation_id,
                "cwd": os.getcwd(),
            }
    codex_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if codex_thread:
        return {
            "machine": local_machine_id(),
            "kind": "codex",
            "pane_id": None,
            "workspace_id": None,
            "terminal_id": None,
            "session_id": codex_thread,
            "cwd": os.getcwd(),
        }
    return None


def _normalise_wake_target(raw: dict, board_agent: str, at: str) -> dict:
    return {
        "machine": raw.get("machine"),
        "kind": raw.get("kind"),
        "pane_id": raw.get("pane_id"),
        "workspace_id": raw.get("workspace_id"),
        "terminal_id": raw.get("terminal_id"),
        "session_id": raw.get("session_id"),
        "cwd": raw.get("cwd"),
        "agent": board_agent,
        "at": at,
    }


def _capture_wake_target(card: dict, agent: str, at: str) -> None:
    if is_owner(agent) or os.environ.get("WB_NO_WAKE_CAPTURE") == "1":
        return
    target = detect_wake_target()
    if not isinstance(target, dict):
        return
    card["wake_target"] = _normalise_wake_target(target, agent, at)


def enqueue_wake(card: dict, reason: str, text: str, now: str | None = None) -> dict | None:
    if reason not in {"comment", "need_answer", "manual"}:
        return None
    message = text.strip() if isinstance(text, str) else ""
    if not message:
        return None
    target = card.get("wake_target") if isinstance(card.get("wake_target"), dict) else None
    now_iso = now or utc_now()
    with wakes_lock():
        queue = load_wakes()
        wakes = queue.setdefault("wakes", [])
        pending = next(
            (entry for entry in reversed(wakes) if entry.get("status") == "pending" and entry.get("card_id") == card.get("id")),
            None,
        )
        due_at = _wake_due_at(now_iso)
        if pending:
            texts = pending.get("texts")
            if not isinstance(texts, list):
                texts = []
                pending["texts"] = texts
            texts.append(message)
            pending["due_at"] = due_at
            if target:
                pending["target"] = target
            pending["detail"] = "debounced"
            entry = pending
        else:
            entry = {
                "id": uuid.uuid4().hex[:8],
                "card_id": card.get("id"),
                "reason": reason,
                "texts": [message],
                "created_at": now_iso,
                "due_at": due_at,
                "status": "pending",
                "attempts": 0,
                "method": None,
                "detail": "",
                "target": target,
                "finished_at": None,
            }
            wakes.append(entry)
        save_wakes(queue)
        return dict(entry)


def wakes_for_api() -> list[dict]:
    queue = load_wakes()
    wakes = queue.get("wakes", [])
    pending = [entry for entry in wakes if entry.get("status") == "pending"]
    pending.sort(key=lambda e: (e.get("due_at", ""), e.get("created_at", "")))
    finished = [entry for entry in wakes if entry.get("status") in {"delivered", "failed"}]
    finished.sort(key=lambda e: e.get("finished_at") or e.get("created_at") or "", reverse=True)
    return pending + finished[:30]


def record_wake_result(
    card_id: str,
    status: str,
    method: str | None,
    text: str,
    target: dict | None = None,
) -> dict | None:
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            return None
        now = utc_now()
        if isinstance(target, dict):
            card["wake_target"] = target
        _append_activity(
            card,
            {
                "type": "wake",
                "at": now,
                "agent": "workboard",
                "status": status,
                "method": method,
                "text": text,
            },
        )
        card["updated_at"] = now
        save_board(board)
        return dict(card)


def _normalise_timestamp(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return utc_now()


def _created_event_for_card(card: dict) -> dict:
    return {
        "at": _normalise_timestamp(card.get("created_at")),
        "agent": card.get("owner_agent") or "unknown",
        "type": "created",
    }


def _append_activity(card: dict, event: dict) -> None:
    activity = card.setdefault("activity", [])
    if not isinstance(activity, list):
        activity = []
    activity.append(event)
    if len(activity) > ACTIVITY_LIMIT:
        del activity[:-ACTIVITY_LIMIT]
    card["activity"] = activity


def _project_key_base(project_id: str) -> str:
    lowered = project_id.lower()
    ros_match = re.fullmatch(r"ros-(\d+)", lowered)
    if ros_match:
        digits = ros_match.group(1)[:5] or "0"
        base = f"R{digits}"
    else:
        parts = [p for p in re.split(r"[^a-zA-Z0-9]+", lowered) if p]
        if len(parts) >= 2:
            base = "".join(part[0] for part in parts).upper()
        else:
            token = parts[0].upper() if parts else ""
            base = token[:3]
    if not base:
        base = "PR"
    if len(base) < 2:
        base = (base + "X")[:2]
    if not base[0].isalpha():
        base = f"P{base}"[:6]
    if len(base) > 6:
        base = base[:6]
    if len(base) < 2:
        base = (base + "X")[:2]
    return base


def _next_unique_key(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        s = str(suffix)
        room = max(1, 6 - len(s))
        candidate = f"{base[:room]}{s}"
        suffix += 1
    used.add(candidate)
    return candidate


def _is_valid_project_key(key: object) -> bool:
    return isinstance(key, str) and PROJECT_KEY_RE.fullmatch(key) is not None


def _sort_cards_for_seq(cards: list[dict]) -> list[dict]:
    return sorted(cards, key=lambda c: (_normalise_timestamp(c.get("created_at")), c.get("id", "")))


def _migrate_board(board: dict) -> None:
    projects = board.get("projects", [])
    if not isinstance(projects, list):
        return

    cards = board.get("cards", [])
    if not isinstance(cards, list):
        cards = []
        board["cards"] = cards

    # Ensure every project has a unique Jira-style key.
    used_keys: set[str] = set()
    pending_projects: list[dict] = []
    for project in projects:
        key = project.get("key")
        if _is_valid_project_key(key) and key not in used_keys:
            used_keys.add(key)
        else:
            pending_projects.append(project)

    for project in pending_projects:
        project["key"] = _next_unique_key(_project_key_base(project.get("id", "")), used_keys)

    overrides = {"workboard": "WB"}
    by_project_id = {project.get("id"): project for project in projects}
    for project_id, wanted_key in overrides.items():
        project = by_project_id.get(project_id)
        if not project:
            continue
        current = project.get("key")
        if current == wanted_key:
            continue
        owner = next((p for p in projects if p is not project and p.get("key") == wanted_key), None)
        if owner is None:
            if isinstance(current, str):
                used_keys.discard(current)
            project["key"] = wanted_key
            used_keys.add(wanted_key)

    # Ensure every card has activity and cap long activity history.
    for card in cards:
        activity = card.get("activity")
        if not isinstance(activity, list):
            card["activity"] = []
            activity = card["activity"]
        if not activity:
            card["activity"] = [_created_event_for_card(card)]
        elif len(activity) > ACTIVITY_LIMIT:
            card["activity"] = activity[-ACTIVITY_LIMIT:]
        if not isinstance(card.get("attachments"), list):
            card["attachments"] = []
        if not isinstance(card.get("needs"), list):
            card["needs"] = []

    # Ensure sequence numbers exist per project and counters are coherent.
    cards_by_project: dict[str, list[dict]] = {p.get("id"): [] for p in projects if p.get("id")}
    for card in cards:
        pid = card.get("project_id")
        if pid in cards_by_project:
            cards_by_project[pid].append(card)

    for project in projects:
        project_id = project.get("id")
        project_cards = cards_by_project.get(project_id, [])
        seen: set[int] = set()
        resequence = False
        for card in project_cards:
            seq = card.get("seq")
            if not isinstance(seq, int) or seq < 1 or seq in seen:
                resequence = True
                break
            seen.add(seq)
        if resequence:
            for seq, card in enumerate(_sort_cards_for_seq(project_cards), start=1):
                card["seq"] = seq
            required_next = len(project_cards) + 1
        else:
            required_next = (max(seen) + 1) if seen else 1
        current_next = project.get("next_seq")
        if not isinstance(current_next, int) or current_next < required_next:
            project["next_seq"] = required_next


def load_board() -> dict:
    if not BOARD_PATH.exists():
        raise SystemExit(f"Missing board file: {BOARD_PATH}")
    board = read_json(BOARD_PATH, {})
    _migrate_board(board)
    return board


def save_board(board: dict) -> None:
    board["updated_at"] = utc_now()
    write_json(BOARD_PATH, board)
    render_markdown(board)


def column_title(board: dict, col_id: str) -> str:
    for c in board.get("columns", []):
        if c["id"] == col_id:
            return c["title"]
    return col_id


def project_title(board: dict, project_id: str) -> str:
    for p in board.get("projects", []):
        if p["id"] == project_id:
            return p["title"]
    return project_id


def project_key(board: dict, project_id: str) -> str:
    for p in board.get("projects", []):
        if p["id"] == project_id:
            key = p.get("key")
            return key if isinstance(key, str) else project_id.upper()
    return project_id.upper()


def card_key(board: dict, card: dict) -> str:
    seq = card.get("seq")
    if isinstance(seq, int):
        return f"{project_key(board, card.get('project_id', '?'))}-{seq}"
    return f"{project_key(board, card.get('project_id', '?'))}-?"


def attachment_url(card_id: str, attachment_id: str) -> str:
    return f"{ATTACHMENT_URL_BASE}/{card_id}/{attachment_id}"


def attachments_root() -> Path:
    return BOARD_DIR / "attachments"


def _storage_name(attachment_id: str, name: str) -> str:
    return f"{attachment_id}-{name}"


def attachment_path(card_id: str, attachment_id: str, name: str) -> Path:
    return attachments_root() / card_id / _storage_name(attachment_id, name)


def sanitise_attachment_name(raw_name: object) -> str:
    if not isinstance(raw_name, str):
        raise BoardValidationError("attachment name must be a string")
    base = os.path.basename(raw_name.strip())
    cleaned = "".join(ch for ch in base if ch in ATTACHMENT_NAME_ALLOWED).strip()
    if not cleaned:
        cleaned = "file"
    if len(cleaned) > 120:
        stem, suffix = os.path.splitext(cleaned)
        if suffix and len(suffix) < 120:
            cleaned = f"{stem[:120 - len(suffix)]}{suffix}"
        else:
            cleaned = cleaned[:120]
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "file"
    return cleaned


def _validate_attachment_note(note: object) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise BoardValidationError("attachment note must be a string")
    if len(note) > 4000:
        raise BoardValidationError("attachment note must be 4000 characters or fewer")
    return note


def _guess_attachment_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".md", ".markdown", ".txt")):
        return "text/plain"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _validate_need_text(text: object) -> str:
    if not isinstance(text, str):
        raise BoardValidationError("need text must be a string")
    cleaned = text.strip()
    if not (1 <= len(cleaned) <= NEED_TEXT_MAX_LEN):
        raise BoardValidationError("need text must be 1 to 4000 characters")
    return cleaned


def _validate_need_status(status: object) -> str:
    if not isinstance(status, str) or status not in NEED_STATUS:
        raise BoardValidationError("need status must be one of: open, answered, resolved")
    return status


def _validate_need_answer(answer: object) -> str:
    if not isinstance(answer, str):
        raise BoardValidationError("need answer must be a string")
    cleaned = answer.strip()
    if not (1 <= len(cleaned) <= NEED_TEXT_MAX_LEN):
        raise BoardValidationError("need answer must be 1 to 4000 characters")
    return cleaned


def find_card(board: dict, card_id: str) -> dict | None:
    ref = (card_id or "").strip()
    if not ref:
        return None
    for card in board.get("cards", []):
        if card["id"] == ref:
            return card
    key_ref = ref.upper()
    for card in board.get("cards", []):
        if card_key(board, card).upper() == key_ref:
            return card
    return None


def _find_attachment(card: dict, attachment_id: str) -> dict | None:
    for attachment in card.get("attachments", []):
        if attachment.get("id") == attachment_id:
            return attachment
    return None


def _find_need(card: dict, need_id: str) -> dict | None:
    for need in card.get("needs", []):
        if need.get("id") == need_id:
            return need
    return None


def machine_ids(board: dict) -> list[str]:
    return [m["id"] for m in board.get("machines", [])]


def check_machine(board: dict, machine: str | None) -> None:
    if machine and machine not in machine_ids(board):
        raise SystemExit(f"Unknown machine '{machine}'. Known: {', '.join(machine_ids(board))}")


def _project_ids(board: dict) -> set[str]:
    return {project["id"] for project in board.get("projects", [])}


def _column_ids(board: dict) -> set[str]:
    return {column["id"] for column in board.get("columns", [])}


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise BoardValidationError("title must be a string")
    cleaned = title.strip()
    if not cleaned:
        raise BoardValidationError("title must be non-empty")
    if len(cleaned) > 200:
        raise BoardValidationError("title must be 200 characters or fewer")
    return cleaned


def _validate_notes(notes: object) -> str:
    if not isinstance(notes, str):
        raise BoardValidationError("notes must be a string")
    if len(notes) > 4000:
        raise BoardValidationError("notes must be 4000 characters or fewer")
    return notes


def _validate_project(board: dict, project_id: object) -> str:
    if not isinstance(project_id, str) or not project_id:
        raise BoardValidationError("project_id must be a non-empty string")
    if project_id not in _project_ids(board):
        raise BoardValidationError(f"unknown project_id '{project_id}'")
    return project_id


def _validate_column(board: dict, column: object) -> str:
    if not isinstance(column, str) or not column:
        raise BoardValidationError("column must be a non-empty string")
    if column not in _column_ids(board):
        raise BoardValidationError(f"unknown column '{column}'")
    return column


def _validate_machine(board: dict, machine: object) -> str | None:
    if machine is None:
        return None
    if not isinstance(machine, str):
        raise BoardValidationError("machine must be null or a machine id")
    if machine not in machine_ids(board):
        raise BoardValidationError(f"unknown machine '{machine}'")
    return machine


def _validate_owner_agent(owner_agent: object) -> str | None:
    if owner_agent is None:
        return None
    if not isinstance(owner_agent, str):
        raise BoardValidationError("owner_agent must be a string")
    return owner_agent


def _validate_priority(priority: object) -> str:
    if priority is None:
        return "medium"
    if not isinstance(priority, str):
        raise BoardValidationError("priority must be a string")
    return priority


def _validate_subproject(subproject: object) -> str | None:
    if subproject is None:
        return None
    if not isinstance(subproject, str):
        raise BoardValidationError("subproject must be a string or null")
    return subproject


def _validate_project_key(key: object) -> str:
    if not isinstance(key, str):
        raise BoardValidationError("project key must be a string")
    cleaned = key.strip().upper()
    if not PROJECT_KEY_RE.fullmatch(cleaned):
        raise BoardValidationError("project key must match ^[A-Z][A-Z0-9]{1,5}$")
    return cleaned


PROJECT_STATUSES = ("active", "paused", "archived")
PROJECT_PRIORITIES = ("low", "medium", "high", "urgent")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def _apply_project_fields(board: dict, proj: dict, fields: dict) -> None:
    projects = board.get("projects", [])
    if "title" in fields:
        proj["title"] = _validate_title(fields["title"])
    if "key" in fields:
        key = _validate_project_key(fields["key"])
        if any(o is not proj and o.get("key") == key for o in projects):
            raise BoardValidationError(f"project key already exists: {key}")
        proj["key"] = key
    if "path" in fields:
        path = fields["path"]
        if path is not None and not isinstance(path, str):
            raise BoardValidationError("path must be a string or null")
        proj["path"] = (path or "").strip() or None
    if "machine" in fields:
        proj["machine"] = _validate_machine(board, fields["machine"] or None)
    if "parent_id" in fields:
        parent = fields["parent_id"] or None
        if parent is not None:
            _validate_project(board, parent)
            cursor = parent
            index = {p["id"]: p for p in projects}
            while cursor:
                if cursor == proj["id"]:
                    raise BoardValidationError("a project cannot be its own ancestor")
                cursor = (index.get(cursor) or {}).get("parent_id")
        proj["parent_id"] = parent
    if "herdr_workspaces" in fields:
        labels = fields["herdr_workspaces"] or []
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise BoardValidationError("herdr_workspaces must be a list of strings")
        proj["herdr_workspaces"] = [x.strip() for x in labels if x.strip()]
    if "description" in fields:
        desc = fields["description"] or ""
        if not isinstance(desc, str) or len(desc) > 20000:
            raise BoardValidationError("description must be a string of 20000 characters or fewer")
        proj["description"] = desc
    if "status" in fields:
        if fields["status"] not in PROJECT_STATUSES:
            raise BoardValidationError(f"status must be one of {', '.join(PROJECT_STATUSES)}")
        proj["status"] = fields["status"]
    if "priority" in fields:
        if fields["priority"] not in PROJECT_PRIORITIES:
            raise BoardValidationError(f"priority must be one of {', '.join(PROJECT_PRIORITIES)}")
        proj["priority"] = fields["priority"]


PROJECT_FIELDS = {"title", "key", "path", "machine", "parent_id", "herdr_workspaces", "description", "status", "priority"}


def create_project(fields: dict) -> dict:
    unknown = set(fields) - PROJECT_FIELDS - {"id"}
    if unknown:
        raise BoardValidationError(f"unknown project fields: {', '.join(sorted(unknown))}")
    project_id = fields.get("id")
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise BoardValidationError("id must be lowercase letters, digits and dashes (max 48)")
    if "title" not in fields:
        raise BoardValidationError("title is required")
    with board_lock():
        board = load_board()
        if project_id in _project_ids(board):
            raise BoardValidationError(f"project id already exists: {project_id}")
        used = {p.get("key") for p in board.get("projects", []) if isinstance(p.get("key"), str)}
        proj = {"id": project_id, "key": None, "next_seq": 1, "title": "", "path": None, "machine": None,
                "parent_id": None, "herdr_workspaces": [], "description": "", "status": "active", "priority": "medium"}
        rest = {k: v for k, v in fields.items() if k != "id"}
        if not rest.get("key"):
            rest.pop("key", None)
            proj["key"] = _next_unique_key(_project_key_base(project_id), used)
        board.setdefault("projects", []).append(proj)
        try:
            _apply_project_fields(board, proj, rest)
        except BoardValidationError:
            board["projects"].remove(proj)
            raise
        save_board(board)
    return proj


def update_project(project_id: str, fields: dict) -> dict:
    unknown = set(fields) - PROJECT_FIELDS
    if unknown:
        raise BoardValidationError(f"unknown project fields: {', '.join(sorted(unknown))}")
    with board_lock():
        board = load_board()
        proj = next((p for p in board.get("projects", []) if p["id"] == project_id), None)
        if not proj:
            raise CardNotFoundError("project not found")
        _apply_project_fields(board, proj, fields)
        save_board(board)
    return proj


PROJECT_DELETE_MODES = ("empty", "move", "all")


def _project_subtree(board: dict, project_id: str) -> set[str]:
    ids = {project_id}
    grew = True
    while grew:
        grew = False
        for p in board.get("projects", []):
            if p.get("parent_id") in ids and p["id"] not in ids:
                ids.add(p["id"])
                grew = True
    return ids


def delete_project(project_id: str, mode: str = "empty", move_to: str | None = None, agent: str = OWNER) -> dict:
    """Delete a project.

    empty: refuse while it has cards or sub-projects.
    move:  its cards and direct sub-projects go to move_to first (cards get new keys there).
    all:   delete it, every sub-project below it and all their cards and attachments.
    """
    if mode not in PROJECT_DELETE_MODES:
        raise BoardValidationError(f"mode must be one of {', '.join(PROJECT_DELETE_MODES)}")
    with board_lock():
        board = load_board()
        projects = board.get("projects", [])
        cards = board.get("cards", [])
        proj = next((p for p in projects if p["id"] == project_id), None)
        if not proj:
            raise CardNotFoundError("project not found")
        own_cards = [c for c in cards if c.get("project_id") == project_id]
        kids = [p for p in projects if p.get("parent_id") == project_id]
        summary = {"deleted_projects": [project_id], "moved_cards": 0, "moved_projects": 0, "deleted_cards": 0}
        if mode == "empty":
            if own_cards:
                raise BoardValidationError("project still has cards; move or delete them first")
            if kids:
                raise BoardValidationError("project still has sub-projects")
        elif mode == "move":
            if not move_to:
                raise BoardValidationError("move needs a target project")
            if move_to in _project_subtree(board, project_id):
                raise BoardValidationError("target must not be the project itself or one of its sub-projects")
            target = _project_index(board).get(move_to)
            if not target:
                raise BoardValidationError(f"unknown target project '{move_to}'")
            now = utc_now()
            for card in sorted(own_cards, key=lambda c: c.get("seq") or 0):
                card["project_id"] = move_to
                card["seq"] = _next_project_seq(target)
                card["updated_at"] = now
                _append_activity(card, {"at": now, "agent": agent, "type": "updated", "fields": ["project_id"],
                                        "text": f"Moved from deleted project {proj.get('title') or project_id}"})
            for kid in kids:
                kid["parent_id"] = move_to
            summary["moved_cards"] = len(own_cards)
            summary["moved_projects"] = len(kids)
        else:
            doomed = _project_subtree(board, project_id)
            gone = [c for c in cards if c.get("project_id") in doomed]
            for card in gone:
                shutil.rmtree(attachments_root() / card["id"], ignore_errors=True)
            cards[:] = [c for c in cards if c.get("project_id") not in doomed]
            projects[:] = [p for p in projects if p["id"] not in doomed]
            summary["deleted_projects"] = sorted(doomed)
            summary["deleted_cards"] = len(gone)
            _prune_workspaces(board, doomed)
            save_board(board)
            return summary
        projects.remove(proj)
        _prune_workspaces(board, {project_id})
        save_board(board)
    return summary


WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
WORKSPACE_MAX = 30


def _prune_workspaces(board: dict, gone: set) -> None:
    for ws in board.get("workspaces") or []:
        ws["project_ids"] = [pid for pid in ws.get("project_ids", []) if pid not in gone]


def set_workspaces(items: object) -> list[dict]:
    """Replace the dashboard workspaces: named groups of projects (sub-projects follow their parent)."""
    if not isinstance(items, list) or len(items) > WORKSPACE_MAX:
        raise BoardValidationError(f"workspaces must be a list of at most {WORKSPACE_MAX}")
    with board_lock():
        board = load_board()
        known = _project_ids(board)
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                raise BoardValidationError("each workspace must be an object")
            title = item.get("title")
            if not isinstance(title, str) or not (1 <= len(title.strip()) <= 60):
                raise BoardValidationError("workspace title must be 1 to 60 characters")
            wid = item.get("id") or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
            if not isinstance(wid, str) or not WORKSPACE_ID_RE.fullmatch(wid):
                raise BoardValidationError(f"invalid workspace id '{wid}'")
            if any(w["id"] == wid for w in out):
                raise BoardValidationError(f"duplicate workspace id '{wid}'")
            pids = item.get("project_ids", [])
            if not isinstance(pids, list) or not all(isinstance(p, str) for p in pids):
                raise BoardValidationError("project_ids must be a list of project ids")
            unknown = sorted(set(pids) - known)
            if unknown:
                raise BoardValidationError(f"unknown project(s): {', '.join(unknown)}")
            out.append({"id": wid, "title": title.strip(), "project_ids": list(dict.fromkeys(pids))})
        board["workspaces"] = out
        save_board(board)
    return out


def _validate_comment_text(text: object) -> str:
    if not isinstance(text, str):
        raise BoardValidationError("comment text must be a string")
    if not (1 <= len(text) <= COMMENT_MAX_LEN):
        raise BoardValidationError("comment text must be 1 to 4000 characters")
    return text


def _ensure_update_fields(fields: dict) -> None:
    unknown = sorted(set(fields) - CARD_UPDATE_FIELDS)
    if unknown:
        raise BoardValidationError(f"unknown update field(s): {', '.join(unknown)}")


def _project_index(board: dict) -> dict[str, dict]:
    return {project["id"]: project for project in board.get("projects", [])}


def _next_project_seq(project: dict) -> int:
    current = project.get("next_seq")
    if not isinstance(current, int) or current < 1:
        current = 1
    project["next_seq"] = current + 1
    return current


def _build_card_for_create(board: dict, fields: dict, agent: str, now: str) -> dict:
    unknown = sorted(set(fields) - CARD_CREATE_FIELDS)
    if unknown:
        raise BoardValidationError(f"unknown create field(s): {', '.join(unknown)}")

    card_id = fields.get("id")
    if card_id is not None and (not isinstance(card_id, str) or not card_id):
        raise BoardValidationError("id must be a non-empty string")

    project_id = _validate_project(board, fields.get("project_id"))
    project = _project_index(board).get(project_id)
    if not project:
        raise BoardValidationError(f"unknown project_id '{project_id}'")

    card = {
        "id": card_id or str(uuid.uuid4())[:8],
        "project_id": project_id,
        "seq": _next_project_seq(project),
        "title": _validate_title(fields.get("title")),
        "column": _validate_column(board, fields.get("column", "backlog")),
        "priority": _validate_priority(fields.get("priority")),
        "owner_agent": _validate_owner_agent(fields.get("owner_agent", agent)),
        "last_agent": agent,
        "machine": _validate_machine(board, fields.get("machine")),
        "subproject": _validate_subproject(fields.get("subproject")),
        "tags": [],
        "links": [],
        "notes": _validate_notes(fields.get("notes", "")),
        "attachments": [],
        "needs": [],
        "created_at": now,
        "updated_at": now,
        "activity": [],
    }
    _capture_wake_target(card, agent, now)
    _append_activity(card, {"at": now, "agent": agent, "type": "created"})
    return card


def _validate_at(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BoardValidationError(f"invalid --at timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_card(fields: dict, agent: str) -> dict:
    fields = dict(fields)
    at = fields.pop("at", None)
    with board_lock():
        board = load_board()
        card = _build_card_for_create(board, fields, agent, _validate_at(at) if at else utc_now())
        if find_card(board, card["id"]):
            raise BoardValidationError(f"card id already exists: {card['id']}")
        board.setdefault("cards", []).append(card)
        save_board(board)
    return card


def update_card(card_id: str, fields: dict, agent: str) -> dict:
    _ensure_update_fields(fields)
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        projects = _project_index(board)
        now = utc_now()
        changed_fields: list[str] = []
        old_column = card.get("column")

        if "title" in fields:
            value = _validate_title(fields["title"])
            if card.get("title") != value:
                card["title"] = value
                changed_fields.append("title")
        if "column" in fields:
            value = _validate_column(board, fields["column"])
            if card.get("column") != value:
                card["column"] = value
                changed_fields.append("column")
        if "notes" in fields:
            value = _validate_notes(fields["notes"])
            if card.get("notes") != value:
                card["notes"] = value
                changed_fields.append("notes")
        if "project_id" in fields:
            value = _validate_project(board, fields["project_id"])
            if card.get("project_id") != value:
                card["project_id"] = value
                card["seq"] = _next_project_seq(projects[value])
                changed_fields.append("project_id")
        if "machine" in fields:
            value = _validate_machine(board, fields["machine"])
            if card.get("machine") != value:
                card["machine"] = value
                changed_fields.append("machine")
        if "subproject" in fields:
            value = _validate_subproject(fields["subproject"])
            if card.get("subproject") != value:
                card["subproject"] = value
                changed_fields.append("subproject")
        if "priority" in fields:
            value = _validate_priority(fields["priority"])
            if card.get("priority") != value:
                card["priority"] = value
                changed_fields.append("priority")
        if "owner_agent" in fields:
            value = _validate_owner_agent(fields["owner_agent"])
            if card.get("owner_agent") != value:
                card["owner_agent"] = value
                changed_fields.append("owner_agent")

        if old_column != card.get("column"):
            _append_activity(
                card,
                {
                    "at": now,
                    "agent": agent,
                    "type": "moved",
                    "from": old_column,
                    "to": card.get("column"),
                },
            )
        update_fields = [field for field in changed_fields if field != "column"]
        if update_fields:
            _append_activity(card, {"at": now, "agent": agent, "type": "updated", "fields": update_fields})
        _capture_wake_target(card, agent, now)
        card["last_agent"] = agent
        card["updated_at"] = now
        save_board(board)
        updated = dict(card)
    return updated


def add_comment(card_id: str, text: str, agent: str) -> dict:
    comment = _validate_comment_text(text)
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        now = utc_now()
        _append_activity(card, {"at": now, "agent": agent, "type": "comment", "text": comment})
        _capture_wake_target(card, agent, now)
        card["last_agent"] = agent
        card["updated_at"] = now
        save_board(board)
        updated = dict(card)
    return updated


def add_comment_and_wake(card_id: str, text: str, agent: str, wake: bool = True) -> tuple[dict, dict | None]:
    card = add_comment(card_id, text, agent)
    wake_entry = None
    if wake and is_owner(agent):
        wake_entry = enqueue_wake(card, "comment", text)
    return card, wake_entry


def add_attachment(
    card_id: str,
    src_path: str | Path,
    agent: str,
    note: str = "",
    name: str | None = None,
) -> tuple[dict, dict]:
    source = Path(src_path).expanduser()
    if not source.exists() or not source.is_file():
        raise BoardValidationError(f"attachment source does not exist: {source}")
    size = source.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise BoardValidationError("attachment exceeds 25 MB limit")
    note_value = _validate_attachment_note(note)
    safe_name = sanitise_attachment_name(name if name is not None else source.name)
    attachment_type = _guess_attachment_type(safe_name)

    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        now = utc_now()

        attachment_id = uuid.uuid4().hex[:8]
        while _find_attachment(card, attachment_id):
            attachment_id = uuid.uuid4().hex[:8]

        card_dir = attachments_root() / card["id"]
        card_dir.mkdir(parents=True, exist_ok=True)
        target = attachment_path(card["id"], attachment_id, safe_name)
        shutil.copy2(source, target)

        attachment = {
            "id": attachment_id,
            "name": safe_name,
            "size": size,
            "type": attachment_type,
            "added_at": now,
            "agent": agent,
            "note": note_value,
        }
        try:
            card.setdefault("attachments", []).append(attachment)
            _append_activity(
                card,
                {
                    "at": now,
                    "agent": agent,
                    "type": "attachment",
                    "attachment_id": attachment_id,
                    "name": safe_name,
                },
            )
            _capture_wake_target(card, agent, now)
            card["last_agent"] = agent
            card["updated_at"] = now
            save_board(board)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return dict(card), dict(attachment)


def remove_attachment(card_id: str, attachment_id: str, agent: str) -> tuple[dict, dict]:
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        existing = _find_attachment(card, attachment_id)
        if not existing:
            raise AttachmentNotFoundError("attachment not found")

        card["attachments"] = [a for a in card.get("attachments", []) if a.get("id") != attachment_id]
        target = attachment_path(card["id"], existing["id"], existing["name"])
        target.unlink(missing_ok=True)
        card_dir = target.parent
        if card_dir.exists() and not any(card_dir.iterdir()):
            card_dir.rmdir()

        now = utc_now()
        _append_activity(
            card,
            {
                "at": now,
                "agent": agent,
                "type": "attachment_removed",
                "name": existing["name"],
            },
        )
        _capture_wake_target(card, agent, now)
        card["last_agent"] = agent
        card["updated_at"] = now
        save_board(board)
        return dict(card), dict(existing)


def add_need(card_id: str, text: str, agent: str) -> tuple[dict, dict]:
    need_text = _validate_need_text(text)
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        now = utc_now()
        need_id = uuid.uuid4().hex[:8]
        while _find_need(card, need_id):
            need_id = uuid.uuid4().hex[:8]
        need = {
            "id": need_id,
            "text": need_text,
            "agent": agent,
            "at": now,
            "status": "open",
            "answer": None,
            "answered_at": None,
            "answered_by": None,
            "resolved_at": None,
        }
        card.setdefault("needs", []).append(need)
        _append_activity(card, {"at": now, "agent": agent, "type": "need", "need_id": need_id, "text": need_text})
        _capture_wake_target(card, agent, now)
        card["last_agent"] = agent
        card["updated_at"] = now
        save_board(board)
        return dict(card), dict(need)


def answer_need(card_id: str, need_id: str, answer: str, by: str) -> tuple[dict, dict]:
    answer_text = _validate_need_answer(answer)
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        need = _find_need(card, need_id)
        if not need:
            raise NeedNotFoundError("need not found")
        now = utc_now()
        need["answer"] = answer_text
        need["status"] = "answered"
        need["answered_at"] = now
        need["answered_by"] = by
        need["resolved_at"] = None
        _append_activity(
            card,
            {"at": now, "agent": by, "type": "need_answered", "need_id": need_id, "answer": answer_text},
        )
        card["last_agent"] = by
        card["updated_at"] = now
        save_board(board)
        return dict(card), dict(need)


def answer_need_and_wake(
    card_id: str,
    need_id: str,
    answer: str,
    by: str,
    wake: bool = True,
) -> tuple[dict, dict, dict | None]:
    card, need = answer_need(card_id, need_id, answer, by)
    wake_entry = None
    if wake and is_owner(by):
        need_text = need.get("text") if isinstance(need.get("text"), str) else ""
        wake_text = f'Answer to your need "{need_text}": {need.get("answer", "")}'
        wake_entry = enqueue_wake(card, "need_answer", wake_text)
    return card, need, wake_entry


def set_need_status(card_id: str, need_id: str, status: str, by: str) -> tuple[dict, dict]:
    next_status = _validate_need_status(status)
    with board_lock():
        board = load_board()
        card = find_card(board, card_id)
        if not card:
            raise CardNotFoundError("card not found")
        need = _find_need(card, need_id)
        if not need:
            raise NeedNotFoundError("need not found")
        now = utc_now()
        need["status"] = next_status
        if next_status == "open":
            need["resolved_at"] = None
        elif next_status == "answered":
            if not isinstance(need.get("answer"), str) or not need.get("answer"):
                raise BoardValidationError("cannot mark need answered without an answer")
            if not need.get("answered_at"):
                need["answered_at"] = now
            if not need.get("answered_by"):
                need["answered_by"] = by
            need["resolved_at"] = None
            _append_activity(
                card,
                {
                    "at": now,
                    "agent": by,
                    "type": "need_answered",
                    "need_id": need_id,
                    "answer": need.get("answer", ""),
                },
            )
        elif next_status == "resolved":
            need["resolved_at"] = now
            _append_activity(card, {"at": now, "agent": by, "type": "need_resolved", "need_id": need_id})

        _capture_wake_target(card, by, now)
        card["last_agent"] = by
        card["updated_at"] = now
        save_board(board)
        return dict(card), dict(need)


def card_attachment(card_id: str, attachment_id: str) -> tuple[dict, dict, Path] | None:
    board = load_board()
    card = find_card(board, card_id)
    if not card:
        return None
    attachment = _find_attachment(card, attachment_id)
    if not attachment:
        return None
    path = attachment_path(card["id"], attachment["id"], attachment["name"])
    if not path.exists():
        return None
    return dict(card), dict(attachment), path


def delete_card(card_id: str) -> dict:
    with board_lock():
        board = load_board()
        cards = board.get("cards", [])
        card = find_card(board, card_id)
        if card:
            cards[:] = [existing for existing in cards if existing.get("id") != card.get("id")]
            shutil.rmtree(attachments_root() / card["id"], ignore_errors=True)
            save_board(board)
            return dict(card)
    raise CardNotFoundError("card not found")


def iter_needs(board: dict, include_resolved: bool = False, asked_by: str | None = None) -> list[tuple[dict, dict]]:
    items: list[tuple[dict, dict]] = []
    for card in board.get("cards", []):
        for need in card.get("needs", []):
            status = need.get("status")
            if status == "resolved" and not include_resolved:
                continue
            if asked_by and need.get("agent") != asked_by:
                continue
            if status not in NEED_STATUS:
                continue
            items.append((card, need))
    items.sort(key=lambda item: item[1].get("at", ""), reverse=True)
    return items


def render_machine(lines: list[str], machine: dict, snap: dict | None) -> None:
    lines.append(f"### {machine['title']} (`{machine['id']}`)")
    if not snap:
        lines.append("- _never synced_")
        lines.append("")
        return
    if snap.get("error"):
        lines.append(f"- **Sync failed {snap.get('failed_at')}:** {snap['error']}")
        if not snap.get("data"):
            lines.append("")
            return
        lines.append(f"- Showing last good snapshot from {snap.get('synced_at')}")
    data = snap.get("data") or {}
    lines.append(f"- Synced {snap.get('synced_at')} · load {data.get('load')}")

    herdr = data.get("herdr_agents") or []
    if herdr:
        working = [a for a in herdr if a.get("status") == "working"]
        lines.append(f"- **herdr agents:** {len(herdr)} panes, {len(working)} working")
        for a in sorted(herdr, key=lambda x: (x.get("status") != "working", x.get("workspace") or "")):
            lines.append(
                f"  - [{a.get('status')}] {a.get('workspace')} · {a.get('agent')} · "
                f"{a.get('title')} · `{a.get('cwd')}`"
            )
    agents = data.get("agents") or []
    if agents:
        summary = ", ".join(
            f"{g['agent']} ×{g['count']} in `{g['cwd']}`" for g in agents if g["agent"] != "herdr"
        )
        lines.append(f"- **Agent processes:** {summary}")
    servers = data.get("dev_servers") or []
    if servers:
        cwds = sorted({s["cwd"] for s in servers if s.get("cwd")})
        lines.append(f"- **Dev servers:** {', '.join(f'`{c}`' for c in cwds)}")
    jobs = data.get("launchd") or []
    failing = [j for j in jobs if j.get("last_exit") not in (0, None) and not j.get("running")]
    if jobs:
        lines.append(
            f"- **launchd jobs:** {len(jobs)} ({sum(j['running'] for j in jobs)} running, "
            f"{len(failing)} last run failed)"
        )
        for j in failing:
            lines.append(f"  - FAILED: `{j['label']}` exit {j['last_exit']}")
    tasks = data.get("hermes_tasks") or []
    if tasks:
        lines.append(f"- **hermes kanban:** {len(tasks)} open")
        for t in tasks:
            lines.append(f"  - [{t['status']}] {t['title']} · {t.get('assignee') or '—'}")
    recent = data.get("recent_projects") or []
    if recent:
        lines.append("- **Repos touched in last 48h:**")
        for p in recent:
            git = ""
            if p.get("branch"):
                git = f" · `{p['branch']}` · {p.get('dirty_files', 0)} uncommitted · {p.get('last_commit')}"
            lines.append(f"  - `{p['path']}` ({p['modified']}){git}")
    lines.append("")


def render_markdown(board: dict) -> None:
    snapshots = read_json(SNAPSHOTS_PATH, {})
    open_needs = [(card, need) for card, need in iter_needs(board) if need.get("status") == "open"]
    lines: list[str] = [
        "# Workboard",
        "",
        f"_Cards updated: {board.get('updated_at', '?')}_",
        "",
    ]

    lines.append("## Needs you")
    lines.append("")
    if open_needs:
        for card, need in open_needs:
            lines.append(
                f"- **{card_key(board, card)}** {card.get('title', '')} · {need.get('agent', '—')}: {need.get('text', '')}"
            )
    else:
        lines.append("- _No open needs._")
    lines.append("")

    if board.get("machines"):
        lines.append("## Machines (live)")
        lines.append("")
        for machine in board["machines"]:
            render_machine(lines, machine, snapshots.get(machine["id"]))

    projects = board.get("projects", [])
    roots = [p for p in projects if not p.get("parent_id")]
    children_map: dict[str, list[dict]] = {}
    for p in projects:
        pid = p.get("parent_id")
        if pid:
            children_map.setdefault(pid, []).append(p)

    def project_block(proj: dict, depth: int = 2) -> None:
        where = f" · {proj['machine']}" if proj.get("machine") else ""
        lines.append(f"{'#' * depth} {proj['title']} (`{proj['id']}`){where}")
        if proj.get("path"):
            lines.append(f"_Path: `{proj['path']}`_")
        lines.append("")
        proj_cards = [c for c in board.get("cards", []) if c.get("project_id") == proj["id"]]
        by_col: dict[str, list[dict]] = {c["id"]: [] for c in board.get("columns", [])}
        for card in proj_cards:
            by_col.setdefault(card.get("column", "backlog"), []).append(card)
        for col in board.get("columns", []):
            cards = by_col.get(col["id"], [])
            if not cards:
                continue
            lines.append(f"**{col['title']}**")
            for card in sorted(cards, key=lambda x: x.get("updated_at", ""), reverse=True):
                agent = card.get("owner_agent") or card.get("last_agent") or "—"
                extras = "".join(
                    f" · {label}: {card[key]}"
                    for key, label in (("machine", "on"), ("subproject", "sub"))
                    if card.get(key)
                )
                lines.append(
                    f"- **{card_key(board, card)} · {card['title']}** (`{card['id']}`) · {agent}{extras}"
                )
                if card.get("notes"):
                    lines.append(f"  - {card['notes']}")
            lines.append("")

        for child in children_map.get(proj["id"], []):
            project_block(child, min(depth + 1, 6))

    lines.append("## Projects")
    lines.append("")
    if not roots:
        lines.append("_No projects defined._")
    for root in roots:
        project_block(root, 3)

    lines.append("---")
    lines.append(
        "Refresh: `workboard sync` "
        "(machines) · `render` (cards only)"
    )
    BOARD_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect(machine: dict) -> dict:
    if machine.get("ssh"):
        cmd = ["ssh", *SSH_OPTS, machine["ssh"], "python3", "-"]
    else:
        cmd = [sys.executable, "-"]
    with COLLECTOR.open() as src:
        result = subprocess.run(
            cmd, stdin=src, capture_output=True, text=True, timeout=90, check=False
        )
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise RuntimeError(f"exit {result.returncode}: {detail}")
    return json.loads(result.stdout)


def cmd_sync(args: argparse.Namespace) -> int:
    board = load_board()
    targets = [m for m in board.get("machines", []) if not args.machine or m["id"] == args.machine]
    results: dict[str, dict] = {}
    for machine in targets:
        try:
            results[machine["id"]] = {"data": collect(machine), "synced_at": utc_now()}
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            results[machine["id"]] = {"error": str(exc), "failed_at": utc_now()}

    with board_lock():
        snapshots = read_json(SNAPSHOTS_PATH, {})
        for mid, res in results.items():
            if "error" in res:
                previous = snapshots.get(mid, {})
                snapshots[mid] = {
                    **previous,
                    "error": res["error"],
                    "failed_at": res["failed_at"],
                }
            else:
                snapshots[mid] = res
        write_json(SNAPSHOTS_PATH, snapshots)
        render_markdown(load_board())

    failed = 0
    for mid, res in results.items():
        if "error" in res:
            failed += 1
            print(f"{mid}: FAILED {res['error']}")
        else:
            d = res["data"]
            print(
                f"{mid}: ok · {len(d.get('herdr_agents') or [])} herdr agents · "
                f"{len(d.get('dev_servers') or [])} dev servers · "
                f"{len(d.get('recent_projects') or [])} recent repos"
            )
    if not args.machine and debt_is_stale():
        try:
            store = scan_debt()
            print(f"debt: rescanned {len(store['projects'])} projects" + (f" · errors: {store['errors']}" if store["errors"] else ""))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"debt: FAILED {exc}")
    return 1 if failed else 0


def debt_path() -> Path:
    return BOARD_DIR / "debt.json"


def debt_is_stale(max_age_seconds: int = DEBT_MAX_AGE_SECONDS) -> bool:
    scanned = read_json(debt_path(), {}).get("scanned_at")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(scanned).replace("Z", "+00:00"))
    except ValueError:
        return True
    return age.total_seconds() > max_age_seconds


def run_debt_scan(machine: dict, targets: list[dict]) -> dict:
    arg = base64.b64encode(json.dumps(targets).encode("utf-8")).decode("ascii")
    if machine.get("ssh"):
        cmd = ["ssh", *SSH_OPTS, machine["ssh"], "python3", "-", arg]
    else:
        cmd = [sys.executable, "-", arg]
    with DEBT_SCANNER.open() as src:
        result = subprocess.run(cmd, stdin=src, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise RuntimeError(f"exit {result.returncode}: {detail}")
    return json.loads(result.stdout)


def merge_debt(board: dict, results: dict, errors: dict) -> dict:
    """Fold scan results into debt.json, keeping one history point per project per day."""
    with board_lock():
        store = read_json(debt_path(), {})
        projects = store.get("projects") if isinstance(store.get("projects"), dict) else {}
        history = store.get("history") if isinstance(store.get("history"), dict) else {}
        today = utc_now()[:10]
        for pid, res in results.items():
            projects[pid] = res
            if isinstance(res, dict) and res.get("grade"):
                points = [e for e in history.get(pid, []) if e.get("date") != today]
                points.append({k: res.get(k) for k in ("minutes", "loc", "per_kloc", "grade")} | {"date": today})
                history[pid] = points[-DEBT_HISTORY_DAYS:]
        known = {p.get("id") for p in board.get("projects", [])}
        store = {
            "scanned_at": utc_now(),
            "projects": {k: v for k, v in projects.items() if k in known},
            "history": {k: v for k, v in history.items() if k in known},
            "errors": errors,
        }
        write_json(debt_path(), store)
    return store


def scan_debt(project_ids: set | None = None) -> dict:
    board = load_board()
    results: dict = {}
    errors: dict = {}
    for machine in board.get("machines", []):
        targets = [
            {"id": p["id"], "path": p["path"]}
            for p in board.get("projects", [])
            if p.get("machine") == machine["id"] and p.get("path") and (project_ids is None or p["id"] in project_ids)
        ]
        if not targets:
            continue
        try:
            results.update(run_debt_scan(machine, targets))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            errors[machine["id"]] = str(exc)
    return merge_debt(board, results, errors)


def cmd_debt(args: argparse.Namespace) -> int:
    board = load_board()
    ids = None
    if args.project:
        ref = args.project.strip().lower()
        project = next((p for p in board.get("projects", []) if p.get("id") == ref or str(p.get("key", "")).lower() == ref), None)
        if not project:
            print(f"project not found: {args.project}", file=sys.stderr)
            return 1
        ids = {project["id"]}
    store = scan_debt(ids) if args.rescan or ids or debt_is_stale() else read_json(debt_path(), {})
    rows = [(pid, r) for pid, r in store.get("projects", {}).items() if r.get("grade") and (ids is None or pid in ids)]
    if args.json:
        print(json.dumps({pid: r for pid, r in rows}, indent=2))
        return 0
    rows.sort(key=lambda x: -x[1]["per_kloc"])
    print(f"{'project':<28} grade  debt   per kLOC     LOC  biggest cost")
    for pid, r in rows:
        kinds = r.get("minutes_by_kind") or {}
        top = max(kinds, key=kinds.get) if kinds and max(kinds.values()) else "-"
        print(f"{pid:<28} {r['grade']:^5} {r['minutes'] / 60:5.1f}h {r['per_kloc']:9.1f} {r['loc']:7d}  {top.replace('_', ' ')}")
    for mid, err in (store.get("errors") or {}).items():
        print(f"{mid}: scan FAILED {err}", file=sys.stderr)
    return 0


def _lookup_card_or_exit(board: dict, ref: str) -> dict:
    card = find_card(board, ref)
    if not card:
        raise SystemExit(f"Unknown card id: {ref}")
    return card


def _format_wake_target(target: object) -> str:
    if not isinstance(target, dict):
        return "none"
    bits = []
    machine = target.get("machine")
    kind = target.get("kind")
    pane = target.get("pane_id")
    session_id = target.get("session_id")
    workspace_id = target.get("workspace_id")
    terminal_id = target.get("terminal_id")
    agent = target.get("agent")
    if machine:
        bits.append(str(machine))
    if kind:
        bits.append(str(kind))
    if pane:
        bits.append(f"pane {pane}")
    if workspace_id:
        bits.append(f"ws {workspace_id}")
    if terminal_id:
        bits.append(f"term {terminal_id}")
    if session_id:
        bits.append(f"session {session_id}")
    if agent:
        bits.append(f"agent {agent}")
    cwd = target.get("cwd")
    if cwd:
        bits.append(f"cwd {cwd}")
    at = target.get("at")
    if at:
        bits.append(f"at {at}")
    return " · ".join(bits) if bits else "none"


def cmd_show(args: argparse.Namespace) -> int:
    board = load_board()
    cards = board.get("cards", [])
    if args.project:
        cards = [c for c in cards if c.get("project_id") == args.project]
    if args.machine:
        cards = [c for c in cards if c.get("machine") == args.machine]
    if args.agent:
        needle = args.agent.lower()
        cards = [
            c
            for c in cards
            if needle in (c.get("owner_agent") or "").lower()
            or needle in (c.get("last_agent") or "").lower()
        ]
    if args.column:
        cards = [c for c in cards if c.get("column") == args.column]
    if args.active_only:
        cards = [c for c in cards if c.get("column") not in ("done", "backlog")]

    print(f"Workboard · updated {board.get('updated_at')}\n")
    for card in sorted(cards, key=lambda x: (x.get("project_id"), x.get("column"), x.get("title"))):
        proj = project_title(board, card.get("project_id", "?"))
        col = column_title(board, card.get("column", "backlog"))
        agent = card.get("owner_agent") or "—"
        where = f" · on {card['machine']}" if card.get("machine") else ""
        print(f"[{col}] {card_key(board, card)} · {proj} · {card['title']}")
        print(f"  id: {card['id']} · agent: {agent}{where}")
        if card.get("subproject"):
            print(f"  subproject: {card['subproject']}")
        if card.get("notes"):
            print(f"  notes: {card['notes']}")
        attachments = card.get("attachments") or []
        if attachments:
            print("  attachments:")
            for attachment in attachments:
                print(
                    f"  - {attachment.get('name')} · "
                    f"{attachment_url(card['id'], attachment.get('id', ''))}"
                )
        pending_needs = [n for n in card.get("needs", []) if n.get("status") in ("open", "answered")]
        if pending_needs:
            print("  needs:")
            for need in sorted(pending_needs, key=lambda n: n.get("at", ""), reverse=True):
                line = (
                    f"  - [{need.get('status')}] {need.get('id')} · "
                    f"{need.get('agent', '—')}: {need.get('text', '')}"
                )
                if need.get("status") == "answered" and need.get("answer"):
                    line += f" | answer: {need.get('answer')}"
                print(line)
        print()
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    board = load_board()
    card = _lookup_card_or_exit(board, args.id)
    project = next((p for p in board.get("projects", []) if p.get("id") == card.get("project_id")), None)

    print(f"{card_key(board, card)} · {card.get('title', '')}")
    print(f"id: {card.get('id')}")
    print(f"column: {card.get('column')} ({column_title(board, card.get('column', ''))})")
    print(f"priority: {card.get('priority')}")
    print(f"owner: {card.get('owner_agent') or '—'}")
    print(f"last agent: {card.get('last_agent') or '—'}")
    print(f"project: {card.get('project_id')} ({project_title(board, card.get('project_id', ''))})")
    if project and project.get("path"):
        print(f"project path: {project.get('path')}")
    print(f"machine: {card.get('machine') or '—'}")
    print(f"wake target: {_format_wake_target(card.get('wake_target'))}")
    print("")
    print("description:")
    print(card.get("notes") or "")
    print("")

    needs = [n for n in card.get("needs", []) if n.get("status") in {"open", "answered"}]
    print("needs:")
    if not needs:
        print("- none")
    else:
        for need in sorted(needs, key=lambda n: n.get("at", "")):
            line = (
                f"- [{need.get('status')}] {need.get('id')} · {need.get('agent', '—')}: "
                f"{need.get('text', '')}"
            )
            if need.get("answer"):
                line += f" | answer: {need.get('answer')}"
            print(line)

    attachments = card.get("attachments") or []
    print("")
    print("attachments:")
    if not attachments:
        print("- none")
    else:
        for attachment in attachments:
            print(
                f"- {attachment.get('id')} · {attachment.get('name')} · "
                f"{attachment_url(card.get('id', ''), attachment.get('id', ''))}"
            )

    activity = card.get("activity") or []
    print("")
    print("activity (last 15):")
    if not activity:
        print("- none")
    else:
        for event in activity[-15:]:
            kind = event.get("type", "event")
            detail = ""
            if kind == "comment":
                detail = f": {event.get('text', '')}"
            elif kind == "moved":
                detail = f" {event.get('from')} -> {event.get('to')}"
            elif kind == "updated":
                detail = f" fields={','.join(event.get('fields', []))}"
            elif kind == "wake":
                detail = f" {event.get('status')} ({event.get('method')}) {event.get('text', '')}"
            elif kind in {"need", "need_answered", "need_resolved"}:
                detail = f" need={event.get('need_id')}"
            print(f"- {event.get('at')} · {event.get('agent')} · {kind}{detail}")
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    create_fields = {
        "id": args.id,
        "project_id": args.project,
        "title": args.title,
        "column": args.column or "backlog",
        "priority": args.priority or "medium",
        "owner_agent": args.agent,
        "machine": args.machine,
        "subproject": args.subproject,
        "notes": args.notes or "",
        "at": args.at,
    }
    update_fields: dict[str, object] = {
        "project_id": args.project,
        "title": args.title,
        "owner_agent": args.agent,
    }
    if args.column:
        update_fields["column"] = args.column
    if args.priority:
        update_fields["priority"] = args.priority
    if args.machine:
        update_fields["machine"] = args.machine
    if args.subproject is not None:
        update_fields["subproject"] = args.subproject or None
    if args.notes is not None:
        update_fields["notes"] = args.notes

    try:
        if args.id:
            try:
                card = update_card(args.id, update_fields, args.agent)
            except CardNotFoundError:
                card = create_card(create_fields, args.agent)
        else:
            card = create_card(create_fields, args.agent)
    except (BoardValidationError, CardNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
    print(card["id"])
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    board = load_board()
    card = find_card(board, args.id)
    if not card:
        raise SystemExit(f"Unknown card id: {args.id}")
    agent = args.agent or card.get("last_agent") or card.get("owner_agent") or "unknown"
    try:
        update_card(args.id, {"column": args.column}, agent)
        if args.note is not None:
            add_comment(args.id, args.note, agent)
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    except BoardValidationError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"moved {args.id} -> {args.column}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    try:
        delete_card(args.id)
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    print(f"deleted {args.id}")
    return 0


def cmd_touch(args: argparse.Namespace) -> int:
    if args.note is not None:
        board = load_board()
        card = find_card(board, args.id)
        if not card:
            raise SystemExit(f"Unknown card id: {args.id}")
        agent = args.agent or card.get("last_agent") or card.get("owner_agent") or "unknown"
        try:
            add_comment(args.id, args.note, agent)
        except BoardValidationError as exc:
            raise SystemExit(str(exc)) from exc
        return 0

    with board_lock():
        board = load_board()
        card = find_card(board, args.id)
        if not card:
            raise SystemExit(f"Unknown card id: {args.id}")
        now = utc_now()
        if args.agent:
            card["last_agent"] = args.agent
            _capture_wake_target(card, args.agent, now)
        card["updated_at"] = now
        save_board(board)
    return 0


def _convert_source_to_pdf(source: Path) -> tuple[Path, tempfile.TemporaryDirectory[str], str]:
    supported = {".md", ".markdown", ".txt", ".html"}
    suffix = source.suffix.lower()
    if suffix not in supported:
        raise SystemExit("--pdf supports only .md, .markdown, .txt, or .html")
    if not MD2PDF.exists():
        raise SystemExit(f"Missing converter script: {MD2PDF}")
    tempdir = tempfile.TemporaryDirectory(prefix="workboard-attach-")
    output = Path(tempdir.name) / f"{source.stem}.pdf"
    result = subprocess.run(
        [str(MD2PDF), str(source), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        tempdir.cleanup()
        details = (result.stderr or result.stdout).strip() or "md2pdf failed"
        raise SystemExit(details)
    return output, tempdir, f"{source.stem}.pdf"


def cmd_attach(args: argparse.Namespace) -> int:
    source = Path(args.file).expanduser()
    if not source.exists():
        raise SystemExit(f"No such file: {source}")
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    attachment_name = args.name
    attach_source = source
    try:
        if args.pdf:
            attach_source, tempdir, attachment_name = _convert_source_to_pdf(source)
        _, attachment = add_attachment(
            args.id,
            attach_source,
            args.agent,
            note=args.note or "",
            name=attachment_name,
        )
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    except BoardValidationError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if tempdir is not None:
            tempdir.cleanup()
    print(f"{attachment['id']} {attachment_url(args.id, attachment['id'])}")
    return 0


def cmd_detach(args: argparse.Namespace) -> int:
    try:
        _, attachment = remove_attachment(args.id, args.attachment_id, args.agent)
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    except AttachmentNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"removed {attachment['id']}")
    return 0


def cmd_need(args: argparse.Namespace) -> int:
    try:
        _, need = add_need(args.id, args.note, args.agent)
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    except BoardValidationError as exc:
        raise SystemExit(str(exc)) from exc
    print(need["id"])
    return 0


def cmd_needs(args: argparse.Namespace) -> int:
    board = load_board()
    items = iter_needs(board, include_resolved=args.all, asked_by=args.agent)
    for card, need in items:
        line = (
            f"{card_key(board, card)} | {card.get('title', '')} | {need.get('id')} | "
            f"{need.get('status')} | {need.get('text', '')}"
        )
        if need.get("status") == "answered" and need.get("answer"):
            line += f" | answer: {need.get('answer')}"
        print(line)
    return 0


def cmd_need_done(args: argparse.Namespace) -> int:
    try:
        _, need = set_need_status(args.id, args.need_id, "resolved", args.agent)
    except CardNotFoundError as exc:
        raise SystemExit(f"Unknown card id: {args.id}") from exc
    except NeedNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    except BoardValidationError as exc:
        raise SystemExit(str(exc)) from exc
    print(need["id"])
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    board = load_board()
    card = _lookup_card_or_exit(board, args.id)
    message = args.note or f"Manual wake requested by {OWNER_NAME}."
    entry = enqueue_wake(card, "manual", message)
    if entry:
        print(entry["id"])
    return 0


def cmd_wakes(args: argparse.Namespace) -> int:
    if args.run_once:
        import wake

        processed = wake.process_due()
        print(f"processed {len(processed)} wake(s)")

    board = load_board()
    queue = load_wakes()
    wakes = queue.get("wakes", [])
    wakes.sort(key=lambda e: (e.get("status") != "pending", e.get("due_at", ""), e.get("created_at", "")))
    for entry in wakes:
        card = find_card(board, entry.get("card_id", ""))
        key = card_key(board, card) if card else entry.get("card_id")
        print(
            f"{entry.get('id')} | {entry.get('status')} | {key} | {entry.get('reason')} | "
            f"due {entry.get('due_at')} | tries {entry.get('attempts', 0)} | {entry.get('detail', '')}"
        )
    return 0


def cmd_project_add(args: argparse.Namespace) -> int:
    fields = {"id": args.id, "title": args.title, "path": args.path, "machine": args.machine,
              "parent_id": args.parent or None, "herdr_workspaces": args.herdr or []}
    for name in ("key", "description", "status", "priority"):
        if getattr(args, name, None) is not None:
            fields[name] = getattr(args, name)
    try:
        create_project(fields)
    except BoardValidationError as exc:
        raise SystemExit(str(exc)) from exc
    print(args.id)
    return 0


def cmd_project_set(args: argparse.Namespace) -> int:
    fields: dict = {}
    for name in ("title", "path", "machine", "key", "description", "status", "priority"):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value
    if args.parent is not None:
        fields["parent_id"] = args.parent or None
    if args.herdr is not None:
        fields["herdr_workspaces"] = [w for w in args.herdr if w]
    try:
        update_project(args.id, fields)
    except (BoardValidationError, CardNotFoundError) as exc:
        raise SystemExit(f"{exc}: {args.id}" if isinstance(exc, CardNotFoundError) else str(exc)) from exc
    print(args.id)
    return 0


def cmd_project_rm(args: argparse.Namespace) -> int:
    mode = "all" if args.all else "move" if args.move_to else "empty"
    try:
        summary = delete_project(args.id, mode=mode, move_to=args.move_to, agent=args.agent)
    except (BoardValidationError, CardNotFoundError) as exc:
        raise SystemExit(f"{exc}: {args.id}" if isinstance(exc, CardNotFoundError) else str(exc)) from exc
    print(json.dumps(summary))
    return 0


def cmd_summary(_: argparse.Namespace) -> int:
    board = load_board()
    snapshots = read_json(SNAPSHOTS_PATH, {})
    cards = board.get("cards", [])
    counts = {c["id"]: 0 for c in board.get("columns", [])}
    for card in cards:
        counts[card.get("column", "backlog")] = counts.get(card.get("column", "backlog"), 0) + 1
    print("Cards: " + " · ".join(f"{column_title(board, k)} {v}" for k, v in counts.items()))

    needs: list[str] = [
        f"{card_key(board, card)} {card.get('title', '')} · {need.get('agent', '—')}: {need.get('text', '')}"
        for card, need in iter_needs(board)
        if need.get("status") == "open"
    ]
    for card in cards:
        if card.get("column") == "blocked":
            needs.append(
                f"BLOCKED {card_key(board, card)} {project_title(board, card['project_id'])}: "
                f"{card['title']} ({card.get('notes') or 'no note'})"
            )
    for machine in board.get("machines", []):
        snap = snapshots.get(machine["id"]) or {}
        if snap.get("error"):
            needs.append(f"{machine['title']}: sync failed {snap.get('failed_at')}: {snap['error']}")
        for job in (snap.get("data") or {}).get("launchd") or []:
            if not job.get("running") and job.get("last_exit") not in (0, None):
                needs.append(f"{machine['title']}: launchd {job['label']} last run failed (exit {job['last_exit']})")
    print(f"\nNeeds you ({len(needs)}):")
    for line in needs or ["nothing"]:
        print(f"- {line}")

    active = [c for c in cards if c.get("column") in ("in_progress", "review")]
    print(f"\nIn progress / review ({len(active)}):")
    for card in sorted(active, key=lambda c: c.get("updated_at", ""), reverse=True):
        where = f" · {card['machine']}" if card.get("machine") else ""
        print(
            f"- [{column_title(board, card['column'])}] {card_key(board, card)} "
            f"{project_title(board, card['project_id'])}: {card['title']} · {card.get('owner_agent') or '—'}{where}"
        )

    for machine in board.get("machines", []):
        snap = snapshots.get(machine["id"]) or {}
        data = snap.get("data") or {}
        herdr = data.get("herdr_agents") or []
        working = [a for a in herdr if a.get("status") == "working"]
        procs = sum(g.get("count", 0) for g in data.get("agents") or [] if g.get("agent") != "herdr")
        servers = len({s.get("cwd") for s in data.get("dev_servers") or []})
        line = f"\n{machine['title']} (synced {snap.get('synced_at', 'never')}): "
        line += f"{len(herdr)} herdr panes, {len(working)} working · " if herdr else ""
        line += f"{procs} agent processes · {servers} dev servers"
        print(line)
        for a in working:
            print(f"- working: {a.get('workspace')} · {a.get('agent')} · {a.get('title')}")
    return 0


def cmd_workspaces(args: argparse.Namespace) -> int:
    board = load_board()
    titles = {p["id"]: p.get("title", p["id"]) for p in board.get("projects", [])}
    workspaces = board.get("workspaces") or []
    if not workspaces:
        print("No workspaces yet. Create them in the web UI (sidebar, Workspace menu).")
    for ws in workspaces:
        names = ", ".join(titles.get(pid, pid) for pid in ws.get("project_ids", []))
        print(f"{ws['id']:<20} {ws['title']}: {names or 'no projects'}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    board = load_board()
    projects = board.get("projects", [])
    if args.machine:
        projects = [p for p in projects if p.get("machine") == args.machine]
    by_parent: dict[str | None, list[dict]] = {}
    for p in projects:
        by_parent.setdefault(p.get("parent_id"), []).append(p)
    known = {p["id"] for p in projects}

    def emit(proj: dict, depth: int) -> None:
        herdr = ", ".join(proj.get("herdr_workspaces") or [])
        extras = " · ".join(x for x in (proj.get("machine"), f"herdr: {herdr}" if herdr else "") if x)
        key = proj.get("key") or "?"
        print(
            f"{'  ' * depth}{proj['id']:<{max(22 - 2 * depth, 8)}} {key:<6} {proj['title']}"
            + (f"  [{extras}]" if extras else "")
        )
        for child in sorted(by_parent.get(proj["id"], []), key=lambda c: c["id"]):
            emit(child, depth + 1)

    roots = [p for p in projects if not p.get("parent_id") or p["parent_id"] not in known]
    for proj in sorted(roots, key=lambda p: p["id"]):
        emit(proj, 0)
    return 0


MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
MACHINE_ICONS = ("laptop", "desktop", "server")
STARTER_COLUMNS = [
    {"id": "backlog", "title": "Backlog"},
    {"id": "ready", "title": "Ready"},
    {"id": "in_progress", "title": "In progress"},
    {"id": "blocked", "title": "Blocked"},
    {"id": "review", "title": "Review"},
    {"id": "done", "title": "Done"},
]


def cmd_init(args: argparse.Namespace) -> int:
    if BOARD_PATH.exists() and not args.force:
        print(f"{BOARD_PATH} already exists (use --force to replace it)", file=sys.stderr)
        return 1
    BOARD_DIR.mkdir(parents=True, exist_ok=True)
    if args.demo:
        import demo

        board, snapshots, debt = demo.build()
        write_json(SNAPSHOTS_PATH, snapshots)
        write_json(debt_path(), debt)
    else:
        machine_id = args.machine or "local"
        if not MACHINE_ID_RE.fullmatch(machine_id):
            raise SystemExit(f"invalid machine id '{machine_id}' (lowercase letters, digits, dashes)")
        title = args.title or (os.uname().nodename.split(".")[0] or "This machine")
        board = {
            "version": 1,
            "columns": STARTER_COLUMNS,
            "machines": [{"id": machine_id, "title": title, "ssh": None, "icon": "laptop"}],
            "projects": [],
            "cards": [],
            "workspaces": [],
        }
    with board_lock():
        write_json(BOARD_PATH, board)
        save_board(load_board())
    print(f"Created {BOARD_PATH}" + (" with demo data" if args.demo else ""))
    return 0


def cmd_machines(_: argparse.Namespace) -> int:
    board = load_board()
    local = local_machine_id(board)
    for m in board_machines(board):
        where = f"ssh {m['ssh']}" if m.get("ssh") else "board host"
        print(f"{m['id']:<14} {m.get('title') or m['id']:<20} {where}{'  (this machine)' if m['id'] == local else ''}")
    return 0


def cmd_machine_add(args: argparse.Namespace) -> int:
    if not MACHINE_ID_RE.fullmatch(args.id):
        raise SystemExit(f"invalid machine id '{args.id}' (lowercase letters, digits, dashes)")
    with board_lock():
        board = load_board()
        machines = board.setdefault("machines", [])
        entry = next((m for m in machines if isinstance(m, dict) and m.get("id") == args.id), None)
        if entry is None:
            entry = {"id": args.id}
            machines.append(entry)
        entry["title"] = args.title or entry.get("title") or args.id
        entry["ssh"] = args.ssh or None
        entry["icon"] = args.icon or entry.get("icon") or ("server" if args.ssh else "laptop")
        save_board(board)
    print(f"{args.id}: {entry['title']} ({'ssh ' + entry['ssh'] if entry['ssh'] else 'board host'})")
    return 0


def cmd_machine_rm(args: argparse.Namespace) -> int:
    with board_lock():
        board = load_board()
        before = len(board.get("machines", []))
        board["machines"] = [m for m in board.get("machines", []) if not (isinstance(m, dict) and m.get("id") == args.id)]
        if len(board["machines"]) == before:
            raise SystemExit(f"no machine '{args.id}'")
        save_board(board)
    print(f"Removed machine {args.id}; its cards and projects keep the id as a label.")
    return 0


def cmd_render(_: argparse.Namespace) -> int:
    with board_lock():
        render_markdown(load_board())
    print(BOARD_MD)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Workboard: cross-agent Kanban CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="Print cards (default: all)")
    p_show.add_argument("--project", "-p")
    p_show.add_argument("--agent", "-a")
    p_show.add_argument("--machine", "-m")
    p_show.add_argument("--column", "-c")
    p_show.add_argument("--active-only", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_card = sub.add_parser("card", help="Print full card details")
    p_card.add_argument("id")
    p_card.set_defaults(func=cmd_card)

    p_up = sub.add_parser("upsert", help="Create or update a card")
    p_up.add_argument("--id")
    p_up.add_argument("--project", "-p", required=True)
    p_up.add_argument("--title", "-t", required=True)
    p_up.add_argument("--column", "-c")
    p_up.add_argument("--priority")
    p_up.add_argument("--agent", "-a", required=True)
    p_up.add_argument("--machine", "-m", help="Machine id from board.json machines")
    p_up.add_argument("--subproject", "-s")
    p_up.add_argument("--at", help="ISO date for imported history (new cards only)")
    p_up.add_argument("--notes", "-n")
    p_up.set_defaults(func=cmd_upsert)

    p_mv = sub.add_parser("move", help="Move card to column")
    p_mv.add_argument("id")
    p_mv.add_argument("column")
    p_mv.add_argument("--agent", "-a")
    p_mv.add_argument("--note", "-n")
    p_mv.set_defaults(func=cmd_move)

    p_del = sub.add_parser("delete", help="Delete a card")
    p_del.add_argument("id")
    p_del.set_defaults(func=cmd_delete)

    p_touch = sub.add_parser("touch", help="Bump updated_at / note without moving")
    p_touch.add_argument("id")
    p_touch.add_argument("--agent", "-a")
    p_touch.add_argument("--note", "-n")
    p_touch.set_defaults(func=cmd_touch)

    p_comment = sub.add_parser("comment", help="Append a comment event to a card")
    p_comment.add_argument("id")
    p_comment.add_argument("--agent", "-a", required=True)
    p_comment.add_argument("--note", "-n", required=True)
    p_comment.add_argument("--no-wake", action="store_true", help="Do not wake the card's agent for an owner comment")

    def _cmd_comment(inner_args: argparse.Namespace) -> int:
        try:
            _, wake_entry = add_comment_and_wake(
                inner_args.id,
                inner_args.note,
                inner_args.agent,
                wake=not inner_args.no_wake,
            )
        except CardNotFoundError as exc:
            raise SystemExit(f"Unknown card id: {inner_args.id}") from exc
        except BoardValidationError as exc:
            raise SystemExit(str(exc)) from exc
        if wake_entry:
            print(wake_entry["id"])
        return 0

    p_comment.set_defaults(func=_cmd_comment)

    p_attach = sub.add_parser("attach", help="Attach a file to a card")
    p_attach.add_argument("id")
    p_attach.add_argument("file")
    p_attach.add_argument("--agent", "-a", required=True)
    p_attach.add_argument("--note", "-n")
    p_attach.add_argument("--name")
    p_attach.add_argument("--pdf", action="store_true", help="Convert markdown/text/html to PDF before attaching")
    p_attach.set_defaults(func=cmd_attach)

    p_detach = sub.add_parser("detach", help="Remove an attachment from a card")
    p_detach.add_argument("id")
    p_detach.add_argument("attachment_id")
    p_detach.add_argument("--agent", "-a", required=True)
    p_detach.set_defaults(func=cmd_detach)

    p_need = sub.add_parser("need", help="Ask the board owner for something on a card")
    p_need.add_argument("id")
    p_need.add_argument("--agent", "-a", required=True)
    p_need.add_argument("--note", "-n", required=True, help="Need text")
    p_need.set_defaults(func=cmd_need)

    p_needs = sub.add_parser("needs", help="List card needs")
    p_needs.add_argument("--agent", "-a")
    p_needs.add_argument("--all", action="store_true", help="Include resolved needs")
    p_needs.set_defaults(func=cmd_needs)

    p_need_done = sub.add_parser("need-done", help="Mark a need as resolved")
    p_need_done.add_argument("id")
    p_need_done.add_argument("need_id")
    p_need_done.add_argument("--agent", "-a", required=True)
    p_need_done.set_defaults(func=cmd_need_done)

    p_wake = sub.add_parser("wake", help="Enqueue a manual wake for a card")
    p_wake.add_argument("id")
    p_wake.add_argument("--note", "-n")
    p_wake.set_defaults(func=cmd_wake)

    p_wakes = sub.add_parser("wakes", help="List wake queue entries")
    p_wakes.add_argument("--run-once", action="store_true", help="Run one dispatcher pass before listing")
    p_wakes.set_defaults(func=cmd_wakes)

    p_proj = sub.add_parser("project-add", help="Add project or sub-project")
    p_proj.add_argument("--id", required=True)
    p_proj.add_argument("--title", "-t", required=True)
    p_proj.add_argument("--key")
    p_proj.add_argument("--path")
    p_proj.add_argument("--machine", "-m", help="Machine id from board.json machines")
    p_proj.add_argument("--parent")
    p_proj.add_argument("--description")
    p_proj.add_argument("--status", choices=PROJECT_STATUSES)
    p_proj.add_argument("--priority", choices=PROJECT_PRIORITIES)
    p_proj.add_argument("--herdr", action="append", help="herdr workspace label (repeatable)")
    p_proj.set_defaults(func=cmd_project_add)

    p_pset = sub.add_parser("project-set", help="Edit a project; empty string clears a field")
    p_pset.add_argument("id")
    p_pset.add_argument("--key")
    p_pset.add_argument("--title", "-t")
    p_pset.add_argument("--path")
    p_pset.add_argument("--machine", "-m")
    p_pset.add_argument("--parent")
    p_pset.add_argument("--description")
    p_pset.add_argument("--status", choices=PROJECT_STATUSES)
    p_pset.add_argument("--priority", choices=PROJECT_PRIORITIES)
    p_pset.add_argument("--herdr", action="append", help="herdr workspace label (repeatable; replaces list)")
    p_pset.set_defaults(func=cmd_project_set)

    p_prm = sub.add_parser("project-rm", help="Delete a project (empty only, unless --move-to or --all)")
    p_prm.add_argument("id")
    p_prm_mode = p_prm.add_mutually_exclusive_group()
    p_prm_mode.add_argument("--move-to", help="Move its cards and sub-projects to this project first")
    p_prm_mode.add_argument("--all", action="store_true", help="Also delete its sub-projects and every card in them")
    p_prm.add_argument("--agent", "-a", default=OWNER)
    p_prm.set_defaults(func=cmd_project_rm)

    p_summary = sub.add_parser("summary", help="Short status: needs-you, active cards, machines")
    p_summary.set_defaults(func=cmd_summary)

    p_projects = sub.add_parser("projects", help="List project ids as a tree")
    p_projects.add_argument("--machine", "-m")
    p_projects.set_defaults(func=cmd_projects)

    p_ws = sub.add_parser("workspaces", help="List dashboard workspaces and their projects")
    p_ws.set_defaults(func=cmd_workspaces)

    p_sync = sub.add_parser("sync", help="Snapshot what is running on each machine")
    p_sync.add_argument("--machine", "-m")
    p_sync.set_defaults(func=cmd_sync)

    p_debt = sub.add_parser("debt", help="Technical debt per project (rescans when older than 30 minutes)")
    p_debt.add_argument("--project", "-p", help="Rescan and show one project")
    p_debt.add_argument("--rescan", action="store_true", help="Rescan every project now")
    p_debt.add_argument("--json", action="store_true")
    p_debt.set_defaults(func=cmd_debt)

    p_init = sub.add_parser("init", help="Create board.json (empty, or --demo for sample data)")
    p_init.add_argument("--demo", action="store_true", help="Fill the board with fictional demo projects and agents")
    p_init.add_argument("--machine", help="Id for this machine (default: local)")
    p_init.add_argument("--title", help="Display name for this machine (default: hostname)")
    p_init.add_argument("--force", action="store_true", help="Replace an existing board.json")
    p_init.set_defaults(func=cmd_init)

    p_machines = sub.add_parser("machines", help="List machines")
    p_machines.set_defaults(func=cmd_machines)

    p_madd = sub.add_parser("machine-add", help="Add or update a machine (--ssh for remote ones)")
    p_madd.add_argument("id")
    p_madd.add_argument("--title", "-t")
    p_madd.add_argument("--ssh", help="SSH host alias; omit for the machine the board lives on")
    p_madd.add_argument("--icon", choices=MACHINE_ICONS)
    p_madd.set_defaults(func=cmd_machine_add)

    p_mrm = sub.add_parser("machine-rm", help="Remove a machine")
    p_mrm.add_argument("id")
    p_mrm.set_defaults(func=cmd_machine_rm)

    p_render = sub.add_parser("render", help="Write BOARD.md from board.json")
    p_render.set_defaults(func=cmd_render)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
