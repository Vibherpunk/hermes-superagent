"""Tests for Superagent Attention Panel API endpoints and view-assembly readers.

Verifies:
- Shared guard: when superagent.enabled is false, all attention endpoints return 404.
- GET /attention/overview shape + needs_person count == list length.
- GET /attention/overview includes dropped_and_failed_replies when fixture rows exist.
- GET /attention/threads listing distinct thread keys with trigger/decision summaries.
- GET /attention/thread/{thread_key} chronological timeline.
- GET /attention/decisions with outcome filtering and ordering.
- GET /attention/tickets listing and stage filtering.
- GET /attention/ticket/{ticket_id} detail retrieval and 404 for missing ticket.
- GET /attention/settings config view (mode, enabled, paused, surfaces).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import orchestrator_reads


# ---------------------------------------------------------------------------
# Fixtures mirroring tests/plugins/test_kanban_dashboard_plugin.py
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_attention_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home: Path):
    """TestClient attached to a fresh FastAPI instance mounting the kanban router."""
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _enable_superagent(home: Path, custom_yaml: str | None = None) -> None:
    """Helper to enable superagent in config.yaml."""
    cfg_file = home / "config.yaml"
    if custom_yaml is not None:
        cfg_file.write_text(custom_yaml)
    else:
        cfg_file.write_text(
            "superagent:\n"
            "  enabled: true\n"
            "  accept_mode: observe\n"
        )


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    title: str = "Test Task",
    status: str = "todo",
    block_kind: str | None = None,
    created_at: int = 1000,
    priority: int = 0,
    assignee: str | None = None,
    ticket_kind: str | None = None,
    mandate_id: int | None = None,
) -> None:
    """Insert a task row directly into the tasks table."""
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, block_kind, created_at, priority,
            assignee, ticket_kind, mandate_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            title,
            status,
            block_kind,
            created_at,
            priority,
            assignee,
            ticket_kind,
            mandate_id,
        ),
    )


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_disabled_superagent_returns_404_on_all_endpoints(client: TestClient):
    """When superagent.enabled is false (or not configured), every attention endpoint returns 404."""
    endpoints = [
        "/api/plugins/kanban/attention/overview",
        "/api/plugins/kanban/attention/threads",
        "/api/plugins/kanban/attention/thread/th_test",
        "/api/plugins/kanban/attention/decisions",
        "/api/plugins/kanban/attention/tickets",
        "/api/plugins/kanban/attention/ticket/t_test",
        "/api/plugins/kanban/attention/settings",
    ]
    for ep in endpoints:
        r = client.get(ep)
        assert r.status_code == 404, f"expected 404 for disabled superagent on {ep}, got {r.status_code}"
        assert "superagent is disabled" in r.text

    post_endpoints = [
        "/api/plugins/kanban/attention/ticket/t_test/stop",
        "/api/plugins/kanban/attention/ticket/t_test/retry",
        "/api/plugins/kanban/attention/decision/1/dismiss",
        "/api/plugins/kanban/attention/trigger/trg_test/reevaluate",
        "/api/plugins/kanban/attention/thread/th_test/unsubscribe-all",
        "/api/plugins/kanban/attention/pause",
        "/api/plugins/kanban/attention/resume",
        "/api/plugins/kanban/attention/processing",
    ]
    for ep in post_endpoints:
        r = client.post(ep)
        assert r.status_code == 404, f"expected 404 for disabled superagent on POST {ep}, got {r.status_code}"
        assert "superagent is disabled" in r.text


def test_explicitly_disabled_superagent_returns_404(client: TestClient, kanban_home: Path):
    """When superagent.enabled is explicitly false, endpoints still return 404."""
    (kanban_home / "config.yaml").write_text("superagent:\n  enabled: false\n")
    endpoints = [
        "/api/plugins/kanban/attention/overview",
        "/api/plugins/kanban/attention/threads",
        "/api/plugins/kanban/attention/thread/th_test",
        "/api/plugins/kanban/attention/decisions",
        "/api/plugins/kanban/attention/tickets",
        "/api/plugins/kanban/attention/ticket/t_test",
        "/api/plugins/kanban/attention/settings",
    ]
    for ep in endpoints:
        r = client.get(ep)
        assert r.status_code == 404

    post_endpoints = [
        "/api/plugins/kanban/attention/ticket/t_test/stop",
        "/api/plugins/kanban/attention/ticket/t_test/retry",
        "/api/plugins/kanban/attention/decision/1/dismiss",
        "/api/plugins/kanban/attention/trigger/trg_test/reevaluate",
        "/api/plugins/kanban/attention/thread/th_test/unsubscribe-all",
        "/api/plugins/kanban/attention/pause",
        "/api/plugins/kanban/attention/resume",
        "/api/plugins/kanban/attention/processing",
    ]
    for ep in post_endpoints:
        r = client.post(ep)
        assert r.status_code == 404


def test_overview_shape_and_needs_person_count(client: TestClient, kanban_home: Path):
    """GET /attention/overview: verify pinned shape and needs_person count == list length."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        # 1. person/answer: blocked + needs_input
        _insert_task(conn, "t1", title="Needs Answer", status="blocked", block_kind="needs_input", created_at=100)
        # 2. person/blocked: failed status
        _insert_task(conn, "t2", title="Task Failed", status="failed", created_at=200)
        # 3. working
        _insert_task(conn, "t3", title="In Progress", status="running", created_at=300)
        # 4. queued
        _insert_task(conn, "t4", title="Ready Task", status="ready", created_at=400)
        # 5. closed
        _insert_task(conn, "t5", title="Done Task", status="done", created_at=500)
        conn.commit()

    r = client.get("/api/plugins/kanban/attention/overview")
    assert r.status_code == 200, r.text
    data = r.json()

    # Verify root shape
    assert "health" in data
    assert "flow_counts_24h" in data
    assert "needs_person" in data
    assert "needs_person_count" in data
    assert "stages" in data
    assert "tickets_by_stage" in data

    # Verify needs_person count invariant
    assert data["needs_person_count"] == len(data["needs_person"])
    assert data["needs_person_count"] == 2

    # Verify stage grouping
    stages = data["stages"]
    assert len(stages["person/answer"]) == 1
    assert stages["person/answer"][0]["id"] == "t1"
    assert len(stages["person/blocked"]) == 1
    assert stages["person/blocked"][0]["id"] == "t2"
    assert len(stages["working"]) == 1
    assert stages["working"][0]["id"] == "t3"
    assert len(stages["queued"]) == 1
    assert stages["queued"][0]["id"] == "t4"
    assert len(stages["closed"]) == 1
    assert stages["closed"][0]["id"] == "t5"

    # No dropped or failed reply rows -> dropped_and_failed_replies section must not appear
    assert "dropped_and_failed_replies" not in data


