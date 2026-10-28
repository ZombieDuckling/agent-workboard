"""Fictional demo data for `workboard init --demo`: a small team of agents on two machines.

Timestamps are relative to now so the board looks live when it is created.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import workboard

COLUMNS = [
    {"id": "backlog", "title": "Backlog"},
    {"id": "ready", "title": "Ready"},
    {"id": "in_progress", "title": "In progress"},
    {"id": "blocked", "title": "Blocked"},
    {"id": "review", "title": "Review"},
    {"id": "done", "title": "Done"},
]

MACHINES = [
    {"id": "laptop", "title": "Laptop", "ssh": None, "icon": "laptop"},
    {"id": "buildbox", "title": "Build box", "ssh": "buildbox", "icon": "server"},
]

PROJECTS = [
    ("storefront", "Storefront", "SHOP", None, "laptop", "~/code/storefront", "high",
     "Customer-facing shop: product pages, cart and checkout. Astro front end, Postgres, Stripe test mode."),
    ("checkout", "Checkout flow", "CHK", "storefront", "laptop", "~/code/storefront/apps/checkout", "high",
     "Cart, payment and order confirmation."),
    ("admin", "Admin dashboard", "ADM", "storefront", "laptop", "~/code/storefront/apps/admin", "medium",
     "Back office for orders, stock and refunds."),
    ("mobile-app", "Mobile app", "APP", None, "buildbox", "~/code/mobile-app", "medium",
     "React Native companion app: order tracking and push notifications."),
    ("docs", "Docs site", "DOCS", None, "laptop", "~/code/docs", "low",
     "Public developer docs and changelog."),
    ("infra", "Infrastructure", "OPS", None, "buildbox", "~/code/infra", "high",
     "Terraform, CI runners, backups and monitoring."),
    ("data-pipeline", "Data pipeline", "ETL", None, "buildbox", "~/code/data-pipeline", "medium",
     "Nightly sales export and analytics warehouse loads."),
]

WORKSPACES = [
    {"id": "storefront", "title": "Storefront", "project_ids": ["storefront"]},
    {"id": "platform", "title": "Platform", "project_ids": ["infra", "data-pipeline"]},
    {"id": "apps", "title": "Apps & docs", "project_ids": ["mobile-app", "docs"]},
]

# (project, title, column, priority, agent, machine, minutes ago, notes)
CARDS = [
    ("checkout", "Apple Pay button on the payment step", "in_progress", "high", "claude-checkout", "laptop", 4,
     "Add Apple Pay through Stripe Payment Request.\n\n- [x] domain verification file\n- [ ] button on `/pay`\n- [ ] e2e test in Safari"),
    ("checkout", "Cart total rounds wrong with 3+ discount codes", "review", "urgent", "codex-review", "laptop", 25,
     "Repro: three 10% codes on a 19.99 cart shows 14.57, Stripe charges 14.58.\n\nFix in `applyDiscounts()`: round once at the end, not per code."),
    ("checkout", "Guest checkout without an account", "ready", "medium", "claude-checkout", "laptop", 300,
     "Let guests pay with only an email; offer account creation on the confirmation page."),
    ("checkout", "Choose the Stripe account for test payments", "blocked", "high", "claude-checkout", "laptop", 95,
     "Two Stripe accounts exist (personal and company). Waiting for the owner to pick one."),
    ("admin", "Refund button for partially shipped orders", "in_progress", "medium", "cursor-admin", "laptop", 12,
     "Refund only the unshipped lines; show the amount before confirming."),
    ("admin", "Low-stock alert email", "backlog", "low", "cursor-admin", "laptop", 2000,
     "Daily email when any SKU drops below its reorder point."),
    ("storefront", "Product page Lighthouse score under 80 on mobile", "in_progress", "high", "opencode-perf", "laptop", 8,
     "LCP is 4.1 s on 4G. Hero image is 1.8 MB PNG; switch to AVIF and preload it."),
    ("storefront", "Sitemap misses category pages", "done", "medium", "codex-review", "laptop", 1500,
     "Added categories to `sitemap.xml.ts`; verified in Search Console."),
    ("storefront", "Cookie banner blocks the cart button on small screens", "done", "high", "cursor-admin", "laptop", 2900,
     "Moved the banner to the top on widths under 400 px."),
    ("mobile-app", "Push notification when an order ships", "in_progress", "medium", "claude-mobile", "buildbox", 18,
     "Expo push tokens stored per user; webhook from the shipping provider triggers the send."),
    ("mobile-app", "App Store screenshots for 6.7-inch iPhone", "blocked", "low", "claude-mobile", "buildbox", 240,
     "Needs final copy for the five screenshot captions."),
    ("mobile-app", "Crash on Android 12 when opening order history", "ready", "urgent", "claude-mobile", "buildbox", 60,
     "Sentry shows `NullPointerException` in `OrderList` when the list is empty."),
    ("docs", "Document the webhooks API", "review", "medium", "opencode-docs", "laptop", 45,
     "Event list, payload examples and retry policy."),
    ("docs", "Changelog page for v2.3", "done", "low", "opencode-docs", "laptop", 4000, "Published."),
    ("infra", "Nightly database backups to object storage", "in_progress", "high", "opencode-infra", "buildbox", 6,
     "pg_dump at 02:00, 30-day retention, restore test every Sunday."),
    ("infra", "Rotate the CI deploy key", "blocked", "urgent", "opencode-infra", "buildbox", 130,
     "The old key is in three repos. Needs admin access to the GitHub org."),
    ("infra", "Uptime alerts to the team chat", "done", "medium", "opencode-infra", "buildbox", 3100,
     "Checks every minute; alerts after two failures."),
    ("infra", "Move CI runners to the build box", "backlog", "medium", "opencode-infra", "buildbox", 5000,
     "Self-hosted runners would cut CI time roughly in half."),
    ("data-pipeline", "Sales export fails on orders with emoji in the address", "in_progress", "high", "codex-etl", "buildbox", 15,
     "CSV writer opens the file without `encoding=\"utf-8\"`."),
    ("data-pipeline", "Daily revenue dashboard", "ready", "medium", "codex-etl", "buildbox", 700,
     "Revenue, orders and average basket per day, from the warehouse."),
    ("data-pipeline", "Deduplicate customers by email", "backlog", "low", "codex-etl", "buildbox", 6000,
     "Merge duplicate customer rows; keep the oldest id."),
]

COMMENTS = {
    "Cart total rounds wrong with 3+ discount codes": [
        ("owner", 40, "Does this also affect the order confirmation email?"),
        ("codex-review", 25, "Yes, the email used the same per-code rounding. Both now call `roundTotal()` once. Added a test with three codes."),
    ],
    "Product page Lighthouse score under 80 on mobile": [
        ("opencode-perf", 30, "Switched the hero to AVIF (1.8 MB to 140 KB). LCP is now **2.2 s**. Next: defer the reviews widget."),
    ],
    "Apple Pay button on the payment step": [
        ("owner", 20, "Keep the existing card form as the default; Apple Pay is an extra button."),
        ("claude-checkout", 4, "Done that way. Button only shows when `canMakePayment()` is true."),
    ],
    "Sales export fails on orders with emoji in the address": [
        ("codex-etl", 15, "Reproduced with an address containing a house emoji. Fix is one line; adding a regression test."),
    ],
}

NEEDS = {
    "Choose the Stripe account for test payments": [
        ("claude-checkout", 95, "Which Stripe account should test payments use?\n\n- **Company**: matches production, but needs an admin to create a restricted key.\n- **Personal**: works today.\n\nI will wire up whichever you pick."),
    ],
    "Rotate the CI deploy key": [
        ("opencode-infra", 130, "Please give me admin on the GitHub org for 30 minutes, or add the new deploy key yourself (public key is in the ticket)."),
    ],
    "App Store screenshots for 6.7-inch iPhone": [
        ("claude-mobile", 240, "Five captions needed, up to 30 characters each. Draft:\n\n1. Track every order\n2. Know when it ships\n3. Reorder in one tap\n4. Your receipts in one place\n5. Help when you need it"),
    ],
}


def _iso(now: datetime, minutes_ago: float) -> str:
    return (now - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build(now: datetime | None = None) -> tuple[dict, dict, dict]:
    """Return (board, snapshots, debt) dictionaries."""
    now = now or datetime.now(timezone.utc)
    owner = workboard.OWNER
    projects = [
        {"id": pid, "title": title, "key": key, "parent_id": parent, "machine": machine, "path": path,
         "priority": prio, "status": "active", "description": desc, "herdr_workspaces": [pid if not parent else parent]}
        for pid, title, key, parent, machine, path, prio, desc in PROJECTS
    ]
    cards = []
    for i, (pid, title, column, prio, agent, machine, ago, notes) in enumerate(CARDS):
        created = ago + 600 + i * 37
        activity = [{"at": _iso(now, created), "agent": agent, "type": "created"}]
        if column != "backlog":
            activity.append({"at": _iso(now, ago + 5), "agent": agent, "type": "moved", "from": "ready", "to": column})
        for who, mins, text in COMMENTS.get(title, []):
            activity.append({"at": _iso(now, mins), "agent": owner if who == "owner" else who, "type": "comment", "text": text})
        needs = []
        for j, (who, mins, text) in enumerate(NEEDS.get(title, [])):
            needs.append({"id": f"need{i:02d}{j}", "text": text, "agent": who, "at": _iso(now, mins), "status": "open",
                          "answer": None, "answered_at": None, "answered_by": None, "resolved_at": None})
            activity.append({"at": _iso(now, mins), "agent": who, "type": "need", "need_id": f"need{i:02d}{j}", "text": text})
        activity.sort(key=lambda e: e["at"])
        cards.append({
            "id": f"demo{i:04d}", "project_id": pid, "title": title, "column": column, "priority": prio,
            "owner_agent": agent, "last_agent": activity[-1]["agent"], "machine": machine, "subproject": None,
            "tags": [], "links": [], "notes": notes, "created_at": activity[0]["at"], "updated_at": activity[-1]["at"],
            "activity": activity, "attachments": [], "needs": needs,
        })
    board = {"version": 1, "updated_at": _iso(now, 0), "columns": COLUMNS, "machines": MACHINES,
             "projects": projects, "cards": cards, "workspaces": WORKSPACES}

    def agent(ws, name, kind, status, title, cwd, pane):
        return {"workspace": ws, "name": name, "pane_id": pane, "agent": kind, "status": status, "title": title, "cwd": cwd}

    stamp = now.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    snapshots = {
        "laptop": {"synced_at": _iso(now, 1), "data": {
            "host": "Laptop", "user": "you", "collected_at": stamp, "load": [2.1, 1.8, 1.6],
            "herdr_agents": [
                agent("storefront", "claude-checkout", "claude", "working", "Apple Pay button", "~/code/storefront/apps/checkout", "w1:p1"),
                agent("storefront", "cursor-admin", "cursor", "working", "Partial refunds", "~/code/storefront/apps/admin", "w1:p2"),
                agent("storefront", "opencode-perf", "opencode", "working", "Product page LCP", "~/code/storefront", "w1:p3"),
                agent("storefront", "codex-review", "codex", "idle", "Waiting for review", "~/code/storefront", "w1:p4"),
                agent("docs", "opencode-docs", "opencode", "idle", "Webhooks docs in review", "~/code/docs", "w2:p1"),
            ],
            "agents": [{"agent": "claude", "cwd": "~/code/storefront/apps/checkout", "count": 1},
                       {"agent": "opencode", "cwd": "~/code/storefront", "count": 2}],
            "dev_servers": [{"uptime": "02:14:09", "command": "astro dev", "cwd": "~/code/storefront"},
                            {"uptime": "00:41:52", "command": "vite", "cwd": "~/code/storefront/apps/admin"}],
            "launchd": [{"label": "dev.workboard.web", "running": True, "last_exit": 0},
                        {"label": "dev.workboard.sync", "running": False, "last_exit": 0}],
            "recent_projects": [
                {"path": "~/code/storefront", "modified": _iso(now, 4)[:16].replace("T", " "), "branch": "feat/apple-pay", "last_commit": "Add Apple Pay domain file", "dirty_files": 3},
                {"path": "~/code/docs", "modified": _iso(now, 45)[:16].replace("T", " "), "branch": "main", "last_commit": "Webhooks: retry policy", "dirty_files": 0},
            ],
            "hermes_tasks": [],
        }},
        "buildbox": {"synced_at": _iso(now, 1), "data": {
            "host": "Build box", "user": "you", "collected_at": stamp, "load": [5.4, 4.9, 4.2],
            "herdr_agents": [
                agent("infra", "opencode-infra", "opencode", "working", "Nightly backups", "~/code/infra", "w1:p1"),
                agent("data-pipeline", "codex-etl", "codex", "working", "UTF-8 sales export", "~/code/data-pipeline", "w2:p1"),
                agent("mobile-app", "claude-mobile", "claude", "working", "Ship notifications", "~/code/mobile-app", "w3:p1"),
            ],
            "agents": [{"agent": "codex", "cwd": "~/code/data-pipeline", "count": 1}],
            "dev_servers": [{"uptime": "05:02:33", "command": "expo start", "cwd": "~/code/mobile-app"}],
            "launchd": [],
            "recent_projects": [
                {"path": "~/code/infra", "modified": _iso(now, 6)[:16].replace("T", " "), "branch": "backups", "last_commit": "pg_dump job and retention", "dirty_files": 2},
                {"path": "~/code/data-pipeline", "modified": _iso(now, 15)[:16].replace("T", " "), "branch": "main", "last_commit": "Warehouse load: add returns", "dirty_files": 1},
            ],
            "hermes_tasks": [],
        }},
    }

    def debt_entry(loc, src, tests, todo, fixme, supp, big, missing):
        minutes = todo * 15 + fixme * 30 + supp * 10 + big * 120 + missing * 30
        per_kloc = round(minutes / (loc / 1000), 1)
        grade = next(g for limit, g in ((30, "A"), (60, "B"), (120, "C"), (240, "D"), (float("inf"), "E")) if per_kloc < limit)
        return {"loc": loc, "source_files": src, "test_files": tests,
                "markers": {"TODO": todo, "XXX": 0, "FIXME": fixme, "HACK": 0}, "suppressions": supp,
                "big_files": big, "big_file_list": [], "missing_tests": missing, "marker_samples": [],
                "minutes": minutes, "minutes_by_kind": {"markers": todo * 15 + fixme * 30, "suppressions": supp * 10,
                                                        "big_files": big * 120, "missing_tests": missing * 30},
                "per_kloc": per_kloc, "grade": grade, "scanned_at": _iso(now, 20)}

    debt_projects = {
        "checkout": debt_entry(18400, 96, 31, 6, 2, 4, 1, 0),
        "admin": debt_entry(22100, 120, 12, 14, 5, 11, 3, 12),
        "mobile-app": debt_entry(31800, 188, 20, 22, 6, 18, 4, 18),
        "docs": debt_entry(6200, 40, 4, 3, 0, 1, 0, 4),
        "infra": debt_entry(9300, 61, 14, 2, 1, 3, 0, 0),
        "data-pipeline": debt_entry(12700, 58, 5, 9, 4, 6, 2, 7),
    }
    history = {
        pid: [{"minutes": int(e["minutes"] * f), "loc": e["loc"], "per_kloc": round(e["per_kloc"] * f, 1), "grade": e["grade"],
               "date": (now - timedelta(days=d)).strftime("%Y-%m-%d")} for d, f in ((6, 1.12), (3, 1.05), (0, 1.0))]
        for pid, e in debt_projects.items()
    }
    debt = {"scanned_at": _iso(now, 20), "projects": debt_projects, "history": history, "errors": {}}
    return board, snapshots, debt
