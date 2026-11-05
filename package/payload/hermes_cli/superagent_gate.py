"""Pure admission gate for Hermes Super-agent (Phase 2, E-012, E-014).

Evaluates trigger admission based on policy snapshot. Pure functions,
no I/O, no database, no gateway imports.
"""

from __future__ import annotations

from typing import Any

__all__ = ["admit"]


def _collect_strings(val: Any, out: set[str]) -> None:
    """Collect non-empty stringified items into out set."""
    if isinstance(val, (list, set, tuple)):
        for item in val:
            if item is not None:
                s = str(item).strip()
                if s:
                    out.add(s)
    elif isinstance(val, str) and val.strip():
        out.add(val.strip())


def _matches_identity(val: Any, target_id: str) -> bool:
    """Check if target_id matches val (as string, or element in collection)."""
    if val is None:
        return False
    if isinstance(val, (list, set, tuple)):
        return any(str(x) == target_id for x in val if x is not None)
    return str(val) == target_id


def _extract_source_allowlist(policy: dict[str, Any], source: str) -> set[str]:
    """Extract people allowlist identities for the specified source."""
    if not source:
        return set()

    identities: set[str] = set()

    # 1. From policy["sources"][source]
    sources = policy.get("sources")
    if isinstance(sources, dict):
        src_entry = sources.get(source)
        if isinstance(src_entry, dict):
            for k in ("allowlist", "allowlists", "people", "users"):
                _collect_strings(src_entry.get(k), identities)

    # 2. From policy["allowlists"] / policy["allowlist"]
    for key in ("allowlists", "allowlist"):
        al = policy.get(key)
        if not isinstance(al, dict):
            continue
        _collect_strings(al.get(source), identities)
        al_sources = al.get("sources")
        if isinstance(al_sources, dict):
            _collect_strings(al_sources.get(source), identities)

    return identities


def _extract_source_bots(policy: dict[str, Any], source: str) -> set[str]:
    """Extract bot identities for the specified source."""
    if not source:
        return set()

    bots: set[str] = set()

    # 1. From policy["sources"][source]
    sources = policy.get("sources")
    if isinstance(sources, dict):
        src_entry = sources.get(source)
        if isinstance(src_entry, dict):
            for k in ("bots", "bot_list", "bot_allowlist"):
                _collect_strings(src_entry.get(k), bots)

    # 2. From policy["bots"] / policy["bot_list"] / policy["bot_allowlist"]
    for key in ("bots", "bot_list", "bot_allowlist"):
        b_dict = policy.get(key)
        if not isinstance(b_dict, dict):
            continue
        _collect_strings(b_dict.get(source), bots)
        b_sources = b_dict.get("sources")
        if isinstance(b_sources, dict):
            _collect_strings(b_sources.get(source), bots)

    # 3. From policy["allowlists"]["bots"][source]
    for key in ("allowlists", "allowlist"):
        al = policy.get(key)
        if isinstance(al, dict) and isinstance(al.get("bots"), dict):
            _collect_strings(al["bots"].get(source), bots)

    return bots


def _is_owner_for_source(policy: dict[str, Any], source: str, actor_id: str) -> bool:
    """Check if actor_id matches owner identity for this source."""
    if not actor_id or not source:
        return False

    actor_str = str(actor_id)

    # 1. From policy["sources"][source]
    sources = policy.get("sources")
    if isinstance(sources, dict):
        src_entry = sources.get(source)
        if isinstance(src_entry, dict):
            for k in ("owner", "owner_id", "owner_identities", "owners"):
                if _matches_identity(src_entry.get(k), actor_str):
                    return True

    # 2. From policy["owner_identities"] (or owner_ids, owners, owner_id, owner)
    for k in ("owner_identities", "owner_ids", "owners", "owner_id", "owner"):
        owners = policy.get(k)
        if owners is None:
            continue
        if isinstance(owners, dict):
            sval = owners.get(source)
            if sval is None and isinstance(owners.get("sources"), dict):
                sval = owners["sources"].get(source)
            if _matches_identity(sval, actor_str):
                return True
        elif _matches_identity(owners, actor_str):
            return True

    return False


def _is_first_party_actor(policy: dict[str, Any], source: str, actor_id: str) -> bool:
    """Check if actor_id is owner or explicit first-party identity for source."""
    if _is_owner_for_source(policy, source, actor_id):
        return True

    sources = policy.get("sources")
    if isinstance(sources, dict):
        src_entry = sources.get(source)
        if isinstance(src_entry, dict):
            for k in (
                "first_party_identities",
                "first_party_ids",
                "first_party_actors",
            ):
                if _matches_identity(src_entry.get(k), actor_id):
                    return True

    for k in ("first_party_identities", "first_party_ids", "first_party_actors"):
        fp = policy.get(k)
        if isinstance(fp, dict) and _matches_identity(fp.get(source), actor_id):
            return True

    return False


