#!/usr/bin/env python3
"""Wake dispatcher for workboard comments/answers."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import workboard

Runner = Callable[[str, List[str], Optional[str], int], Tuple[int, str, str]]

RESUME_ARGS = {
    "claude": ["--resume"],
    "cursor": ["--resume"],
    "codex": ["resume"],
    "opencode": ["--session"],
    "hermes": ["--resume"],
}
# Woken agents run unattended, so by default every kind starts in its skip-all-prompts ("yolo")
# mode; an approval prompt would otherwise stall the agent until someone finds the pane.
# WB_YOLO=0 starts them with their normal permission prompts instead.
YOLO_ARGS = {
    "opencode": ["--auto"],
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "cursor": ["--force", "--trust", "--sandbox", "disabled", "--approve-mcps"],
    "hermes": ["--yolo"],
}
if os.environ.get("WB_YOLO", "1") == "0":
    YOLO_ARGS = {kind: [] for kind in YOLO_ARGS}
SPAWN_ORDER = [(kind, YOLO_ARGS[kind]) for kind in ("opencode", "claude", "codex", "cursor")]
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dispatch_lock_path() -> Path:
    return workboard.BOARD_DIR / "wake.lock"


def _acquire_dispatch_lock() -> object | None:
    path = _dispatch_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def _release_dispatch_lock(lock: object | None) -> None:
    if lock is None:
        return
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


REMOTE_HERDR = '"$(command -v herdr || echo "$HOME"/.local/bin/herdr)"'


def _herdr_path(machine: str) -> str:
    if workboard.machine_ssh(machine):
        return REMOTE_HERDR
    return shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")


def _default_runner(machine: str, argv: list[str], input_text: str | None = None, timeout: int = 15) -> tuple[int, str, str]:
    ssh_host = workboard.machine_ssh(machine)
    if not ssh_host:
        cmd = list(argv)
        if cmd and cmd[0] == "herdr":
            cmd[0] = _herdr_path(machine)
        if input_text is not None and cmd[1:3] == ["agent", "prompt"] and len(cmd) == 4:
            cmd.append(input_text)
            input_text = None
        try:
            result = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 124, "", f"{type(exc).__name__}: {exc}"
        return result.returncode, result.stdout, result.stderr

    remote_parts = []
    for idx, part in enumerate(argv):
        if idx == 0 and part == "herdr":
            remote_parts.append(REMOTE_HERDR)
            continue
        remote_parts.append(shlex.quote(part))
    remote_cmd = " ".join(remote_parts)

    ssh_cmd = ["ssh"]
    if not (
        input_text is not None
        and len(argv) == 4
        and argv[0] == "herdr"
        and argv[1] == "agent"
        and argv[2] == "prompt"
    ):
        ssh_cmd.append("-n")
    ssh_cmd.extend([*SSH_OPTS, ssh_host])

    if (
        input_text is not None
        and len(argv) == 4
        and argv[0] == "herdr"
        and argv[1] == "agent"
        and argv[2] == "prompt"
    ):
        pane_id = argv[3]
        remote_cmd = f"{REMOTE_HERDR} agent prompt {shlex.quote(pane_id)} \"$(cat)\""
    ssh_cmd.append(remote_cmd)

    try:
        result = subprocess.run(
            ssh_cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


def _runner(runner: Runner | None) -> Runner:
    return runner or _default_runner


def _parse_json(text: str) -> dict:
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_agent_info(stdout: str) -> dict | None:
    payload = _parse_json(stdout)
    info = payload.get("result", {}).get("agent")
    return info if isinstance(info, dict) else None


def _extract_workspaces(stdout: str) -> list[dict]:
    payload = _parse_json(stdout)
    spaces = payload.get("result", {}).get("workspaces")
    if not isinstance(spaces, list):
        return []
    return [space for space in spaces if isinstance(space, dict)]


def _extract_any_field(obj: object, field: str) -> str | None:
    if isinstance(obj, dict):
        value = obj.get(field)
        if isinstance(value, str) and value:
            return value
        for child in obj.values():
            found = _extract_any_field(child, field)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = _extract_any_field(child, field)
            if found:
                return found
    return None


def _extract_pane_and_tab(stdout: str) -> tuple[str | None, str | None]:
    payload = _parse_json(stdout)
    pane_id = _extract_any_field(payload, "pane_id")
    tab_id = _extract_any_field(payload, "tab_id")
    if not pane_id:
        pane_match = re.search(r"\b[\w]+:p[\w]+\b", stdout)
        if pane_match:
            pane_id = pane_match.group(0)
    if not tab_id and pane_id and ":p" in pane_id:
        tab_id = pane_id.replace(":p", ":t", 1)
    return pane_id, tab_id


def _path_exists(machine: str, cwd: str, runner: Runner) -> bool:
    rc, _, _ = runner(
        machine,
        ["python3", "-c", "import os,sys;sys.exit(0 if os.path.isdir(sys.argv[1]) else 1)", cwd],
        timeout=8,
    )
    return rc == 0


def _home_dir(machine: str, runner: Runner) -> str:
    rc, stdout, _ = runner(machine, ["python3", "-c", "import pathlib; print(pathlib.Path.home())"], timeout=8)
    if rc == 0 and stdout.strip():
        return stdout.strip().splitlines()[-1]
    return str(Path.home())


def _choose_cwd(machine: str, preferred: str | None, fallback: str | None, runner: Runner) -> str:
    for candidate in (preferred, fallback):
        if isinstance(candidate, str) and candidate and _path_exists(machine, candidate, runner):
            return candidate
    return _home_dir(machine, runner)


def _workspace_info(machine: str, runner: Runner) -> list[dict]:
    rc, stdout, _ = runner(machine, ["herdr", "workspace", "list"], timeout=10)
    if rc != 0:
        return []
    return _extract_workspaces(stdout)


def _workspace_by_id(workspaces: list[dict], workspace_id: str | None) -> dict | None:
    if not workspace_id:
        return None
    return next((w for w in workspaces if w.get("workspace_id") == workspace_id), None)


def _workspace_by_label(workspaces: list[dict], label: str | None) -> dict | None:
    if not isinstance(label, str):
        return None
    needle = label.strip().lower()
    if not needle:
        return None
    return next((w for w in workspaces if str(w.get("label", "")).strip().lower() == needle), None)


def _ensure_workspace(machine: str, cwd: str, target_workspace_id: str | None, project_labels: list[str], runner: Runner) -> tuple[str | None, str | None, str | None]:
    spaces = _workspace_info(machine, runner)

    selected = _workspace_by_id(spaces, target_workspace_id)
    if selected:
        return selected.get("workspace_id"), selected.get("label"), None

    for label in project_labels:
        selected = _workspace_by_label(spaces, label)
        if selected:
            return selected.get("workspace_id"), selected.get("label"), None

    selected = _workspace_by_label(spaces, "Workboard")
    if selected:
        return selected.get("workspace_id"), selected.get("label"), None

    rc, _, stderr = runner(
        machine,
        ["herdr", "workspace", "create", "--label", "Workboard", "--cwd", cwd, "--no-focus"],
        timeout=20,
    )
    if rc != 0:
        return None, None, (stderr or "workspace create failed").strip()

    spaces = _workspace_info(machine, runner)
    selected = _workspace_by_label(spaces, "Workboard")
    if not selected:
        return None, None, "workspace create succeeded but Workboard label not found"
    return selected.get("workspace_id"), selected.get("label"), None


def _tab_create(machine: str, workspace_id: str, cwd: str, name: str, runner: Runner) -> tuple[str | None, str | None, str | None]:
    rc, stdout, stderr = runner(
        machine,
        ["herdr", "tab", "create", "--workspace", workspace_id, "--cwd", cwd, "--label", name, "--no-focus"],
        timeout=20,
    )
    if rc != 0:
        return None, None, (stderr or "tab create failed").strip()
    pane_id, tab_id = _extract_pane_and_tab(stdout)
    if not pane_id:
        return None, None, "tab create did not return pane id"
    return pane_id, tab_id, None


def _tab_close(machine: str, tab_id: str | None, runner: Runner) -> None:
    if not tab_id:
        return
    runner(machine, ["herdr", "tab", "close", tab_id], timeout=10)


def _agent_start(
    machine: str,
    name: str,
    kind: str,
    pane_id: str,
    extra_args: list[str],
    runner: Runner,
) -> tuple[bool, str]:
    argv = ["herdr", "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "120000"]
    if extra_args:
        argv.extend(["--", *extra_args])
    rc, stdout, stderr = runner(machine, argv, timeout=150)
    if rc != 0:
        error = (stderr or stdout or "agent start failed").strip()
        if "blocked during startup" in error:
            error = f"{kind} is stuck on a startup prompt (usually folder trust; open it there once and trust the folder)"
        return False, error
    return True, ""


def _prompt_agent(machine: str, pane_id: str, message: str, runner: Runner) -> tuple[bool, str]:
    rc, stdout, stderr = runner(machine, ["herdr", "agent", "prompt", pane_id], input_text=message, timeout=30)
    if rc != 0:
        return False, (stderr or stdout or "agent prompt failed").strip()
    return True, ""


def _agent_get(machine: str, pane_id: str, runner: Runner) -> tuple[bool, dict | None, str]:
    rc, stdout, stderr = runner(machine, ["herdr", "agent", "get", pane_id], timeout=10)
    if rc != 0:
        return False, None, (stderr or "agent get failed").strip()
    info = _extract_agent_info(stdout)
    if not info:
        return False, None, "agent get did not return agent info"
    return True, info, ""


def _find_live_agent(machine: str, target: dict, runner: Runner) -> dict | None:
    """A running herdr agent with the target's session id, or named like the board agent."""
    session_id = target.get("session_id")
    name = target.get("agent")
    if not session_id and not name:
        return None
    rc, stdout, _ = runner(machine, ["herdr", "agent", "list"], timeout=10)
    if rc != 0:
        return None
    agents = _parse_json(stdout).get("result", {}).get("agents", [])
    for info in agents if isinstance(agents, list) else []:
        session = info.get("agent_session")
        live_session = session.get("value") if isinstance(session, dict) else None
        if session_id and live_session == session_id:
            return info
    for info in agents if isinstance(agents, list) else []:
        if name and info.get("name") == name:
            return info
    return None


