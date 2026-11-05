"""Tests for the superagent configuration block per E-033."""

import pytest
from gateway.kanban_watchers_dispatcher import superagent_settings
from hermes_cli.config import validate_config_structure


class TestSuperagentConfigSettings:
    """Unit tests for superagent_settings resolver and validation."""

    def test_missing_block_returns_defaults_with_enabled_false(self):
        cfg = superagent_settings({})
        assert cfg["enabled"] is False
        assert cfg["accept_mode"] == "observe"
        assert cfg["eval_timeout_seconds"] == 60
        assert cfg["worker_slots"] == 4
        assert cfg["human_reserve_slots"] == 1
        assert cfg["router_profile"] is None
        assert cfg["router_model"] is None
        assert cfg["worker_model"] is None
        assert cfg["rules"] == []
        assert cfg["github"] == {
            "repository": None,
            "receive_all": {"issues": False, "pull_requests": False, "comments": False, "ci": False},
        }
        assert cfg["vibehard"] == {
            "enabled": False,
            "ledger_path": "~/.vibehard-agent/ledger.sqlite",
            "stuck_after_seconds": 1800,
        }
        assert cfg["sources"] == {}

    def test_full_block_parses(self):
        raw = {
            "superagent": {
                "enabled": True,
                "accept_mode": "auto",
                "eval_timeout_seconds": 120,
                "worker_slots": 8,
                "human_reserve_slots": 1,
                "router_model": {"provider": "custom", "model": "router-v1"},
                "worker_model": {"provider": "custom", "model": "worker-v1"},
                "rules": [{"name": "rule1", "instruction": "do something"}],
                "github": {
                    "repository": "owner/repo",
                    "receive_all": {"issues": True, "pull_requests": True, "comments": True, "ci": True},
                },
                "vibehard": {
                    "enabled": True,
                    "ledger_path": "/custom/path/ledger.sqlite",
                    "stuck_after_seconds": 3600,
                },
                "sources": {"source1": {"allowlist": ["a"], "admission": {}, "bots": ["bot1"]}},
            }
        }
        cfg = superagent_settings(raw)
        assert cfg["enabled"] is True
        assert cfg["accept_mode"] == "auto"
        assert cfg["eval_timeout_seconds"] == 120
        assert cfg["worker_slots"] == 8
        assert cfg["human_reserve_slots"] == 1
        assert cfg["router_model"] == {"provider": "custom", "model": "router-v1"}
        assert cfg["worker_model"] == {"provider": "custom", "model": "worker-v1"}
        assert cfg["rules"] == [{"name": "rule1", "instruction": "do something"}]
        assert cfg["github"]["repository"] == "owner/repo"
        assert cfg["github"]["receive_all"]["issues"] is True
        assert cfg["vibehard"] == {
            "enabled": True,
            "ledger_path": "/custom/path/ledger.sqlite",
            "stuck_after_seconds": 3600,
        }
        assert "source1" in cfg["sources"]

    def test_invalid_accept_mode_raises(self):
        raw = {"superagent": {"accept_mode": "invalid_mode"}}
        with pytest.raises(ValueError, match="superagent.accept_mode"):
            superagent_settings(raw)

    def test_invalid_worker_slots_raises(self):
        for bad_val in (0, -1, "bad"):
            raw = {"superagent": {"worker_slots": bad_val}}
            with pytest.raises(ValueError, match="superagent.worker_slots"):
                superagent_settings(raw)

    def test_worker_slots_bool_raises(self):
        for bool_val in (True, False):
            raw = {"superagent": {"worker_slots": bool_val}}
            with pytest.raises(ValueError, match="superagent.worker_slots"):
                superagent_settings(raw)

    def test_invalid_human_reserve_slots_raises(self):
        for bad_val in (-1, 2, "bad"):
            raw = {"superagent": {"human_reserve_slots": bad_val}}
            with pytest.raises(ValueError, match="superagent.human_reserve_slots"):
                superagent_settings(raw)

    def test_human_reserve_slots_bool_raises(self):
        for bool_val in (True, False):
            raw = {"superagent": {"human_reserve_slots": bool_val}}
            with pytest.raises(ValueError, match="superagent.human_reserve_slots"):
                superagent_settings(raw)

    def test_partial_receive_all_merges_defaults(self):
        raw = {
            "superagent": {
                "github": {
                    "repository": "owner/repo",
                    "receive_all": {"issues": True},
                }
            }
        }
        cfg = superagent_settings(raw)
        assert cfg["github"]["repository"] == "owner/repo"
        assert cfg["github"]["receive_all"] == {
            "issues": True,
            "pull_requests": False,
            "comments": False,
            "ci": False,
        }

    def test_github_none_parses_to_defaults(self):
        raw = {"superagent": {"github": None}}
        cfg = superagent_settings(raw)
        assert cfg["github"] == {
            "repository": None,
            "receive_all": {"issues": False, "pull_requests": False, "comments": False, "ci": False},
        }

    def test_github_receive_all_none_parses_to_defaults(self):
        raw = {
            "superagent": {
                "github": {
                    "repository": "owner/repo",
                    "receive_all": None,
                }
            }
        }
        cfg = superagent_settings(raw)
        assert cfg["github"]["repository"] == "owner/repo"
        assert cfg["github"]["receive_all"] == {
            "issues": False,
            "pull_requests": False,
            "comments": False,
            "ci": False,
        }

    def test_rules_list_round_trips(self):
        rules = [
            {"name": "r1", "instruction": "first rule"},
            {"name": "r2", "instruction": "second rule"},
        ]
        cfg = superagent_settings({"superagent": {"rules": rules}})
        assert cfg["rules"] == rules

    def test_passthrough_defaults_to_true(self):
        cfg = superagent_settings({})
        assert cfg["passthrough"] is True

    def test_passthrough_parses_false(self):
        cfg = superagent_settings({"superagent": {"passthrough": False}})
        assert cfg["passthrough"] is False

    def test_invalid_passthrough_raises(self):
        for bad_val in ("false", "true", 1, 0, None, []):
            with pytest.raises(ValueError, match="superagent.passthrough"):
                superagent_settings({"superagent": {"passthrough": bad_val}})