def test_overview_dropped_failed_replies_section_appears(client: TestClient, kanban_home: Path):
    """A dropped/failed replies section appears in overview when fixture rows exist (via dropped_and_failed_replies)."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        conn.execute(
            """
            INSERT INTO sa_outbox (source, purpose, body, status, attempts, created_at)
            VALUES
                ('slack', 'reply', 'dropped-msg-1', 'dropped', 1, 100),
                ('slack', 'reply', 'failed-msg-3-att', 'failed', 3, 200),
                ('slack', 'reply', 'transient-fail', 'failed', 1, 300),
                ('slack', 'reply', 'sent-msg', 'sent', 1, 400)
            """,
        )
        conn.commit()

    r = client.get("/api/plugins/kanban/attention/overview")
    assert r.status_code == 200, r.text
    data = r.json()

    # The section must appear
    assert "dropped_and_failed_replies" in data
    dropped_list = data["dropped_and_failed_replies"]
    assert len(dropped_list) == 2

    bodies = [d["body"] for d in dropped_list]
    assert "dropped-msg-1" in bodies
    assert "failed-msg-3-att" in bodies
    assert "transient-fail" not in bodies
    assert "sent-msg" not in bodies


def test_thread_timeline_chronological_entries(client: TestClient, kanban_home: Path):
    """GET /attention/thread/{thread_key} returns entries in strict chronological order."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        conn.execute(
            """
            INSERT INTO sa_triggers (id, source, scope, thread_key, kind, body, idempotency_key, created_at)
            VALUES ('trig_1', 'slack', 'general', 'th_chrono', 'message', 'Initial question', 'ik_chrono', 100)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
            VALUES ('th_chrono', 't_chrono', 0, 150)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_inputs (ticket_id, kind, body, created_at)
            VALUES ('t_chrono', 'instruction', 'Human follow-up answer', 200)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_decisions (thread_key, ticket_id, outcome, strength, policy_version, created_at)
            VALUES ('th_chrono', 't_chrono', 'accept', 'high', '1.0', 300)
            """,
        )
        conn.commit()

    r = client.get("/api/plugins/kanban/attention/thread/th_chrono")
    assert r.status_code == 200, r.text
    timeline = r.json()

    assert len(timeline) == 3
    # Check chronological ordering: oldest first
    assert [entry["ts"] for entry in timeline] == [100, 200, 300]
    assert timeline[0]["kind"] == "trigger"
    assert "Trigger [slack] message:" in timeline[0]["summary"]
    assert timeline[1]["kind"] == "input"
    assert "Input [instruction] ticket=t_chrono:" in timeline[1]["summary"]
    assert timeline[2]["kind"] == "decision"
    assert "Decision outcome=accept" in timeline[2]["summary"]


