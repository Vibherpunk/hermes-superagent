"""Tests for pure admission gate (hermes_cli/superagent_gate.py) per E-012, E-014."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from hermes_cli.superagent_gate import admit

# Shared policy snapshot fixture for table-driven admission tests
BASE_POLICY: dict[str, Any] = {
    "accept_mode": "observe",
    "sources": {
        "telegram": {
            "allowlist": ["tg_alice", "tg_bob"],
            "bots": ["tg_bot_helper"],
            "owner": "tg_owner",
        },
        "github": {
            "allowlist": ["gh_alice"],
            "bots": ["gh_bot_ci"],
            "owner": "gh_owner",
        },
        "cron": {
            "first_party": True,
        },
        "vibehard": {
            "first_party": True,
        },
    },
    "owner_identities": {
        "telegram": "tg_owner",
        "github": "gh_owner",
    },
    "first_party": ["cron", "vibehard"],
}


ADMISSION_CASES = [
    # 1. listed + addressing -> explicit, via='addressed'
    (
        "listed+addressing",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_alice",
            "actor_is_bot": False,
            "addressed": True,
            "kind": "message",
        },
        BASE_POLICY,
        "explicit",
        {
            "via": "addressed",
            "source": "telegram",
            "scope": "chat_100",
            "sender": "tg_alice",
        },
    ),
    # 2. listed + not addressing -> inferred, via='admission'
    (
        "listed+not addressing",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_alice",
            "actor_is_bot": False,
            "addressed": False,
            "kind": "message",
        },
        BASE_POLICY,
        "inferred",
        {
            "via": "admission",
            "source": "telegram",
            "scope": "chat_100",
            "sender": "tg_alice",
        },
    ),
    # 3. unlisted + addressing -> not_admitted
    (
        "unlisted+addressing",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_stranger",
            "actor_is_bot": False,
            "addressed": True,
            "kind": "message",
        },
        BASE_POLICY,
        "not_admitted",
        {},
    ),
    # 4. unlisted + not addressing -> not_admitted
    (
        "unlisted+not addressing",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_stranger",
            "actor_is_bot": False,
            "addressed": False,
            "kind": "message",
        },
        BASE_POLICY,
        "not_admitted",
        {},
    ),
    # 5. listed bot -> inferred, via='bot' (bot list only)
    (
        "listed bot",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_bot_helper",
            "actor_is_bot": True,
            "addressed": False,
            "kind": "message",
        },
        BASE_POLICY,
        "inferred",
        {
            "via": "bot",
            "source": "telegram",
            "scope": "chat_100",
            "sender": "tg_bot_helper",
        },
    ),
    # 6. unlisted bot -> not_admitted
    (
        "unlisted bot",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_spambot",
            "actor_is_bot": True,
            "addressed": True,
            "kind": "message",
        },
        BASE_POLICY,
        "not_admitted",
        {},
    ),
    # 7. owner -> explicit, via='first_party'
    (
        "owner",
        {
            "source": "telegram",
            "scope": "chat_100",
            "actor_source_id": "tg_owner",
            "actor_is_bot": False,
            "addressed": False,
            "kind": "message",
        },
        BASE_POLICY,
        "explicit",
        {
            "via": "first_party",
            "source": "telegram",
            "scope": "chat_100",
            "sender": "tg_owner",
        },
    ),
    # 8. cron sender-less first-party -> explicit, via='first_party'
    (
        "cron sender-less first-party",
        {
            "source": "cron",
            "scope": "main",
            "actor_source_id": None,
            "actor_is_bot": False,
            "addressed": False,
            "kind": "tick",
        },
        BASE_POLICY,
        "explicit",
        {"via": "first_party", "source": "cron", "scope": "main", "sender": None},
    ),
    # 9. CI sender-less not first-party -> not_admitted
    (
        "CI sender-less not first-party",
        {
            "source": "github",
            "scope": "repo:org/hermes",
            "actor_source_id": None,
            "actor_is_bot": False,
            "addressed": False,
            "kind": "workflow_run",
        },
        BASE_POLICY,
        "not_admitted",
        {},
    ),
    # 10. cross-source listing does NOT admit (actor listed under telegram, triggering on github)
    (
        "cross-source listing does NOT admit",
        {
            "source": "github",
            "scope": "repo:org/hermes",
            "actor_source_id": "tg_alice",
            "actor_is_bot": False,
            "addressed": True,
            "kind": "issue_comment",
        },
        BASE_POLICY,
        "not_admitted",
        {},
    ),
    # 11. vibehard first-party -> explicit, via='first_party'
    (
        "vibehard first-party",
        {
            "source": "vibehard",
            "scope": "job-101",
            "actor_source_id": "vibehard:charlie",
            "actor_is_bot": False,
            "addressed": False,
            "kind": "submitted",
        },
        BASE_POLICY,
        "explicit",
        {
            "via": "first_party",
            "source": "vibehard",
            "scope": "job-101",
            "sender": "vibehard:charlie",
        },
    ),
]


@pytest.mark.parametrize(
    ("case_name", "trigger", "policy", "expected_strength", "expected_basis"),
    ADMISSION_CASES,
    ids=[row[0] for row in ADMISSION_CASES],
)
def test_admit_spec_table(
    case_name: str,
    trigger: dict[str, Any],
    policy: dict[str, Any],
    expected_strength: str,
    expected_basis: dict[str, Any],
) -> None:
    """Table-driven test covering the exact E-014 spec cases."""
    strength, basis = admit(trigger, policy)
    assert strength == expected_strength
    assert basis == expected_basis


def test_import_hygiene_ast() -> None:
    """hermes_cli/superagent_gate.py AST contains no imports from gateway or kanban modules."""
    gate_file = (
        Path(__file__).parent.parent.parent / "hermes_cli" / "superagent_gate.py"
    )
    assert gate_file.is_file()

    tree = ast.parse(gate_file.read_text("utf-8"), filename=str(gate_file))
    imported_names: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.append(node.module)

    for mod in imported_names:
        assert not mod.startswith("gateway"), f"Banned import from gateway: {mod}"
        assert "kanban" not in mod, f"Banned import of kanban: {mod}"


def test_import_hygiene_clean_process() -> None:
    """Importing hermes_cli.superagent_gate in a clean process loads zero gateway or kanban modules."""
    probe_code = (
        "import sys\n"
        "import hermes_cli.superagent_gate\n"
        "loaded = set(sys.modules.keys())\n"
        "gateway_mods = [m for m in loaded if m == 'gateway' or m.startswith('gateway.')]\n"
        "kanban_mods = [m for m in loaded if 'kanban' in m]\n"
        "assert not gateway_mods, f'gateway modules loaded: {gateway_mods}'\n"
        "assert not kanban_mods, f'kanban modules loaded: {kanban_mods}'\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Import hygiene failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