class TestSuperagentConfigValidationStructure:
    """Test validate_config_structure reports errors with exact key naming."""

    def test_structure_validates_clean_defaults(self):
        issues = validate_config_structure({"superagent": {"enabled": False}})
        errors = [i for i in issues if i.severity == "error" and "superagent" in i.message]
        assert not errors

    def test_structure_reports_invalid_accept_mode(self):
        issues = validate_config_structure({"superagent": {"accept_mode": "bad"}})
        errors = [i for i in issues if i.severity == "error" and "superagent.accept_mode" in i.message]
        assert len(errors) == 1

    def test_structure_reports_invalid_worker_slots(self):
        issues = validate_config_structure({"superagent": {"worker_slots": 0}})
        errors = [i for i in issues if i.severity == "error" and "superagent.worker_slots" in i.message]
        assert len(errors) == 1

    def test_structure_reports_invalid_human_reserve_slots(self):
        for bad_val in (-1, 2, True):
            issues = validate_config_structure({"superagent": {"human_reserve_slots": bad_val}})
            errors = [i for i in issues if i.severity == "error" and "superagent.human_reserve_slots" in i.message]
            assert len(errors) == 1

    def test_structure_reports_worker_slots_bool(self):
        issues = validate_config_structure({"superagent": {"worker_slots": True}})
        errors = [i for i in issues if i.severity == "error" and "superagent.worker_slots" in i.message]
        assert len(errors) == 1

    def test_structure_reports_invalid_passthrough(self):
        for bad_val in ("false", 1, 0, []):
            issues = validate_config_structure({"superagent": {"passthrough": bad_val}})
            errors = [i for i in issues if i.severity == "error" and "superagent.passthrough" in i.message]
            assert len(errors) == 1

    def test_structure_reports_invalid_vibehard(self):
        issues = validate_config_structure({"superagent": {"vibehard": "not-a-dict"}})
        errors = [i for i in issues if i.severity == "error" and "superagent.vibehard" in i.message]
        assert len(errors) == 1

    def test_structure_reports_invalid_vibehard_enabled(self):
        for bad_val in ("true", 1, 0, []):
            issues = validate_config_structure({"superagent": {"vibehard": {"enabled": bad_val}}})
            errors = [i for i in issues if i.severity == "error" and "superagent.vibehard.enabled" in i.message]
            assert len(errors) == 1

    def test_structure_reports_invalid_vibehard_stuck_after_seconds(self):
        for bad_val in (0, -1, "bad", True):
            issues = validate_config_structure({"superagent": {"vibehard": {"stuck_after_seconds": bad_val}}})
            errors = [i for i in issues if i.severity == "error" and "superagent.vibehard.stuck_after_seconds" in i.message]
            assert len(errors) == 1