def test_threads_endpoint_listing(client: TestClient, kanban_home: Path):
    """GET /attention/threads lists distinct thread_keys with latest trigger/decision summaries."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        # Thread 1: trigger only
        conn.execute(
            """
            INSERT INTO sa_triggers (id, source, scope, thread_key, kind, body, idempotency_key, created_at)
            VALUES ('trig_a', 'discord', 'dev', 'th_alpha', 'event', 'Alpha trigger', 'ik_alpha', 100)
            """,
        )
        # Thread 2: trigger and later decision
        conn.execute(
            """
            INSERT INTO sa_triggers (id, source, scope, thread_key, kind, body, idempotency_key, created_at)
            VALUES ('trig_b', 'slack', 'ops', 'th_beta', 'alert', 'Beta trigger', 'ik_beta', 200)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_decisions (thread_key, outcome, strength, policy_version, created_at)
            VALUES ('th_beta', 'reject', 'definite', '1.0', 300)
            """,
        )
        conn.commit()

    r = client.get("/api/plugins/kanban/attention/threads")
    assert r.status_code == 200, r.text
    thread_list = r.json()

    assert len(thread_list) == 2
    # Ordered by most recent activity: th_beta at 300, th_alpha at 100
    assert thread_list[0]["thread_key"] == "th_beta"
    assert thread_list[0]["latest_activity_at"] == 300
    assert "Decision outcome=reject" in thread_list[0]["latest_summary"]
    assert "Trigger [slack] alert" in (thread_list[0]["latest_trigger_summary"] or "")

    assert thread_list[1]["thread_key"] == "th_alpha"
    assert thread_list[1]["latest_activity_at"] == 100
    assert "Trigger [discord] event" in thread_list[1]["latest_summary"]


def test_decisions_outcome_filter(client: TestClient, kanban_home: Path):
    """GET /attention/decisions supports filtering by outcome."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        conn.execute(
            """
            INSERT INTO sa_decisions (thread_key, outcome, strength, policy_version, created_at)
            VALUES
                ('th1', 'accept', 'strong', '1.0', 10),
                ('th2', 'reject', 'weak', '1.0', 20),
                ('th3', 'accept', 'strong', '1.0', 30),
                ('th4', 'unavailable', 'none', '1.0', 40),
                ('th5', 'accept', 'strong', '1.0', 50)
            """,
        )
        conn.commit()

    # 1. All decisions
    r = client.get("/api/plugins/kanban/attention/decisions")
    assert r.status_code == 200
    all_decs = r.json()
    assert len(all_decs) == 5
    # Newest first
    assert [d["created_at"] for d in all_decs] == [50, 40, 30, 20, 10]

    # 2. Filter by accept
    r = client.get("/api/plugins/kanban/attention/decisions?outcome=accept")
    assert r.status_code == 200
    accept_decs = r.json()
    assert len(accept_decs) == 3
    assert all(d["outcome"] == "accept" for d in accept_decs)

    # 3. Filter by reject
    r = client.get("/api/plugins/kanban/attention/decisions?outcome=reject")
    assert r.status_code == 200
    reject_decs = r.json()
    assert len(reject_decs) == 1
    assert reject_decs[0]["created_at"] == 20

    # 4. Limit parameter
    r = client.get("/api/plugins/kanban/attention/decisions?limit=2")
    assert r.status_code == 200
    limit_decs = r.json()
    assert len(limit_decs) == 2


