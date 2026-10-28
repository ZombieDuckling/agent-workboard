#!/usr/bin/env python3
"""Print a JSON snapshot of what is running and moving on this machine.

Runs locally or piped over SSH (`ssh <host> python3 - < collect.py`), so it must
stay stdlib-only and Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

HOME = Path.home()
PROJECT_ROOTS = [HOME, HOME / "Projects", HOME / "Documents"]
RECENT_HOURS = 48
AGENT_PATTERN = re.compile(
    r"(^|/)(claude|codex|opencode|hermes|herdr|cursor-agent|aider)(\s|$)", re.IGNORECASE
)
DEV_SERVER_PATTERN = re.compile(
    r"(wrangler dev|vite|astro dev|next dev|npm run dev|pnpm dev|bun run dev|opencode serve)"
)


def _config_value(key: str, default: str) -> str:
    """Env var, else ~/.config/workboard/config.env on this machine (SSH runs get no env)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in (HOME / ".config" / "workboard" / "config.env").read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return default


# launchd jobs to watch (macOS): the board's own jobs, plus anything you add in config.env.
LAUNCHD_PATTERN = re.compile(_config_value("WB_LAUNCHD_PATTERN", r"^dev\.workboard\."))


def run(cmd: list[str], timeout: int = 10, env: dict | None = None) -> str:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **env} if env else None,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def process_cwd(pid: str) -> str | None:
    out = run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], timeout=5)
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def short_path(path: str | None) -> str | None:
    if not path:
        return None
    home = str(HOME)
    return "~" + path[len(home):] if path.startswith(home) else path


def processes() -> tuple[list[dict], list[dict]]:
    """Agent processes grouped by (kind, cwd), and deduped dev servers."""
    groups: dict[tuple, dict] = {}
    servers: list[dict] = []
    out = run(["ps", "-axo", "pid=,etime=,command="])
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, command = parts
        if ".app/Contents/" in command or "Helper" in command or "node_modules/@openai/codex" in command:
            continue
        if DEV_SERVER_PATTERN.search(command):
            servers.append(
                {"uptime": etime, "command": command[:140], "cwd": short_path(process_cwd(pid))}
            )
            continue
        match = AGENT_PATTERN.search(command.split(" --")[0])
        if not match:
            continue
        kind = match.group(2).lower()
        if "app-server" in command:
            kind += " app-server"
        cwd = short_path(process_cwd(pid))
        group = groups.setdefault((kind, cwd), {"agent": kind, "cwd": cwd, "count": 0})
        group["count"] += 1
    agents = sorted(groups.values(), key=lambda g: (-g["count"], g["agent"]))
    return agents, dedupe_servers(servers)


def herdr_agents() -> list[dict]:
    """Agent panes from herdr, with workspace label, status and task title."""
    # SSH and launchd sessions do not have ~/.local/bin on PATH.
    herdr = shutil.which("herdr") or str(HOME / ".local" / "bin" / "herdr")
    try:
        agents = json.loads(run([herdr, "agent", "list"]))["result"]["agents"]
        spaces = json.loads(run([herdr, "workspace", "list"]))["result"]["workspaces"]
    except (ValueError, KeyError, TypeError):
        return []
    labels = {w["workspace_id"]: w.get("label") for w in spaces}
    return [
        {
            "workspace": labels.get(a.get("workspace_id"), a.get("workspace_id")),
            "name": a.get("name"),
            "pane_id": a.get("pane_id"),
            "agent": a.get("agent"),
            "status": a.get("agent_status"),
            "title": a.get("terminal_title_stripped") or a.get("name"),
            "cwd": short_path(a.get("cwd")),
        }
        for a in agents
    ]


def dedupe_servers(servers: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for s in servers:
        key = (s["cwd"], s["command"].split()[0].rsplit("/", 1)[-1])
        if key not in seen:
            seen[key] = s
    return list(seen.values())


def launchd_jobs() -> list[dict]:
    jobs = []
    for line in run(["launchctl", "list"]).splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3 or not LAUNCHD_PATTERN.match(parts[2]):
            continue
        pid, status, label = parts
        jobs.append(
            {
                "label": label,
                "running": pid != "-",
                "last_exit": int(status) if status.lstrip("-").isdigit() else status,
            }
        )
    return sorted(jobs, key=lambda j: j["label"])


def git_info(repo: Path) -> dict:
    # A refreshed index would bump .git mtime and make every scanned repo look recent.
    def git(*args: str) -> str:
        return run(
            ["git", "-C", str(repo), *args], timeout=5, env={"GIT_OPTIONAL_LOCKS": "0"}
        ).strip()

    return {
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or None,
        "last_commit": git("log", "-1", "--format=%cr · %s") or None,
        "dirty_files": len([l for l in git("status", "--porcelain").splitlines() if l]),
    }


def recent_projects() -> list[dict]:
    cutoff = time.time() - RECENT_HOURS * 3600
    found: dict[str, dict] = {}
    for root in PROJECT_ROOTS:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for d in children:
            if not d.is_dir() or d.name.startswith(".") or d in PROJECT_ROOTS:
                continue
            if not (d / ".git").exists() and root != HOME / "Projects":
                continue
            try:
                mtime = max(
                    p.stat().st_mtime
                    for p in (d, d / ".git" / "HEAD", d / ".git" / "logs" / "HEAD")
                    if p.exists()
                )
            except (OSError, ValueError):
                continue
            if mtime < cutoff:
                continue
            entry = {
                "path": short_path(str(d)),
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
            }
            if (d / ".git").exists():
                entry.update(git_info(d))
            found[str(d)] = entry
    return sorted(found.values(), key=lambda p: p["modified"], reverse=True)[:20]


def hermes_tasks() -> list[dict]:
    db = HOME / ".hermes" / "kanban.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "select id, title, status, assignee, workspace_path from tasks "
            "where status not in ('done', 'archived') order by created_at desc limit 25"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [
        {"id": r[0], "title": r[1], "status": r[2], "assignee": r[3], "path": short_path(r[4])}
        for r in rows
    ]


def main() -> None:
    agents, servers = processes()
    load = os.getloadavg()
    snapshot = {
        "host": run(["scutil", "--get", "ComputerName"]).strip() or os.uname().nodename,
        "user": os.environ.get("USER") or HOME.name,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "load": [round(x, 2) for x in load],
        "herdr_agents": herdr_agents(),
        "agents": agents,
        "dev_servers": servers,
        "launchd": launchd_jobs(),
        "recent_projects": recent_projects(),
        "hermes_tasks": hermes_tasks(),
    }
    print(json.dumps(snapshot))


if __name__ == "__main__":
    main()