def _target_from_agent_info(machine: str, info: dict, board_agent: str, at: str) -> dict:
    session = info.get("agent_session")
    session_id = session.get("value") if isinstance(session, dict) else None
    return {
        "machine": machine,
        "kind": info.get("agent"),
        "pane_id": info.get("pane_id"),
        "workspace_id": info.get("workspace_id"),
        "terminal_id": info.get("terminal_id"),
        "session_id": session_id,
        "cwd": info.get("cwd"),
        "agent": board_agent,
        "at": at,
    }


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) if text else ">"


def _wb_command(machine: str) -> str:
    return "workboard"


def _base_message(machine: str, key: str, title: str, card_id: str, texts: list[str]) -> str:
    blocks = "\n\n".join(_blockquote(text) for text in texts if isinstance(text, str) and text.strip())
    wb = _wb_command(machine)
    return (
        f"[Workboard] {workboard.OWNER_NAME} commented on {key} \"{title}\" (card {card_id}):\n\n"
        f"{blocks}\n\n"
        f"Read the ticket: `{wb} card {key}`. "
        f"Reply on it with `{wb} comment {card_id} -a <your agent id> -n \"...\"` (markdown), then do what they asked and keep the card updated. "
        f"If they answered a need, act on it and run `{wb} need-done {card_id} <need-id> -a <you>`."
    )


