"""Tests for deterministic creation-time assignment (DESIGN rev 4 §3, A series).

Covers A-1, A-1b, A-2, A-3, A-3b, A-4, A-5, A-6, A-7, A-8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_assignment as ka
from hermes_cli import kanban_board_config as kbcfg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


ASSIGNMENT_ON = """kanban:
  assignment:
    enabled: true
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
"""


def _cfg(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _board():
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "assignment")
    return cfg


def _hook(plan):
    def hook(conn, tid):
        ka.apply_creation_assignment(conn, tid, plan)
    return hook


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# --------------------------------------------------------------------------
# A-1 / A-1b: rule-table pins at creation
# --------------------------------------------------------------------------

def test_a1_frontier_and_cheap_classes_pinned(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    cfg = _board()
    with kbc.connect() as conn:
        for work_class, model, provider, effort in (
            ("financial", "gemini-4-argon", "custom:antigravity-bridge", "high"),
            ("production", "gemini-4-argon", "custom:antigravity-bridge", "high"),
            ("architecture", "gemini-4-argon", "custom:antigravity-bridge", "xhigh"),
            ("mechanical", "mtplx-bonsai-2-27b-optimized-speed", "custom:bonsai-local", "low"),
            ("repair", "mtplx-bonsai-2-27b-optimized-speed", "custom:bonsai-local", "low"),
        ):
            plan = ka.resolve_assignment(work_class, board_config=cfg,
                                         explicit=ka.ExplicitOverrides(), assignee=None)
            tid = kb.create_task(conn, title=work_class, assignee=None,
                                 work_class=work_class, assignment_hook=_hook(plan))
            task = kb.get_task(conn, tid)
            assert task.status in ("ready", "todo")
            assert task.model_override == model, work_class
            assert task.provider_override == provider, work_class
            assert task.reasoning_effort == effort, work_class
            applied = _events(conn, tid, "assignment_applied")
            assert len(applied) == 1
            assert applied[0].payload["source"] == "rule"
            assert applied[0].payload["work_class"] == work_class


def test_a1b_no_pin_classes_leave_overrides_null(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    cfg = _board()
    with kbc.connect() as conn:
        for work_class in ("coding", "personal", "general", None):
            plan = ka.resolve_assignment(work_class, board_config=cfg,
                                         explicit=ka.ExplicitOverrides(), assignee=None)
            tid = kb.create_task(conn, title="x", assignee=None,
                                 work_class=work_class, assignment_hook=_hook(plan))
            task = kb.get_task(conn, tid)
            assert task.model_override is None
            assert task.provider_override is None
            assert not _events(conn, tid, "assignment_applied")


def test_a1b_frontier_primary_flip_and_seat_repoint(kanban_home):
    _cfg(kanban_home, """kanban:
  assignment:
    enabled: true
    frontier_primary: opus
    seats:
      cheap:
        model: deepseek-v4-pro
        provider: deepseek
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
""")
    cfg = _board()
    plan = ka.resolve_assignment("financial", board_config=cfg,
                                 explicit=ka.ExplicitOverrides(), assignee=None)
    assert plan.model == "anthropic/claude-opus-5.5"
    assert plan.provider == "openrouter"
    cheap = ka.resolve_assignment("mechanical", board_config=cfg,
                                  explicit=ka.ExplicitOverrides(), assignee=None)
    assert cheap.model == "deepseek-v4-pro" and cheap.provider == "deepseek"


# --------------------------------------------------------------------------
# A-2: explicit pins win per-field
# --------------------------------------------------------------------------

def test_a2_explicit_wins_per_field(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    cfg = _board()
    with kbc.connect() as conn:
        # Explicit model pair wins; reasoning still comes from the rule.
        plan = ka.resolve_assignment(
            "financial", board_config=cfg,
            explicit=ka.ExplicitOverrides(model="my-model", provider="my-provider"),
            assignee=None)
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial",
                             model_override="my-model", provider_override="my-provider",
                             assignment_hook=_hook(plan))
        task = kb.get_task(conn, tid)
        assert task.model_override == "my-model"
        assert task.provider_override == "my-provider"
        assert task.reasoning_effort == "high"      # rule field still applied
        applied = _events(conn, tid, "assignment_applied")
        assert applied and applied[0].payload["source"] == "mixed"
        # Explicit reasoning wins; model pair comes from the rule.  (Call
        # sites pass explicit pins to create_task directly; the hook fills
        # only the rule-provided fields — §3.3.)
        plan2 = ka.resolve_assignment(
            "financial", board_config=cfg,
            explicit=ka.ExplicitOverrides(reasoning_effort="low"), assignee=None)
        tid2 = kb.create_task(conn, title="y", assignee=None, work_class="financial",
                              reasoning_effort="low",
                              assignment_hook=_hook(plan2))
        task2 = kb.get_task(conn, tid2)
        assert task2.model_override == "gemini-4-argon"
        assert task2.reasoning_effort == "low"


# --------------------------------------------------------------------------
# A-3 / A-3b: unresolvable seat + atomicity
# --------------------------------------------------------------------------

def test_a3_unresolvable_seat_skips_pin_with_event(kanban_home):
    # No custom_providers registered: the argon seat cannot resolve.
    _cfg(kanban_home, "kanban:\n  assignment:\n    enabled: true\n")
    cfg = _board()
    with kbc.connect() as conn:
        plan = ka.resolve_assignment("financial", board_config=cfg,
                                     explicit=ka.ExplicitOverrides(), assignee=None)
        assert plan.kind == "skipped_unresolvable"
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial",
                             assignment_hook=_hook(plan))
        task = kb.get_task(conn, tid)
        assert task.model_override is None
        skipped = _events(conn, tid, "assignment_skipped_unresolvable")
        assert len(skipped) == 1
        assert not _events(conn, tid, "assignment_applied")


def test_a3b_hook_failure_aborts_creation_atomically(kanban_home):
    with kbc.connect() as conn:
        def bad_hook(c, tid):
            raise RuntimeError("hook exploded")

        with pytest.raises(RuntimeError):
            kb.create_task(conn, title="x", assignee=None, assignment_hook=bad_hook)
        assert kb.list_tasks(conn) == []


def test_a3b_no_hook_no_assignment_events(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial")
        assert kb.get_task(conn, tid).model_override is None
        assert not _events(conn, tid, "assignment_applied")


# --------------------------------------------------------------------------
# A-4: Super Agent classification precedence
# --------------------------------------------------------------------------

def _decision_row(reason=None, matched=None):
    return {
        "reason_json": json.dumps(reason) if reason is not None else None,
        "matched_rules_json": json.dumps(matched) if matched is not None else None,
    }


def test_a4_classification_precedence(kanban_home):
    _cfg(kanban_home, """kanban:
  assignment:
    enabled: true
    sa_rule_map:
      urgent: mechanical