def _is_first_party_senderless(policy: dict[str, Any], source: str, kind: str) -> bool:
    """Check if sender-less trigger is marked as first-party for source/kind."""
    # Check policy["sources"][source]["first_party"]
    sources = policy.get("sources")
    if isinstance(sources, dict):
        src_entry = sources.get(source)
        if isinstance(src_entry, dict):
            fp_val = src_entry.get("first_party")
            if fp_val is True:
                return True
            if isinstance(fp_val, (list, set, tuple)) and (
                kind in fp_val or "*" in fp_val or "all" in fp_val
            ):
                return True
            if isinstance(fp_val, dict) and (
                fp_val.get(kind) is True or fp_val.get("enabled") is True
            ):
                return True
            if (
                isinstance(src_entry.get("first_party_kinds"), (list, set, tuple))
                and kind in src_entry["first_party_kinds"]
            ):
                return True

    # Check top-level policy["first_party"]
    fp = policy.get("first_party")
    if fp is True:
        return True
    if isinstance(fp, (list, set, tuple)):
        return source in fp or kind in fp or f"{source}:{kind}" in fp or "*" in fp
    if isinstance(fp, dict):
        if fp.get(source) is True:
            return True
        if isinstance(fp.get(source), (list, set, tuple)) and (
            kind in fp[source] or "*" in fp[source]
        ):
            return True
        if isinstance(fp.get(source), dict) and (
            fp[source].get(kind) is True or fp[source].get("enabled") is True
        ):
            return True
        if (
            isinstance(fp.get("sources"), (list, set, tuple))
            and source in fp["sources"]
        ):
            return True
        if isinstance(fp.get("kinds"), (list, set, tuple)) and kind in fp["kinds"]:
            return True

    # Check first_party_sources and first_party_kinds lists
    if (
        isinstance(policy.get("first_party_sources"), (list, set, tuple))
        and source in policy["first_party_sources"]
    ):
        return True
    if (
        isinstance(policy.get("first_party_kinds"), (list, set, tuple))
        and kind in policy["first_party_kinds"]
    ):
        return True

    return False


def _is_first_party(
    policy: dict[str, Any],
    source: str,
    kind: str,
    actor_id: str | None,
) -> bool:
    """Check whether trigger qualifies as first-party (E-014)."""
    if _is_first_party_senderless(policy, source, kind):
        return True
    if actor_id is not None:
        return _is_first_party_actor(policy, source, actor_id)
    return False


def admit(
    trigger: dict[str, Any], policy: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Evaluate pure admission gate for a trigger and policy snapshot per E-012 and E-014.

    Args:
        trigger: Dict carrying:
            - source: str
            - scope: str
            - actor_source_id: str | None
            - actor_is_bot: bool | int
            - addressed: bool | int
            - kind: str
        policy: Dict carrying policy snapshot (e.g. from orchestrator.policy_snapshot).

    Returns:
        tuple[str, dict[str, Any]]:
            (strength, basis_dict) where:
            strength in {"explicit", "inferred", "not_admitted"}
            basis_dict is:
                {"via": via, "source": source, "scope": scope, "sender": sender}
                when admitted, or {} when not_admitted.
    """
    if not isinstance(trigger, dict):
        return ("not_admitted", {})
    if not isinstance(policy, dict):
        policy = {}

    source = str(trigger.get("source") or "")
    scope = str(trigger.get("scope") or "")
    raw_actor = trigger.get("actor_source_id")
    actor_source_id = str(raw_actor) if raw_actor is not None else None
    actor_is_bot = bool(trigger.get("actor_is_bot", False))
    addressed = bool(trigger.get("addressed", False))
    kind = str(trigger.get("kind") or "")

    # 1. Sender-less trigger (actor_source_id is None)
    if actor_source_id is None:
        if _is_first_party(policy, source, kind, actor_id=None):
            return (
                "explicit",
                {
                    "via": "first_party",
                    "source": source,
                    "scope": scope,
                    "sender": None,
                },
            )
        return ("not_admitted", {})

    # 2. Actor matches owner / first-party identity for this source
    if _is_first_party(policy, source, kind, actor_id=actor_source_id):
        return (
            "explicit",
            {
                "via": "first_party",
                "source": source,
                "scope": scope,
                "sender": actor_source_id,
            },
        )

    # 3. Bot check (bot list only per E-014)
    bot_list = _extract_source_bots(policy, source)
    is_listed_bot = actor_source_id in bot_list

    if actor_is_bot or is_listed_bot:
        if is_listed_bot:
            return (
                "inferred",
                {
                    "via": "bot",
                    "source": source,
                    "scope": scope,
                    "sender": actor_source_id,
                },
            )
        # Unlisted bot -> ignore / not_admitted
        return ("not_admitted", {})

    # 4. Human / person allowlist check (source-scoped)
    allowlist = _extract_source_allowlist(policy, source)
    is_listed_person = actor_source_id in allowlist

    if is_listed_person:
        if addressed:
            # listed + addressing = explicit
            return (
                "explicit",
                {
                    "via": "addressed",
                    "source": source,
                    "scope": scope,
                    "sender": actor_source_id,
                },
            )
        # listed + not addressing = inferred
        return (
            "inferred",
            {
                "via": "admission",
                "source": source,
                "scope": scope,
                "sender": actor_source_id,
            },
        )

    # 5. Unlisted human (addressing or not) -> ignore / not_admitted
    return ("not_admitted", {})