def _fresh_message(machine: str, name: str, cwd: str, key: str, title: str, card_id: str, texts: list[str]) -> str:
    return (
        f"You were started by the workboard because {workboard.OWNER_NAME} commented on a ticket and the agent that worked on it "
        f"is not running. Your agent id is {name}. Project folder: {cwd}. "
        + _base_message(machine, key, title, card_id, texts)
        + "\n\nLoad the workboard skill if you have it."
    )


def _pick_due_entries(now_iso: str) -> list[dict]:
    now_dt = _parse_iso(now_iso)
    with workboard.wakes_lock():
        queue = workboard.load_wakes()
    due = []
    for entry in queue.get("wakes", []):
        if entry.get("status") != "pending":
            continue
        if _parse_iso(entry.get("due_at")) <= now_dt:
            due.append(entry)
    due.sort(key=lambda e: (_parse_iso(e.get("due_at")), _parse_iso(e.get("created_at")), e.get("id", "")))
    return due


def _reload_entry(entry_id: str) -> dict | None:
    with workboard.wakes_lock():
        queue = workboard.load_wakes()
    for entry in queue.get("wakes", []):
        if entry.get("id") == entry_id:
            return dict(entry)
    return None


def _save_entry(updated: dict) -> dict:
    with workboard.wakes_lock():
        queue = workboard.load_wakes()
        for idx, current in enumerate(queue.get("wakes", [])):
            if current.get("id") == updated.get("id"):
                queue["wakes"][idx] = updated
                break
        workboard.save_wakes(queue)
    return dict(updated)