def test_ticket_detail_success_and_404(client: TestClient, kanban_home: Path):
    """GET /attention/ticket/{ticket_id}: 404 when missing, full detail when present."""
    _enable_superagent(kanban_home)

    # Missing ticket -> 404
    r_missing = client.get("/api/plugins/kanban/attention/ticket/t_nonexistent")
    assert r_missing.status_code == 404
    assert "not found" in r_missing.text

    # Seed existing ticket with mandate, inputs, and outbox
    with kbc.connect_closing() as conn:
        _insert_task(conn, "t_detail", title="Detail Test Task", status="running", created_at=1000)
        conn.execute(
            """
            INSERT INTO sa_mandates (ticket_id, policy_version, scope, strength, created_at)
            VALUES ('t_detail', 'v2', 'project-x', 'enforced', 1010)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_inputs (ticket_id, kind, body, created_at)
            VALUES ('t_detail', 'prompt', 'Task instructions', 1020)
            """,
        )
        conn.execute(
            """
            INSERT INTO sa_outbox (ticket_id, source, purpose, body, status, created_at)
            VALUES ('t_detail', 'slack', 'reply', 'Progress update', 'sent', 1030)
            """,
        )
        conn.commit()

    r_found = client.get("/api/plugins/kanban/attention/ticket/t_detail")
    assert r_found.status_code == 200, r_found.text
    detail = r_found.json()

    assert detail["id"] == "t_detail"
    assert detail["title"] == "Detail Test Task"
    assert detail["mandate"] is not None
    assert detail["mandate"]["ticket_id"] == "t_detail"
    assert len(detail["inputs"]) == 1
    assert detail["inputs"][0]["body"] == "Task instructions"
    assert len(detail["outbox"]) == 1
    assert detail["outbox"][0]["body"] == "Progress update"


