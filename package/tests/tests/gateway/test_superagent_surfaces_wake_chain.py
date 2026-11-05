"""Surfaces Rev 3 — WRD U2b wake-marker chain, driven through the REAL path.

HTTP door (aiohttp TestClient on the real chat-completions handler) ->
_run_agent -> admit_api_steps (marker write) -> session admission FIFO ->
drain (execute_admission / execute: wake check, flag rule, turn policy) ->
_handle_message -> GatewayRunner superagent hook.

Pre-import note: this module imports hermes_bootstrap and the managed
worker at collection time so the bootstrap's interrupted-update settle
runs before the test home guard is active (same pattern as
tests/tui_gateway/conftest.py). Without it, lazily importing the
bootstrap inside a test fails in any .worktrees checkout.
"""

from __future__ import annotations

import copy
import json

import hermes_bootstrap  # noqa: F401  (collection-time settle, see docstring)
import gateway.session_managed_worker  # noqa: F401

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.run import GatewayRunner
from gateway.session_contract import SessionRef
from gateway.session_ingress import execute_admission
from hermes_cli import kanban_db_connect as kbc
from hermes_state_runtime import RuntimeStoreError, admit_session_input


SESSION_ID = "surfaces-wake-session"
API_KEY = "surfaces-test-key-000001"


def _client(api):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", api._handle_chat_completions)
    return TestClient(TestServer(app))


def _headers():
    return {
        "Authorization": "Bearer " + API_KEY,
        "X-Hermes-Session-Id": SESSION_ID,
    }


def _trigger_rows(db_path):
    with kbc.connect_closing(db_path) as conn:
        return conn.execute("SELECT * FROM sa_triggers").fetchall()


def _admission_payloads(db, session_id=SESSION_ID):
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM session_admissions"
            " WHERE target_session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _wire_hook(owner, tmp_path):
    """Point the owner runner's _handle_message at the REAL hook."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    hook_runner = object.__new__(GatewayRunner)
    hook_runner.config = {
        "superagent": {"enabled": True, "pilot_surfaces": ["api_server"]}
    }
    hook_runner._superagent_db_path = db_path
    hook_runner.session_store = owner.runner.session_store
    handled: list = []

    async def handle(event):
        handled.append(event)
        hook_runner._hm_maybe_record_superagent_trigger(event, event.source)
        from gateway.session_results import execution_result

        execution_result.get()["result"] = {"final_response": "ok", "messages": []}
        return "ok"

    owner.runner._handle_message = handle
    return db_path, handled


async def _post(client, body):
    return await client.post("/v1/chat/completions", json=body, headers=_headers())


@pytest.mark.asyncio
async def test_wake_post_marks_payload_and_records_no_trigger(api, owner, tmp_path):
    api._api_key = API_KEY
    db_path, handled = _wire_hook(owner, tmp_path)
    async with _client(api) as client:
        # Positive control FIRST: a plain user turn on this session must
        # record exactly one trigger, and its admission payload must NOT
        # carry the wake marker (this is the _run_agent sentinel pin).
        resp = await _post(client, {"messages": [{"role": "user", "content": "plain user turn"}]})
        assert resp.status == 200, await resp.text()
        rows = _trigger_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["thread_key"].endswith(SESSION_ID)
        payloads = _admission_payloads(owner.db)
        assert len(payloads) == 1
        assert "wake_notification" not in payloads[0].get("api_turn_v1", {})

        # The wake itself: delivered (200, handler ran) but records NO
        # trigger, and its admission payload carries the marker through
        # check_api_turn at admit AND drain/replay.
        resp = await _post(
            client,
            {
                "messages": [{"role": "user", "content": "result notification"}],
                "hermes_notification_category": "result",
            },
        )
        assert resp.status == 200, await resp.text()
        assert len(handled) == 2
        assert len(_trigger_rows(db_path)) == 1  # still only the control
        payloads = _admission_payloads(owner.db)
        assert len(payloads) == 2
        assert payloads[1]["api_turn_v1"].get("wake_notification") == "result"


@pytest.mark.asyncio
async def test_in_process_wake_route_no_trigger(api, owner, tmp_path):
    api._api_key = API_KEY
    db_path, handled = _wire_hook(owner, tmp_path)
    async with _client(api) as client:
        resp = await _post(client, {"messages": [{"role": "user", "content": "seed turn"}]})
        assert resp.status == 200, await resp.text()
    assert len(_trigger_rows(db_path)) == 1

    # api_server_runs.py in-process wake route: passes its category
    # explicitly into _run_agent (verified unchanged by U2b).
    # Contract: returns None; failure raises. The assertions below pin
    # the marker + no-trigger behavior of the drained turn.
    await api.run_internal_session_turn(
        session_id=SESSION_ID,
        text="wake: background job done",
        profile="default",
        notification_category="result",
    )
    assert len(_trigger_rows(db_path)) == 1
    payloads = _admission_payloads(owner.db)
    assert payloads[-1]["api_turn_v1"].get("wake_notification") == "result"


@pytest.mark.asyncio
async def test_check_api_turn_wake_notification_shape(api, owner, tmp_path):
    """check_api_turn validator (A31-4): a string wake_notification
    passes; a non-string is invalid_params (it runs at admit AND at
    drain/replay, so a shape error must fail at the door). Uses a REAL
    admitted payload read back from the admission FIFO."""
    from gateway.session_api_turn import check_api_turn

    api._api_key = API_KEY
    _wire_hook(owner, tmp_path)
    async with _client(api) as client:
        resp = await _post(client, {"messages": [{"role": "user", "content": "seed"}]})
        assert resp.status == 200, await resp.text()
    payload = _admission_payloads(owner.db)[-1]
    ref = SessionRef(owner.profile_id, SESSION_ID)
    assert check_api_turn(owner, ref, payload) is api

    bad = copy.deepcopy(payload)
    bad["api_turn_v1"]["wake_notification"] = 123
    with pytest.raises(RuntimeStoreError):
        check_api_turn(owner, ref, bad)

    good = copy.deepcopy(payload)
    good["api_turn_v1"]["wake_notification"] = "result"
    assert check_api_turn(owner, ref, good) is api


@pytest.mark.asyncio
async def test_drain_non_api_turn_payload_sets_no_flag(api, owner, tmp_path):
    """WRD U2 item (vi): an admission row whose payload is NOT an
    api_turn_v1 turn (the automation/native admission shape) drains with
    no _sa_user_turn flag set."""
    api._api_key = API_KEY
    _wire_hook(owner, tmp_path)
    async with _client(api) as client:
        resp = await _post(client, {"messages": [{"role": "user", "content": "seed"}]})
        assert resp.status == 200, await resp.text()

    row = admit_session_input(
        owner.db,
        epoch=owner.epoch,
        principal_id="api",
        session_id=SESSION_ID,
        request_id="vi-req-1",
        payload={"text": "automation-shaped admission"},
    )
    captured = {}

    async def handle(event):
        captured["event"] = event
        from gateway.session_results import execution_result

        execution_result.get()["result"] = {"final_response": "ok", "messages": []}
        return "ok"

    owner.runner._handle_message = handle
    ref = SessionRef(owner.profile_id, SESSION_ID)
    await execute_admission(owner, ref, row)
    event = captured["event"]
    assert event.internal is True
    assert getattr(event, "_sa_user_turn", False) is False
