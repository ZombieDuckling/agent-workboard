"""Notifications when an agent replies to the board owner.

A reply is an agent comment on a card where the owner (WB_OWNER) has already
commented or answered a need. On macOS it shows a system notification; set
WB_NOTIFY_CMD to also pipe each message to any command (Slack, ntfy, email...). The server calls `process()` every few seconds; the watermark
in notify_state.json makes each reply notify once, across restarts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import workboard

SYSTEM_AGENTS = {"", "workboard", "system"}
OWNER_EVENTS = {"comment", "need_answered"}
MAX_SINGLE = 3
OSASCRIPT = "/usr/bin/osascript"
NOTIFY_SCRIPT = [
    "-e", "on run argv",
    "-e", 'display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv) sound name "Glass"',
    "-e", "end run",
]


def state_path() -> Path:
    return workboard.BOARD_DIR / "notify_state.json"


def load_state() -> dict:
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict) -> None:
    path = state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def event_id(card: dict, event: dict) -> str:
    return f"{card.get('id')}|{event.get('at')}|{event.get('agent')}"


def replies(board: dict, since: str = "", skip: frozenset = frozenset()) -> list[tuple[dict, dict]]:
    """(card, event) for every agent comment after the owner spoke on that card, from `since` on, minus `skip`."""
    found = []
    for card in board.get("cards", []):
        if not isinstance(card, dict):
            continue
        owner_spoke = False
        for event in card.get("activity") or []:
            if not isinstance(event, dict):
                continue
            agent = str(event.get("agent") or "").strip()
            if workboard.is_owner(agent):
                owner_spoke = owner_spoke or event.get("type") in OWNER_EVENTS
                continue
            at = str(event.get("at") or "")
            if (
                owner_spoke
                and event.get("type") == "comment"
                and agent.lower() not in SYSTEM_AGENTS
                and at >= since
                and event_id(card, event) not in skip
            ):
                found.append((card, event))
    found.sort(key=lambda pair: str(pair[1].get("at") or ""))
    return found


def plain(text: str, limit: int = 180) -> str:
    text = re.sub(r"```.*?```", " [code] ", text or "", flags=re.S)
    text = re.sub(r"[`*_#>]+", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def send_mac(title: str, subtitle: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    subprocess.run(
        [OSASCRIPT, *NOTIFY_SCRIPT, title, subtitle, plain(message)],
        capture_output=True,
        timeout=10,
        check=False,
    )


def send_command(command: str, title: str, subtitle: str, message: str) -> None:
    """Pipe the message to WB_NOTIFY_CMD (stdin; also WB_TITLE/WB_SUBTITLE env) in the background,
    so a slow hook never stalls the server."""
    body = f"{title}\n{subtitle}\n\n{plain(message, 700)}".strip()

    def run() -> None:
        try:
            result = subprocess.run(
                shlex.split(command),
                env={**os.environ, "WB_TITLE": title, "WB_SUBTITLE": subtitle},
                input=body,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                print(f"notify command failed ({result.returncode}): {result.stderr.strip()[-300:]}", file=sys.stderr, flush=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"notify command failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=run, daemon=True).start()


def send(title: str, subtitle: str, message: str) -> None:
    send_mac(title, subtitle, message)
    command = os.environ.get("WB_NOTIFY_CMD", "").strip()
    if command:
        send_command(command, title, subtitle, message)


def messages(board: dict, found: list[tuple[dict, dict]]) -> list[tuple[str, str, str]]:
    if len(found) > MAX_SINGLE:
        keys = []
        for card, _event in found:
            key = workboard.card_key(board, card)
            if key not in keys:
                keys.append(key)
        agents = sorted({str(event.get("agent")) for _card, event in found})
        return [(f"{len(found)} replies on the board", ", ".join(agents), "On " + ", ".join(keys[:8]) + (" and more" if len(keys) > 8 else ""))]
    return [
        (
            f"{event.get('agent')} replied on {workboard.card_key(board, card)}",
            str(card.get("title") or ""),
            str(event.get("text") or ""),
        )
        for card, event in found
    ]


def process(board: dict | None = None) -> list[tuple[str, str, str]]:
    """Notify about replies newer than the watermark; returns what was sent."""
    if os.environ.get("WB_NOTIFY", "1") == "0":
        return []
    state = load_state()
    if not state.get("since"):
        save_state({"since": workboard.utc_now()})
        return []
    if board is None:
        board = workboard.load_board()
    since = str(state["since"])
    edge = state.get("edge") if isinstance(state.get("edge"), list) else []
    found = replies(board, since, frozenset(edge))
    if not found:
        return []
    sent = messages(board, found)
    for title, subtitle, message in sent:
        send(title, subtitle, message)
    latest = str(found[-1][1].get("at"))
    edge = (edge if latest == since else []) + [event_id(c, e) for c, e in found if str(e.get("at")) == latest]
    save_state({"since": latest, "edge": edge})
    return sent
