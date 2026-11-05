"""Surfaces Rev 3 — WRD U6 policy admission cases.

Runs the REAL policy_snapshot + superagent_gate.admit against the
Rev 3 §2 (U1/U5) config shapes — no source or gate changes; these pin
the admission outcomes the surfaces rely on once U1/U5 land at deploy
time.
"""

from __future__ import annotations

import pytest

from hermes_cli.orchestrator import policy_snapshot
from hermes_cli.superagent_gate import admit


U1_CONFIG = {
    "superagent": {
        "enabled": True,
        "accept_mode": "auto",
        "router_profile": "default",
        "pilot_surfaces": ["telegram", "api_server", "local"],
        "owner_identities": {"telegram": ["7078451053"], "local": ["uid:501", "local:desktop"]},
        "sources": {
            "api_server": {"first_party_kinds": ["text"]},
        },
        "vibehard": {"enabled": True},
    }
}

# The U5-conditional source entries (registered only when their
# preconditions hold at deploy time) layered over the U1 shape.
U5_CONFIG = {
    "superagent": {
        **U1_CONFIG["superagent"],
        "owner_identities": {"telegram": ["7078451053"], "github": ["gh_owner"]},
        "sources": {
            **U1_CONFIG["superagent"]["sources"],
            "github": {"allowlist": ["gh_alice"]},
        },
    }
}


def _trig(source, actor, *, addressed=True, kind="text", scope="scope-1"):
    return {
        "source": source,
        "scope": scope,
        "actor_source_id": actor,
        "actor_is_bot": False,
        "addressed": addressed,
        "kind": kind,
    }


CASES = [
    # Telegram owner (U1 owner_identities): explicit, addressed or not.
    ("telegram-owner-addressed", U1_CONFIG, _trig("telegram", "7078451053"), "explicit"),
    ("telegram-owner-unaddressed", U1_CONFIG, _trig("telegram", "7078451053", addressed=False), "explicit"),
    # Telegram stranger: nobody home.
    ("telegram-stranger", U1_CONFIG, _trig("telegram", "999999999"), "not_admitted"),
    # Local admission is OWNER-based (desktop-fix review H1, 2026-11-04):
    # first-party-kinds for local admitted ANY actor, so it was removed;
    # only the owner's own identities admit — the gateway desktop uid and
    # the TUI seam's pinned actor. Any other local actor is turned away.
    ("local-text", U1_CONFIG, _trig("local", "local:desktop"), "explicit"),
    ("local-gateway-owner", U1_CONFIG, _trig("local", "uid:501"), "explicit"),
    ("local-stranger", U1_CONFIG, _trig("local", "uid:999"), "not_admitted"),
    ("local-cli-actor", U1_CONFIG, _trig("local", "api"), "not_admitted"),
    ("local-photo", U1_CONFIG, _trig("local", "local:desktop", kind="photo"), "explicit"),
    # API server: kind=text via the "api" actor admits; the wake kind
    # 'stuck' does not (wakes never carry that kind through the door —
    # and if one did, it must not admit).
    ("api-text", U1_CONFIG, _trig("api_server", "api"), "explicit"),
    ("api-stuck", U1_CONFIG, _trig("api_server", "api", kind="stuck"), "not_admitted"),
    # VibeHard submissions stay inert until the source is registered.
    ("vibehard-submitted", U1_CONFIG, _trig("vibehard", "vibehard:job-1", kind="submitted"), "not_admitted"),
    # U5-conditional github entry: allowlisted unaddressed -> inferred;
    # the github owner -> explicit.
    ("github-allowlisted-unaddressed", U5_CONFIG, _trig("github", "gh_alice", addressed=False), "inferred"),
    ("github-owner", U5_CONFIG, _trig("github", "gh_owner"), "explicit"),
]


@pytest.mark.parametrize(
    "config,trigger,expected",
    [pytest.param(cfg, trig, exp, id=name) for name, cfg, trig, exp in CASES],
)
def test_u1_policy_admission(config, trigger, expected):
    policy = policy_snapshot(config)
    strength, _basis = admit(trigger, policy)
    assert strength == expected