""")
    cfg, errs = kbcfg.load_board_config()
    acfg = kbcfg.section_config(cfg, "assignment")
    # basis.work_class beats the rule map.
    row = _decision_row(reason={"work_class": "financial"}, matched=["urgent"])
    assert ka.classify_sa_work_class(row, [], acfg) == "financial"
    # Rule map (matched order) beats source defaults.
    row = _decision_row(reason={}, matched=["urgent"])
    trigger = [{"source": "github"}]
    assert ka.classify_sa_work_class(row, trigger, acfg) == "mechanical"
    # Source defaults: github -> coding, vibehard -> production, else personal.
    row = _decision_row(reason={}, matched=[])
    assert ka.classify_sa_work_class(row, [{"source": "github"}], acfg) == "coding"
    assert ka.classify_sa_work_class(row, [{"source": "vibehard"}], acfg) == "production"
    assert ka.classify_sa_work_class(row, [{"source": "telegram"}], acfg) == "personal"
    # Invalid basis value is ignored, not fatal.
    row = _decision_row(reason={"work_class": "bogus"}, matched=[])
    assert ka.classify_sa_work_class(row, [{"source": "github"}], acfg) == "coding"


# --------------------------------------------------------------------------
# A-5: amend_mandate compatibility (classification never touches ticket_kind)
# --------------------------------------------------------------------------

def test_a5_work_class_and_ticket_kind_are_independent(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    cfg = _board()
    with kbc.connect() as conn:
        plan = ka.resolve_assignment("financial", board_config=cfg,
                                     explicit=ka.ExplicitOverrides(), assignee=None)
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial",
                             assignment_hook=_hook(plan))
        task = kb.get_task(conn, tid)
        assert task.work_class == "financial"

        def _ticket_kind(task_id):
            row = conn.execute("SELECT ticket_kind FROM tasks WHERE id = ?",
                               (task_id,)).fetchone()
            return row["ticket_kind"]

        assert _ticket_kind(tid) is None
        assert kb.edit_task(conn, tid, work_class="repair") is True
        task = kb.get_task(conn, tid)
        assert task.work_class == "repair"
        assert _ticket_kind(tid) is None


# --------------------------------------------------------------------------
# A-6: shipped seats resolve through build_kanban_policy
# --------------------------------------------------------------------------

def test_a6_shipped_seats_resolve_through_policy(kanban_home, monkeypatch, tmp_path):
    _cfg(kanban_home, ASSIGNMENT_ON)
    from types import SimpleNamespace

    from gateway.session_contract import Principal
    from gateway.session_kanban import build_kanban_policy

    profile = kanban_home / "profiles" / "sa-worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = {
        "model": {"default": "profile-model", "provider": "custom"},
        "custom_providers": [{"name": "antigravity-bridge"}, {"name": "bonsai-local"}],
        "agent": {"reasoning_effort": "medium"},
        "platform_toolsets": {"cli": ["terminal"]},
    }
    actor = Principal("owner", str(profile), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(
        authority=SimpleNamespace(profile_id=str(profile)), actor=actor,
        native_owner=True)
    db_path = str(kb.kanban_db_path())
    with kbc.connect() as conn:
        for seat_name in ("argon", "opus", "cheap"):
            seat = ka.DEFAULT_SEATS[seat_name]
            seat_model = seat["model"] if isinstance(seat, dict) else seat.model
            seat_provider = seat["provider"] if isinstance(seat, dict) else seat.provider
            tid = kb.create_task(
                conn, title=seat_name, assignee="sa-worker",
                workspace_kind="dir", workspace_path=str(workspace),
                model_override=seat_model, provider_override=seat_provider)
            task = kb.claim_task(conn, tid)
            assert task is not None, seat_name
            params = {"board": "default", "task_id": tid,
                      "run_id": task.current_run_id,
                      "claim_lock": task.claim_lock, "db": db_path}
            policy, _ = build_kanban_policy(connection, params, config)
            assert policy.model == seat_model, seat_name
            provider = policy.provider() if callable(policy.provider) else policy.provider
            assert provider == seat_provider, seat_name


# --------------------------------------------------------------------------
# A-7: flag OFF — today's behavior exactly
# --------------------------------------------------------------------------

def test_a7_flag_off_stores_class_without_pins(kanban_home):
    _cfg(kanban_home, "kanban:\n  assignment:\n    enabled: false\n")
    cfg, errs = kbcfg.load_board_config()
    assert not kbcfg.feature_enabled(cfg, errs, "assignment")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial")
        task = kb.get_task(conn, tid)
        assert task.work_class == "financial"
        assert task.model_override is None and task.provider_override is None
        assert not _events(conn, tid, "assignment_applied")


# --------------------------------------------------------------------------
# A-8: SA accept path — hook runs inside creation without config reads
# --------------------------------------------------------------------------

def test_a8_sa_style_creation_with_hook(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    cfg = _board()
    with kbc.connect() as conn:
        plan = ka.resolve_assignment("production", board_config=cfg,
                                     explicit=ka.ExplicitOverrides(), assignee=None,
                                     sa_path=True)
        tid = kb.create_task(conn, title="sa ticket", assignee=None,
                             created_by="superagent", work_class="production",
                             assignment_hook=_hook(plan))
        task = kb.get_task(conn, tid)
        assert task.model_override == "gemini-4-argon"
        applied = _events(conn, tid, "assignment_applied")
        assert len(applied) == 1
        # SA no-pin classes still record the classification event.
        plan2 = ka.resolve_assignment("personal", board_config=cfg,
                                      explicit=ka.ExplicitOverrides(), assignee=None,
                                      sa_path=True)
        tid2 = kb.create_task(conn, title="sa personal", assignee=None,
                              created_by="superagent", work_class="personal",
                              assignment_hook=_hook(plan2))
        applied2 = _events(conn, tid2, "assignment_applied")
        assert len(applied2) == 1
        assert applied2[0].payload["source"] == "profile_default"
        assert kb.get_task(conn, tid2).model_override is None


# --------------------------------------------------------------------------
# FIX round (T7): FIX-8, FIX-4 surfacing, FIX-18, A-8 made real
# --------------------------------------------------------------------------

def test_fix8_bogus_provider_is_unresolvable(kanban_home):
    _cfg(kanban_home, ASSIGNMENT_ON)
    # The built-in set is actually consulted: a typo'd provider name does
    # NOT resolve, a known built-in does.
    assert ka.seat_resolvable(ka.Seat(model="m", provider="openrouterr"), None) is False
    assert ka.seat_resolvable(ka.Seat(model="m", provider="openrouter"), None) is True
    # A seat re-pointed at a bogus provider takes the skipped-unresolvable
    # path at creation instead of pinning a seat that fails at admission.
    cfg = {"kanban": {"assignment": {
        "frontier_primary": "argon",
        "seats": {"argon": {"model": "gemini-4-argon", "provider": "openrouterr"}}}}}
    plan = ka.resolve_assignment("financial", board_config=cfg,
                                 explicit=ka.ExplicitOverrides(), assignee=None)
    assert plan.kind == "skipped_unresolvable"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee=None, work_class="financial",
                             assignment_hook=_hook(plan))
        assert kb.get_task(conn, tid).model_override is None
        assert len(_events(conn, tid, "assignment_skipped_unresolvable")) == 1


def test_fix4_assignment_consumer_surfaces_only_its_section(kanban_home):
    # Assignment enabled in config but its section is invalid: the CLI
    # create consumer surfaces exactly ONE board_config_invalid for the
    # assignment section on the created task (feature disabled by the
    # error, card created unpinned).
    _cfg(kanban_home, """kanban:
  assignment:
    enabled: true
    bogus_fix4: 1
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
""")
    from hermes_cli.kanban import run_slash
    out = run_slash("create fix4card --assignee worker --work-class financial")
    assert "fix4card" in out or "Created" in out or "t_" in out
    with kbc.connect() as conn:
        tasks = kb.list_tasks(conn)
        mine = [t for t in tasks if t.title == "fix4card"]
        assert len(mine) == 1
        tid = mine[0].id
        assert kb.get_task(conn, tid).model_override is None
        invalid = _events(conn, tid, "board_config_invalid")
        assert len(invalid) == 1
        assert invalid[0].payload.get("section") == "assignment"
        assert invalid[0].payload.get("key") == "bogus_fix4"


def test_fix18_tool_precompute_error_fails_creation_visibly(kanban_home, monkeypatch):
    _cfg(kanban_home, ASSIGNMENT_ON)
    from tools import kanban_tools

    def _boom(*a, **kw):
        raise RuntimeError("precompute exploded")

    monkeypatch.setattr(ka, "resolve_assignment", _boom)
    # The tool layer's contract turns the raise into a structured tool
    # error — creation FAILS VISIBLY (error result, no card) instead of
    # the old behavior: swallow, skip the hook, create the card unpinned.
    result = kanban_tools._handle_create({"title": "x", "assignee": "worker",
                                          "work_class": "financial"})
    assert "precompute exploded" in result
    with kbc.connect() as conn:
        assert [t for t in kb.list_tasks(conn) if t.title == "x"] == []


def test_a8_real_sa_accept_with_in_txn_trap(kanban_home, tmp_path, monkeypatch):
    # A-8 made real (FIX-10): drive the actual SA accept path
    # (receive_trigger -> assemble_batch -> run_eval -> accept ->
    # _accept_on_conn) with assignment enabled, and trap any config read
    # or own-txn helper firing while a write txn is open.
    _cfg(kanban_home, ASSIGNMENT_ON)
    from hermes_cli import kanban_board_config as kbcfg_mod
    from hermes_cli.orchestrator import accept, assemble_batch, receive_trigger, run_eval

    db_path = tmp_path / "board-a8.db"
    kbc.init_db(db_path)
    config = {
        "superagent": {
            "mode": "auto",
            "owner_identities": ["owner_alice"],
            "sources": {"vibehard": {"allowlist": ["owner_alice"]}},
        }
    }
    receive_trigger(
        db_path, source="vibehard", scope="main_scope", thread_key="vh:1",
        actor_source_id="owner_alice", actor_is_bot=False, addressed=True,
        kind="message", body="Ship the production thing",
        idempotency_key="idem_a8", now=1000)
    batch = assemble_batch(db_path, thread_key="vh:1", now=1001, config=config)
    assert batch is not None
    dec = run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)
    assert dec["outcome"] == "accept"

    with kbc.connect_closing(db_path) as conn:
        real_load = kbcfg_mod.load_board_config

        def _trapped_load(*a, **kw):
            assert not conn.in_transaction, \
                "load_board_config fired while a write txn was open (B-9)"
            return real_load(*a, **kw)

        monkeypatch.setattr(kbcfg_mod, "load_board_config", _trapped_load)

        def _no_own_txn_helper(*a, **kw):
            raise AssertionError("set_model_override used on the creation path")

        monkeypatch.setattr(kb, "set_model_override", _no_own_txn_helper)
        ticket_id = accept(conn, dec["id"], now=1003, config=config)

    assert ticket_id is not None
    with kbc.connect_closing(db_path) as conn:
        task = kb.get_task(conn, ticket_id)
        # vibehard -> production -> frontier primary (argon) pin, applied
        # inside the creation txn by the precomputed hook.
        assert task.work_class == "production"
        assert task.model_override == "gemini-4-argon"
        assert task.provider_override == "custom:antigravity-bridge"
        row = conn.execute("SELECT mandate_id FROM tasks WHERE id = ?",
                           (ticket_id,)).fetchone()
        assert row["mandate_id"] is not None
        applied = _events(conn, ticket_id, "assignment_applied")
        assert len(applied) == 1


# --------------------------------------------------------------------------
# Reconciliation (gates x surfaces U4): create_task inside an outer txn
# --------------------------------------------------------------------------

def test_reconcile_create_task_inside_outer_txn_uses_precomputed_timeout(
    kanban_home, monkeypatch
):
    # Surfaces U4 puts create_task INSIDE the single accept write_txn.
    # With the default timeout precomputed by the caller (the SA accept
    # precompute), gate-spec validation inside that txn performs NO
    # board-config read — the A-8 in-txn trap must never fire.
    def _boom(*a, **kw):
        raise AssertionError(
            "load_board_config called during in-txn create_task (B-9)"
        )

    monkeypatch.setattr(kbcfg, "load_board_config", _boom)
    with kbc.connect() as conn:
        with kb.write_txn(conn, allow_nested=True):
            tid = kb.create_task(
                conn,
                title="in-txn creation",
                gate_spec={
                    "version": 1,
                    "checks": [{"id": "t", "argv": ["true"]}],
                },
                gate_spec_default_timeout_seconds=777,
            )
        task = kb.get_task(conn, tid)
    spec = json.loads(task.gate_spec)
    assert spec["checks"][0]["timeout_seconds"] == 777


# --------------------------------------------------------------------------
# FIX-M1 (merge verification): malformed gates default timeout isolation
# --------------------------------------------------------------------------

_M1_CONFIG = {
    "superagent": {
        "mode": "auto",
        "owner_identities": ["owner_alice"],
        "sources": {"vibehard": {"allowlist": ["owner_alice"]}},
    }
}

_M1_BOARD_BAD_TIMEOUT = """kanban:
  gates:
    enabled: true
    default_timeout_seconds: "abc"
  assignment:
    enabled: true
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
"""

_M1_BOARD_GOOD_TIMEOUT = """kanban:
  gates:
    enabled: true
    default_timeout_seconds: 300
  assignment:
    enabled: true
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
"""


def _m1_setup(tmp_path, *, gate_spec=None):
    from hermes_cli.orchestrator import assemble_batch, receive_trigger, run_eval

    db_path = tmp_path / "board-m1.db"
    kbc.init_db(db_path)
    receive_trigger(
        db_path, source="vibehard", scope="main_scope", thread_key="vh:m1",
        actor_source_id="owner_alice", actor_is_bot=False, addressed=True,
        kind="message", body="Ship the production thing",
        idempotency_key="idem_m1", now=1000)
    batch = assemble_batch(db_path, thread_key="vh:m1", now=1001, config=_M1_CONFIG)
    assert batch is not None
    dec = run_eval(batch, db_path_or_conn=db_path, config=_M1_CONFIG, now=1002)
    assert dec["outcome"] == "accept"
    if gate_spec is not None:
        with kbc.connect_closing(db_path) as conn:
            row = conn.execute(
                "SELECT reason_json FROM sa_decisions WHERE id = ?",
                (dec["id"],)).fetchone()
            reason = json.loads(row["reason_json"] or "{}")
            reason["gate_spec"] = gate_spec
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE sa_decisions SET reason_json = ? WHERE id = ?",
                    (json.dumps(reason), dec["id"]))
    return db_path, dec["id"]


def _m1_counts(db_path):
    with kbc.connect_closing(db_path) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
            "mandates": conn.execute(
                "SELECT COUNT(*) c FROM sa_mandates").fetchone()["c"],
            "ticket_id": conn.execute(
                "SELECT ticket_id FROM sa_decisions").fetchone()["ticket_id"],
        }


def test_fixm1_malformed_gates_timeout_specless_accept_succeeds(
    kanban_home, tmp_path, monkeypatch
):
    # M-1 case (a): kanban.gates.default_timeout_seconds is malformed
    # ("abc") and the SA decision carries NO gate_spec. Pre-fix, the
    # accept call site evaluated int("abc") unconditionally and the
    # accept raised ValueError, creating nothing -- a malformed gates
    # key hard-failed ALL Super Agent accepts (section 5.1 / B-6
    # per-section isolation violation). Now a spec-less accept never
    # evaluates the gates timeout value: it succeeds, the ticket is
    # created, and no gate-spec validation runs at all.
    _cfg(kanban_home, _M1_BOARD_BAD_TIMEOUT)
    db_path, dec_id = _m1_setup(tmp_path)
    from hermes_cli import kanban_gates as _kg_mod
    from hermes_cli.orchestrator import accept

    def _no_gate_eval(*a, **kw):
        raise AssertionError("gate spec validated on a spec-less accept (FIX-M1)")

    monkeypatch.setattr(_kg_mod, "validate_gate_spec", _no_gate_eval)
    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=_M1_CONFIG)
    assert ticket_id is not None
    assert _m1_counts(db_path) == {
        "tasks": 1, "mandates": 1, "ticket_id": ticket_id}
    with kbc.connect_closing(db_path) as conn:
        task = kb.get_task(conn, ticket_id)
        assert task.gate_spec is None
        # Per-section isolation: the assignment feature is unaffected by
        # the malformed gates key -- vibehard -> production pin landed.
        assert task.work_class == "production"
        assert task.model_override == "gemini-4-argon"
        # The gates consumer was never exercised: nothing surfaced for it.
        assert _events(conn, ticket_id, "board_config_invalid") == []


def test_fixm1_malformed_gates_timeout_spec_carrying_accept_fails_typed(
    kanban_home, tmp_path
):
    # M-1 case (b): same malformed timeout, but the decision DOES carry
    # a gate_spec (whose check has no explicit timeout, so the default
    # genuinely applies). The accept must fail as a GATES-feature config
    # error sourced from the loader's own validation (the B-6 machinery:
    # section/key/message, already logged at ERROR by load_board_config)
    # -- NOT as the raw int() ValueError -- and it must fail BEFORE the
    # accept txn opens: no half-created accept (0 tasks / 0 mandates,
    # decision still unlinked).
    _cfg(kanban_home, _M1_BOARD_BAD_TIMEOUT)
    spec = {"version": 1, "checks": [{"id": "t", "argv": ["true"]}]}
    db_path, dec_id = _m1_setup(tmp_path, gate_spec=spec)
    from hermes_cli.orchestrator import accept

    with kbc.connect_closing(db_path) as conn:
        with pytest.raises(RuntimeError, match="default_timeout_seconds") as excinfo:
            accept(conn, dec_id, now=1003, config=_M1_CONFIG)
    assert "board_config_invalid" in str(excinfo.value)
    assert "invalid literal for int()" not in str(excinfo.value)
    assert _m1_counts(db_path) == {"tasks": 0, "mandates": 0, "ticket_id": None}
    # Fail-closed is retryable: fixing the config lets the SAME decision
    # accept cleanly, with the (valid) default timeout applied.
    _cfg(kanban_home, _M1_BOARD_GOOD_TIMEOUT)
    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1004, config=_M1_CONFIG)
    assert ticket_id is not None
    assert _m1_counts(db_path)["tasks"] == 1
    with kbc.connect_closing(db_path) as conn:
        stored = json.loads(kb.get_task(conn, ticket_id).gate_spec)
        assert stored["checks"][0]["timeout_seconds"] == 300