def _card_context(card_id: str) -> tuple[dict, dict, dict | None] | None:
    board = workboard.load_board()
    card = workboard.find_card(board, card_id)
    if not card:
        return None
    project = next((p for p in board.get("projects", []) if p.get("id") == card.get("project_id")), None)
    return board, card, project


def _wake_name(board: dict, card: dict) -> str:
    key = workboard.card_key(board, card)
    key_part = key.split("-", 1)[0].lower()
    seq = card.get("seq")
    seq_part = seq if isinstance(seq, int) else 0
    return f"wb-{key_part}-{seq_part}-{uuid.uuid4().hex[:4]}"


def _finalize(
    entry: dict,
    status: str,
    method: str | None,
    detail: str,
    now_iso: str,
    target: dict | None = None,
) -> dict:
    entry["status"] = status
    entry["method"] = method
    entry["detail"] = detail
    entry["target"] = target if target is not None else entry.get("target")
    entry["finished_at"] = now_iso if status in {"delivered", "failed"} else None
    return entry


def _resume_args(kind: str, session_id: str) -> list[str] | None:
    if kind not in RESUME_ARGS:
        return None
    if kind == "codex":
        return ["resume", *YOLO_ARGS["codex"], session_id]
    return [*RESUME_ARGS[kind], session_id, *YOLO_ARGS.get(kind, [])]


def _project_labels(board: dict, project: dict | None) -> list[str]:
    """herdr labels of the project, else of its nearest ancestor that has some."""
    index = {p.get("id"): p for p in board.get("projects", []) if isinstance(p, dict)}
    seen: set = set()
    while isinstance(project, dict) and project.get("id") not in seen:
        seen.add(project.get("id"))
        labels = project.get("herdr_workspaces")
        found = [label for label in labels if isinstance(label, str) and label.strip()] if isinstance(labels, list) else []
        if found:
            return found
        project = index.get(project.get("parent_id"))
    return []