def test_tickets_listing_and_filtering(client: TestClient, kanban_home: Path):
    """GET /attention/tickets supports listing all tickets and filtering by stage."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        _insert_task(conn, "t_work", title="Running Task", status="running", created_at=100)
        _insert_task(conn, "t_block", title="Blocked Review", status="blocked", block_kind="review", created_at=200)
        _insert_task(conn, "t_closed", title="Closed Task", status="done", created_at=300)
        conn.commit()

    # All tickets
    r_all = client.get("/api/plugins/kanban/attention/tickets")
    assert r_all.status_code == 200
    all_tickets = r_all.json()
    assert len(all_tickets) == 3

    # Filter by specific stage
    r_working = client.get("/api/plugins/kanban/attention/tickets?stage=working")
    assert r_working.status_code == 200
    working_tickets = r_working.json()
    assert len(working_tickets) == 1
    assert working_tickets[0]["id"] == "t_work"

    # Filter by person stage category
    r_person = client.get("/api/plugins/kanban/attention/tickets?stage=person")
    assert r_person.status_code == 200
    person_tickets = r_person.json()
    assert len(person_tickets) == 1
    assert person_tickets[0]["id"] == "t_block"


def test_settings_endpoint_view(client: TestClient, kanban_home: Path):
    """GET /attention/settings returns superagent configuration view (mode, enabled, paused, surfaces)."""
    _enable_superagent(
        kanban_home,
        custom_yaml=(
            "superagent:\n"
            "  enabled: true\n"
            "  accept_mode: auto\n"
            "  sources:\n"
            "    slack:\n"
            "      allowlist: ['#general']\n"
            "    github:\n"
            "      repository: 'owner/repo'\n"
        ),
    )

    with kbc.connect_closing() as conn:
        conn.execute("INSERT OR REPLACE INTO sa_meta (key, value) VALUES ('paused', 'true')")
        conn.commit()

    r = client.get("/api/plugins/kanban/attention/settings")
    assert r.status_code == 200, r.text
    settings = r.json()

    assert settings["enabled"] is True
    assert settings["mode"] == "auto"
    assert settings["paused"] is True
    assert "slack" in settings["surfaces"]
    assert "github" in settings["surfaces"]


# ---------------------------------------------------------------------------
# POST Action Endpoint Tests (Phase 5.3 POST)
# ---------------------------------------------------------------------------

def test_post_ticket_stop(client: TestClient, kanban_home: Path):
    """POST /attention/ticket/{ticket_id}/stop stops ticket and verifies count invariants."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        _insert_task(conn, "t_stop", title="Needs Answer", status="blocked", block_kind="needs_input")
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_mandates (ticket_id, policy_version, scope, strength, basis_json, status, created_at)
                VALUES ('t_stop', 'v1', 'main', 'explicit', '{}', 'active', 1000)
                """
            )
            mid = cur.lastrowid
            conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = 't_stop'", (mid,))

    # Before stop: 1 ticket in needs_person
    r_before = client.get("/api/plugins/kanban/attention/overview")
    assert r_before.status_code == 200
    ov_before = r_before.json()
    assert ov_before["needs_person_count"] == len(ov_before["needs_person"]) == 1
    assert len(ov_before["stages"]["closed"]) == 0

    # Happy path POST stop
    r_stop = client.post("/api/plugins/kanban/attention/ticket/t_stop/stop", json={"reason": "User stopped"})
    assert r_stop.status_code == 200, r_stop.text
    res = r_stop.json()
    assert res.get("ok") is True
    assert res.get("status") == "stopped"

    # After stop: done-criterion 2 count invariants (needs_person == 0, closed == 1)
    r_after = client.get("/api/plugins/kanban/attention/overview")
    assert r_after.status_code == 200
    ov_after = r_after.json()
    assert ov_after["needs_person_count"] == len(ov_after["needs_person"]) == 0
    assert len(ov_after["stages"]["closed"]) == 1
    assert ov_after["stages"]["closed"][0]["id"] == "t_stop"
    assert ov_after["tickets_by_stage"]["closed"][0]["id"] == "t_stop"

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, "t_stop")
        assert task is not None
        assert task.status == "done"
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow is not None
        assert mrow["status"] == "withdrawn"

    # Missing ticket returns 404
    r_missing = client.post("/api/plugins/kanban/attention/ticket/t_nonexistent/stop")
    assert r_missing.status_code == 404

    # Body-provided ticket_id fallback works
    r_body = client.post("/api/plugins/kanban/attention/ticket/stop", json={"ticket_id": "t_stop"})
    assert r_body.status_code == 200


def test_post_ticket_retry(client: TestClient, kanban_home: Path):
    """POST /attention/ticket/{ticket_id}/retry retries blocked/running ticket and verifies count invariants."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        _insert_task(conn, "t_retry", title="Blocked Review", status="blocked", block_kind="review")

    # Before retry: 1 ticket in needs_person
    r_before = client.get("/api/plugins/kanban/attention/overview")
    ov_before = r_before.json()
    assert ov_before["needs_person_count"] == len(ov_before["needs_person"]) == 1
    assert len(ov_before["stages"]["queued"]) == 0

    # Happy path POST retry
    r_retry = client.post("/api/plugins/kanban/attention/ticket/t_retry/retry", json={"reason": "Retry review"})
    assert r_retry.status_code == 200, r_retry.text
    res = r_retry.json()
    assert res.get("ok") is True
    assert res.get("status") == "ready"
    assert res.get("retried") is True

    # After retry: needs_person == 0, queued == 1
    r_after = client.get("/api/plugins/kanban/attention/overview")
    ov_after = r_after.json()
    assert ov_after["needs_person_count"] == len(ov_after["needs_person"]) == 0
    assert len(ov_after["stages"]["queued"]) == 1
    assert ov_after["stages"]["queued"][0]["id"] == "t_retry"

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, "t_retry")
        assert task is not None
        assert task.status == "ready"

    # Refusal on terminal ticket (status='done')
    with kbc.connect_closing() as conn:
        _insert_task(conn, "t_done", title="Completed Task", status="done")
    r_done = client.post("/api/plugins/kanban/attention/ticket/t_done/retry")
    assert r_done.status_code == 400

    # Missing ticket returns 404
    r_missing = client.post("/api/plugins/kanban/attention/ticket/t_missing/retry")
    assert r_missing.status_code == 404


