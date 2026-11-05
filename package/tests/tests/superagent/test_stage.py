"""Unit tests for derived_stage function in hermes_cli.orchestrator_reads."""

from __future__ import annotations

from hermes_cli.orchestrator_reads import derived_stage


class TestDerivedStageBranches:
    """One test per branch (8 branches) as specified in SPEC-V2 §9."""

    def test_branch_1_delivery_error(self):
        """Branch 1: ctx.last_delivery_error is truthy -> 'person/delivery'."""
        ticket = {"status": "running"}
        ctx = {"last_delivery_error": "Connection timed out delivering to Matrix"}
        assert derived_stage(ticket, ctx) == "person/delivery"

    def test_branch_2_person_answer(self):
        """Branch 2: ctx.proposal_state == 'awaiting_answer' OR (status == 'blocked' AND block_kind == 'needs_input')."""
        # Case 2a: proposal_state is awaiting_answer
        ticket = {"status": "claimed"}
        ctx = {"proposal_state": "awaiting_answer"}
        assert derived_stage(ticket, ctx) == "person/answer"

        # Case 2b: status is blocked with block_kind needs_input
        ticket_blocked = {"status": "blocked", "block_kind": "needs_input"}
        assert derived_stage(ticket_blocked, {}) == "person/answer"

    def test_branch_3_person_review(self):
        """Branch 3: ctx.proposal_state == 'awaiting_review' OR (status == 'blocked' AND block_kind == 'review')."""
        # Case 3a: proposal_state is awaiting_review
        ticket = {"status": "claimed"}
        ctx = {"proposal_state": "awaiting_review"}
        assert derived_stage(ticket, ctx) == "person/review"

        # Case 3b: status is blocked with block_kind review
        ticket_review = {"status": "blocked", "block_kind": "review"}
        assert derived_stage(ticket_review, {}) == "person/review"

    def test_branch_4_person_blocked(self):
        """Branch 4: ticket.status IN ('blocked', 'failed') -> 'person/blocked'."""
        assert derived_stage({"status": "blocked"}, {}) == "person/blocked"
        assert derived_stage({"status": "blocked", "block_kind": "other_reason"}, {}) == "person/blocked"
        assert derived_stage({"status": "failed"}, {}) == "person/blocked"

    def test_branch_5_working(self):
        """Branch 5: ticket.status IN ('running', 'claimed') OR ctx.sa_state == 'working' -> 'working'."""
        assert derived_stage({"status": "running"}, {}) == "working"
        assert derived_stage({"status": "claimed"}, {}) == "working"
        assert derived_stage({"status": "todo"}, {"sa_state": "working"}) == "working"

    def test_branch_6_queued(self):
        """Branch 6: ticket.status IN ('ready', 'todo') OR ctx.sa_state == 'dispatched' -> 'queued'."""
        assert derived_stage({"status": "ready"}, {}) == "queued"
        assert derived_stage({"status": "todo"}, {}) == "queued"
        assert derived_stage({"status": "backlog"}, {"sa_state": "dispatched"}) == "queued"

    def test_branch_7_closed(self):
        """Branch 7: ticket.status == 'done' -> 'closed'."""
        assert derived_stage({"status": "done"}, {}) == "closed"

    def test_branch_8_idle(self):
        """Branch 8: else -> 'idle'."""
        assert derived_stage({}, {}) == "idle"
        assert derived_stage({"status": "backlog"}, {}) == "idle"
        assert derived_stage({"status": "custom_status"}, {}) == "idle"