def _deliver(entry: dict, now_iso: str, runner: Runner) -> tuple[dict, dict | None]:
    ctx = _card_context(entry.get("card_id", ""))
    if ctx is None:
        return _finalize(entry, "failed", None, "card not found", now_iso), {
            "status": "failed",
            "method": None,
            "text": "Could not wake: card not found.",
            "target": None,
        }

    board, card, project = ctx
    target = entry.get("target") if isinstance(entry.get("target"), dict) else card.get("wake_target")
    target = target if isinstance(target, dict) else {}
    local = workboard.local_machine_id(board)
    machine = target.get("machine") or (project.get("machine") if isinstance(project, dict) else None) or local
    machine = machine if machine in {m["id"] for m in workboard.board_machines(board)} else local

    key = workboard.card_key(board, card)
    title = card.get("title", "")
    card_id = card.get("id", "")
    texts = entry.get("texts") if isinstance(entry.get("texts"), list) else []
    message = _base_message(machine, key, title, card_id, texts)

    pane_id = target.get("pane_id")
    if isinstance(pane_id, str) and pane_id:
        ok, info, error = _agent_get(machine, pane_id, runner)
        if ok and info:
            same_terminal = True
            if target.get("terminal_id") and info.get("terminal_id"):
                same_terminal = target.get("terminal_id") == info.get("terminal_id")
            same_session = True
            session = info.get("agent_session")
            session_id = session.get("value") if isinstance(session, dict) else None
            if target.get("session_id") and session_id:
                same_session = target.get("session_id") == session_id
            alive = same_terminal and same_session
            if alive and info.get("agent_status") == "blocked":
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if entry["attempts"] >= 10:
                    detail = f"agent is waiting for input in {pane_id}"
                    return _finalize(entry, "failed", "message", detail, now_iso, target), {
                        "status": "failed",
                        "method": "message",
                        "text": f"Could not wake: {detail}.",
                        "target": target,
                    }
                retry_at = (
                    datetime.fromisoformat(now_iso.replace("Z", "+00:00")) + timedelta(seconds=workboard.WAKE_RETRY_SECONDS)
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                entry["due_at"] = retry_at
                entry["detail"] = "agent blocked; retrying"
                return entry, None
            if alive:
                prompted, prompt_error = _prompt_agent(machine, pane_id, message, runner)
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if prompted:
                    kind = info.get("agent") or target.get("kind") or "agent"
                    detail = f"Woke {kind} in {pane_id} (ran `herdr agent prompt {pane_id}`)"
                    new_target = _target_from_agent_info(machine, info, target.get("agent") or card.get("owner_agent") or "unknown", now_iso)
                    return _finalize(entry, "delivered", "message", detail, now_iso, new_target), {
                        "status": "delivered",
                        "method": "message",
                        "text": f"{detail}.",
                        "target": new_target,
                    }
                target_error = prompt_error or "prompt failed"
                # fall through to resume/spawn after failed prompt
            else:
                target_error = "pane no longer matches saved terminal/session"
        else:
            target_error = error or "agent get failed"
    else:
        target_error = "no pane id"

    found = _find_live_agent(machine, {**target, "agent": target.get("agent") or card.get("owner_agent")}, runner)
    if found:
        found_pane = found.get("pane_id")
        if found.get("agent_status") == "blocked":
            target_error = f"agent is waiting for input in {found_pane}"
        else:
            prompted, prompt_error = _prompt_agent(machine, found_pane, message, runner)
            if prompted:
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                detail = f"Woke {found.get('agent') or 'agent'} in {found_pane} (ran `herdr agent prompt {found_pane}`)"
                new_target = _target_from_agent_info(machine, found, target.get("agent") or card.get("owner_agent") or "unknown", now_iso)
                new_target["session_id"] = new_target["session_id"] or target.get("session_id")
                return _finalize(entry, "delivered", "message", detail, now_iso, new_target), {
                    "status": "delivered",
                    "method": "message",
                    "text": f"{detail}.",
                    "target": new_target,
                }
            target_error = prompt_error or "prompt failed"

    project_path = project.get("path") if isinstance(project, dict) else None
    base_cwd = _choose_cwd(machine, project_path if isinstance(project_path, str) else None, None, runner)

    failures: list[str] = [f"message unavailable: {target_error}"]
    resume_kind = target.get("kind") if isinstance(target.get("kind"), str) else None
    session_id = target.get("session_id") if isinstance(target.get("session_id"), str) else None
    name = _wake_name(board, card)

    if resume_kind and session_id and resume_kind in RESUME_ARGS:
        resume_cwd = _choose_cwd(machine, target.get("cwd"), project_path if isinstance(project_path, str) else None, runner)
        workspace_id, workspace_label, err = _ensure_workspace(
            machine,
            resume_cwd,
            target.get("workspace_id"),
            _project_labels(board, project),
            runner,
        )
        if workspace_id:
            pane, tab_id, tab_error = _tab_create(machine, workspace_id, resume_cwd, name, runner)
            if pane:
                args = _resume_args(resume_kind, session_id)
                started, start_error = _agent_start(machine, name, resume_kind, pane, args or [], runner)
                if started:
                    prompted, prompt_error = _prompt_agent(machine, pane, message, runner)
                    if prompted:
                        got, info, _ = _agent_get(machine, pane, runner)
                        if got and info:
                            new_target = _target_from_agent_info(machine, info, name, now_iso)
                        else:
                            new_target = {
                                "machine": machine,
                                "kind": resume_kind,
                                "pane_id": pane,
                                "workspace_id": workspace_id,
                                "terminal_id": None,
                                "session_id": session_id,
                                "cwd": resume_cwd,
                                "agent": name,
                                "at": now_iso,
                            }
                        entry["attempts"] = int(entry.get("attempts", 0)) + 1
                        detail = f"Resumed {resume_kind} session in new tab {pane} (ran `herdr agent start {name} --kind {resume_kind} -- {' '.join(args or [])}`, then `herdr agent prompt {pane}`)"
                        return _finalize(entry, "delivered", "resume", detail, now_iso, new_target), {
                            "status": "delivered",
                            "method": "resume",
                            "text": f"{detail}.",
                            "target": new_target,
                        }
                    failures.append(f"resume prompt failed ({resume_kind}): {prompt_error or 'unknown error'}")
                else:
                    failures.append(f"resume start failed ({resume_kind}): {start_error}")
                _tab_close(machine, tab_id, runner)
            else:
                failures.append(f"resume tab failed ({resume_kind}): {tab_error}")
        else:
            failures.append(f"resume workspace failed ({resume_kind}): {err}")

    for kind, extra in SPAWN_ORDER:
        spawn_cwd = _choose_cwd(machine, project_path if isinstance(project_path, str) else None, base_cwd, runner)
        workspace_id, workspace_label, err = _ensure_workspace(
            machine,
            spawn_cwd,
            target.get("workspace_id"),
            _project_labels(board, project),
            runner,
        )
        if not workspace_id:
            failures.append(f"spawn workspace failed ({kind}): {err}")
            continue

        pane, tab_id, tab_error = _tab_create(machine, workspace_id, spawn_cwd, name, runner)
        if not pane:
            failures.append(f"spawn tab failed ({kind}): {tab_error}")
            continue

        started, start_error = _agent_start(machine, name, kind, pane, extra, runner)
        if not started:
            failures.append(f"spawn start failed ({kind}): {start_error}")
            _tab_close(machine, tab_id, runner)
            continue

        prompt_text = _fresh_message(machine, name, spawn_cwd, key, title, card_id, texts)
        prompted, prompt_error = _prompt_agent(machine, pane, prompt_text, runner)
        if not prompted:
            failures.append(f"spawn prompt failed ({kind}): {prompt_error or 'unknown error'}")
            _tab_close(machine, tab_id, runner)
            continue

        got, info, _ = _agent_get(machine, pane, runner)
        if got and info:
            new_target = _target_from_agent_info(machine, info, name, now_iso)
        else:
            new_target = {
                "machine": machine,
                "kind": kind,
                "pane_id": pane,
                "workspace_id": workspace_id,
                "terminal_id": None,
                "session_id": None,
                "cwd": spawn_cwd,
                "agent": name,
                "at": now_iso,
            }
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        label = workspace_label or workspace_id
        detail = f"Started fresh {kind} agent in {label} (ran `herdr agent start {name} --kind {kind} -- {' '.join(extra)}`, then `herdr agent prompt {pane}`)"
        return _finalize(entry, "delivered", "spawn", detail, now_iso, new_target), {
            "status": "delivered",
            "method": "spawn",
            "text": f"{detail}.",
            "target": new_target,
        }

    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    detail = "; ".join(failures) or "all wake methods failed"
    return _finalize(entry, "failed", "spawn", detail, now_iso, target if isinstance(target, dict) else None), {
        "status": "failed",
        "method": "spawn",
        "text": f"Could not wake: {detail}.",
        "target": target if isinstance(target, dict) else None,
    }


def process_due(now: str | datetime | None = None, runner: Runner | None = None) -> list[dict]:
    """Process all due wake entries once.

    Returns wake entries that were processed in this pass.
    """

    if isinstance(now, datetime):
        now_iso = now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    elif isinstance(now, str) and now:
        now_iso = now
    else:
        now_iso = _utc_now()

    dispatch_lock = _acquire_dispatch_lock()
    if dispatch_lock is None:
        return []

    run = _runner(runner)
    processed: list[dict] = []
    try:
        due_entries = _pick_due_entries(now_iso)
        for due in due_entries:
            current = _reload_entry(due.get("id"))
            if not current:
                continue
            if current.get("status") != "pending" or _parse_iso(current.get("due_at")) > _parse_iso(now_iso):
                continue
            updated, event = _deliver(current, now_iso, run)
            saved = _save_entry(updated)
            processed.append(saved)
            if event and saved.get("status") in {"delivered", "failed"}:
                workboard.record_wake_result(
                    saved.get("card_id", ""),
                    event["status"],
                    event["method"],
                    event["text"],
                    event.get("target"),
                )
    finally:
        _release_dispatch_lock(dispatch_lock)

    return processed


if __name__ == "__main__":
    processed = process_due()
    print(json.dumps({"processed": len(processed)}))