def test_post_decision_dismiss(client: TestClient, kanban_home: Path):
    """POST /attention/decision/{decision_id}/dismiss dismisses an unhandled decision."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES ('th_dec', '[]', 'unavailable', 'not_admitted', 0, 0, '[]', '{}', 'v1', NULL, 0, 1000)
                """
            )
            dec_id = cur.lastrowid

    # Happy path dismiss
    r_dismiss = client.post(f"/api/plugins/kanban/attention/decision/{dec_id}/dismiss")
    assert r_dismiss.status_code == 200, r_dismiss.text
    res = r_dismiss.json()
    assert res.get("ok") is True
    assert res.get("dismissed") is True
    assert res.get("decision_id") == dec_id

    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT dismissed FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
        assert row is not None
        assert row["dismissed"] == 1

    # Second dismiss refused (already dismissed) -> 400
    r_second = client.post(f"/api/plugins/kanban/attention/decision/{dec_id}/dismiss")
    assert r_second.status_code == 400

    # Missing decision -> 404
    r_missing = client.post("/api/plugins/kanban/attention/decision/99999/dismiss")
    assert r_missing.status_code == 404


def test_post_trigger_reevaluate(client: TestClient, kanban_home: Path):
    """POST /attention/trigger/{trigger_id}/reevaluate requeues a trigger and updates health count."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_triggers (
                    id, idempotency_key, source, scope, thread_key, actor_source_id,
                    actor_is_bot, addressed, kind, body, disposition, eval_state,
                    eval_requeues, meta_json, created_at
                ) VALUES ('trg_re', 'ik_re', 'telegram', 'main', 'th_re', 'alice', 0, 1, 'msg', 'body', 'evaluated', 'done', 0, '{}', 1000)
                """
            )

    # Before: eval_queued count == 0
    r_before = client.get("/api/plugins/kanban/attention/overview")
    assert r_before.json()["health"]["eval_queued"] == 0

    # Happy path reevaluate
    r_req = client.post("/api/plugins/kanban/attention/trigger/trg_re/reevaluate")
    assert r_req.status_code == 200, r_req.text
    res = r_req.json()
    assert res.get("ok") is True
    assert res.get("eval_state") == "queued"
    assert res.get("requeued") is True

    # After: eval_queued count == 1
    r_after = client.get("/api/plugins/kanban/attention/overview")
    assert r_after.json()["health"]["eval_queued"] == 1

    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT eval_state, eval_requeues FROM sa_triggers WHERE id = 'trg_re'").fetchone()
        assert row is not None
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 1

    # Refusal while evaluating -> 400
    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE sa_triggers SET eval_state = 'evaluating' WHERE id = 'trg_re'")
    r_eval = client.post("/api/plugins/kanban/attention/trigger/trg_re/reevaluate")
    assert r_eval.status_code == 400

    # Missing trigger -> 404
    r_missing = client.post("/api/plugins/kanban/attention/trigger/trg_nonexistent/reevaluate")
    assert r_missing.status_code == 404