class TestDerivedStagePrecedence:
    """Precedence tests asserting priority order across branches."""

    def test_precedence_delivery_error_beats_awaiting_review(self):
        """Delivery error (branch 1) beats awaiting_review (branch 3)."""
        ticket = {"status": "blocked", "block_kind": "review"}
        ctx = {
            "last_delivery_error": "Webhook 500 error",
            "proposal_state": "awaiting_review",
        }
        assert derived_stage(ticket, ctx) == "person/delivery"

    def test_precedence_awaiting_answer_beats_running_status(self):
        """Awaiting_answer (branch 2) beats running status (branch 5)."""
        ticket = {"status": "running"}
        ctx = {"proposal_state": "awaiting_answer"}
        assert derived_stage(ticket, ctx) == "person/answer"

    def test_precedence_done_with_delivery_error_reports_person_delivery(self):
        """Done status (branch 7) + delivery error still reports person/delivery per branch 1."""
        ticket = {"status": "done"}
        ctx = {"last_delivery_error": "Delivery to slack failed"}
        assert derived_stage(ticket, ctx) == "person/delivery"

    def test_precedence_ladder(self):
        """Additional precedence checks verifying full ladder order."""
        # Branch 1 beats Branch 2
        assert (
            derived_stage(
                {"status": "blocked", "block_kind": "needs_input"},
                {"last_delivery_error": "err", "proposal_state": "awaiting_answer"},
            )
            == "person/delivery"
        )
        # Branch 2 beats Branch 3
        assert (
            derived_stage(
                {"status": "blocked", "block_kind": "review"},
                {"proposal_state": "awaiting_answer"},
            )
            == "person/answer"
        )
        # Branch 3 beats Branch 4
        assert (
            derived_stage(
                {"status": "failed"},
                {"proposal_state": "awaiting_review"},
            )
            == "person/review"
        )
        # Branch 4 beats Branch 5
        assert (
            derived_stage(
                {"status": "failed"},
                {"sa_state": "working"},
            )
            == "person/blocked"
        )
        # Branch 5 beats Branch 6
        assert (
            derived_stage(
                {"status": "running"},
                {"sa_state": "dispatched"},
            )
            == "working"
        )
        # Branch 6 beats Branch 7
        assert (
            derived_stage(
                {"status": "done"},
                {"sa_state": "dispatched"},
            )
            == "queued"
        )


class TestDerivedStageMissingKeys:
    """Missing-keys tests verifying graceful handling of empty or partial dicts."""

    def test_empty_ctx_dict_works(self):
        """Empty ctx dict works without raising KeyError."""
        assert derived_stage({"status": "ready"}, {}) == "queued"
        assert derived_stage({"status": "todo"}, {}) == "queued"
        assert derived_stage({"status": "running"}, {}) == "working"
        assert derived_stage({"status": "done"}, {}) == "closed"

    def test_empty_ticket_dict_works(self):
        """Empty ticket dict works without raising KeyError."""
        assert derived_stage({}, {}) == "idle"
        assert derived_stage({}, {"sa_state": "working"}) == "working"
        assert derived_stage({}, {"last_delivery_error": "fail"}) == "person/delivery"

    def test_missing_and_none_values(self):
        """Partial keys with None or missing values evaluate correctly."""
        ctx_nones = {
            "last_delivery_error": None,
            "proposal_state": None,
            "sa_state": None,
        }
        assert derived_stage({"status": "blocked"}, ctx_nones) == "person/blocked"
        assert derived_stage({"status": None, "block_kind": None}, ctx_nones) == "idle"


class TestDerivedStageGateScope:
    """E-9 (DESIGN rev 4, §2.7): rule 4's triage branch is scoped — it fires
    only for SA-linked tickets or the three Super Agent gate block kinds.
    A legacy triage card with neither keeps the pre-fix fall-through."""

    def test_triage_with_mandate_is_person_blocked(self):
        ticket = {"status": "triage", "mandate_id": 42, "block_kind": None}
        assert derived_stage(ticket, {}) == "person/blocked"

    def test_triage_with_gate_kinds_is_person_blocked(self):
        for kind in ("gate_worker", "gate_env", "spend_ceiling"):
            ticket = {"status": "triage", "mandate_id": None, "block_kind": kind}
            assert derived_stage(ticket, {}) == "person/blocked", kind

    def test_legacy_triage_falls_through_to_idle(self):
        ticket = {"status": "triage", "mandate_id": None, "block_kind": None}
        assert derived_stage(ticket, {}) == "idle"
        ticket2 = {"status": "triage", "block_kind": "capability"}
        assert derived_stage(ticket2, {}) == "idle"

    def test_blocked_status_unaffected_by_scope(self):
        # Non-triage blocked cards still hit rule 4 unconditionally.
        ticket = {"status": "blocked", "mandate_id": None, "block_kind": "capability"}
        assert derived_stage(ticket, {}) == "person/blocked"