def test_post_thread_unsubscribe_all(client: TestClient, kanban_home: Path):
    """POST /attention/thread/{thread_key}/unsubscribe-all drops all subscriptions for thread."""
    _enable_superagent(kanban_home)

    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            conn.execute("INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at) VALUES ('th_unsub', 't1', 0, 1000)")
            conn.execute("INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at) VALUES ('th_unsub', 't2', 1, 1000)")
            conn.execute("INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at) VALUES ('th_keep', 't3', 0, 1000)")

    # Happy path unsubscribe-all
    r = client.post("/api/plugins/kanban/attention/thread/th_unsub/unsubscribe-all")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res.get("ok") is True
    assert res.get("thread_key") == "th_unsub"
    assert res.get("dropped") == 2

    with kbc.connect_closing() as conn:
        unsub_rows = conn.execute("SELECT COUNT(*) FROM sa_subscriptions WHERE thread_key = 'th_unsub'").fetchone()[0]
        assert unsub_rows == 0
        keep_rows = conn.execute("SELECT COUNT(*) FROM sa_subscriptions WHERE thread_key = 'th_keep'").fetchone()[0]
        assert keep_rows == 1

    # Idempotent second call returns dropped: 0
    r_repeat = client.post("/api/plugins/kanban/attention/thread/th_unsub/unsubscribe-all")
    assert r_repeat.status_code == 200
    assert r_repeat.json().get("dropped") == 0


def test_post_processing_pause_resume(client: TestClient, kanban_home: Path):
    """POST /attention/pause, /attention/resume, and /attention/processing toggle health state."""
    _enable_superagent(kanban_home)

    # Initial state: not paused
    r_init = client.get("/api/plugins/kanban/attention/overview")
    assert r_init.json()["health"]["paused"] is False

    # Pause
    r_pause = client.post("/api/plugins/kanban/attention/pause")
    assert r_pause.status_code == 200, r_pause.text
    assert r_pause.json().get("paused") is True

    r_ov_paused = client.get("/api/plugins/kanban/attention/overview")
    assert r_ov_paused.json()["health"]["paused"] is True
    r_set_paused = client.get("/api/plugins/kanban/attention/settings")
    assert r_set_paused.json()["paused"] is True

    # Resume
    r_resume = client.post("/api/plugins/kanban/attention/resume")
    assert r_resume.status_code == 200, r_resume.text
    assert r_resume.json().get("paused") is False

    r_ov_resumed = client.get("/api/plugins/kanban/attention/overview")
    assert r_ov_resumed.json()["health"]["paused"] is False

    # Toggle via /attention/processing with action
    r_proc_pause = client.post("/api/plugins/kanban/attention/processing", json={"action": "pause"})
    assert r_proc_pause.status_code == 200
    assert r_proc_pause.json().get("paused") is True

    r_proc_resume = client.post("/api/plugins/kanban/attention/processing", json={"paused": False})
    assert r_proc_resume.status_code == 200
    assert r_proc_resume.json().get("paused") is False


def test_post_unknown_action_endpoint_not_found(client: TestClient, kanban_home: Path):
    """Unknown action endpoints do not exist and return 404."""
    _enable_superagent(kanban_home)
    assert client.post("/api/plugins/kanban/attention/ticket/t1/cancel").status_code == 404
    assert client.post("/api/plugins/kanban/attention/unknown_action").status_code == 404
