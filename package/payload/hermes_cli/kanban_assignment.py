"""Deterministic model assignment by ticket kind (DESIGN rev 4, §3).

The work_class -> seat-class + reasoning mapping is a CODE CONSTANT
(``RULE_TABLE``, ruling 9).  Board config may re-point only the model/provider
STRINGS behind a seat class (``kanban.assignment.seats``); it can never remap
a class — the board-config validator (§5.1) flags any such attempt and the
assignment consumer treats the feature as disabled.

Assignment runs at creation, inside the caller's creation ``write_txn``, via
the opt-in ``create_task(assignment_hook=...)``.  The hook receives a
PRECOMPUTED plan (B-9): all config reads and the resolvability check happen at
the call site, before ``create_task`` is invoked; inside the txn the hook
performs raw UPDATEs + ``_append_event`` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

WORK_CLASSES = {
    "financial", "production", "architecture", "coding",
    "mechanical", "repair", "personal", "general",
}

# work_class -> (seat class | None, reasoning_effort | None).  CODE CONSTANT.
# None seat = profile_default (no pin).
RULE_TABLE: dict[str, tuple[Optional[str], Optional[str]]] = {
    "financial": ("frontier", "high"),
    "production": ("frontier", "high"),
    "architecture": ("frontier", "xhigh"),
    "coding": (None, None),
    "mechanical": ("cheap", "low"),
    "repair": ("cheap", "low"),
    "personal": (None, None),
    "general": (None, None),
}

# Shipped seat-string defaults (§3.2; spellings verified per C-6).
DEFAULT_SEATS: dict[str, dict[str, str]] = {
    "argon": {"model": "gemini-4-argon", "provider": "custom:antigravity-bridge"},
    "opus": {"model": "anthropic/claude-opus-5.5", "provider": "openrouter"},
    "cheap": {"model": "mtplx-bonsai-2-27b-optimized-speed", "provider": "custom:bonsai-local"},
}

DEFAULT_SA_SOURCE_DEFAULTS = {"github": "coding", "vibehard": "production", "default": "personal"}

# Providers that need no profile-local registration to resolve.
_BUILTIN_PROVIDERS = {
    "openrouter", "anthropic", "openai", "google", "deepseek", "xai", "mistral",
    "groq", "together", "fireworks", "perplexity", "cohere", "azure", "bedrock",
    "vertex", "ollama", "lmstudio", "auto", "",
}


@dataclass(frozen=True)
class Seat:
    model: str
    provider: str
    reasoning_effort: Optional[str] = None


@dataclass
class ExplicitOverrides:
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: Optional[str] = None


@dataclass
class CreationPlan:
    """A precomputed creation assignment (B-9).  Consumed by the in-txn hook."""
    work_class: str
    kind: str  # "pin" | "none" | "profile_default" | "skipped_unresolvable"
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: Optional[str] = None
    source: str = "rule"  # "rule" | "explicit" | "mixed" | "profile_default"
    emit_event: bool = False


def validate_work_class(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    if v not in WORK_CLASSES:
        raise ValueError(f"work_class must be one of {sorted(WORK_CLASSES)}, got {value!r}")
    return v


def seat_strings(board_config: Optional[dict]) -> dict[str, dict[str, str]]:
    """Seat model/provider strings: config re-points over shipped defaults."""
    seats = {k: dict(v) for k, v in DEFAULT_SEATS.items()}
    try:
        cfg_seats = (board_config or {}).get("kanban", {}).get("assignment", {}).get("seats") or {}
    except AttributeError:
        cfg_seats = {}
    if isinstance(cfg_seats, dict):
        for name, entry in cfg_seats.items():
            if name in seats and isinstance(entry, dict):
                for key in ("model", "provider"):
                    if isinstance(entry.get(key), str) and entry[key]:
                        seats[name][key] = entry[key]
    return seats


def frontier_primary(board_config: Optional[dict]) -> str:
    try:
        primary = (board_config or {}).get("kanban", {}).get("assignment", {}).get("frontier_primary")
    except AttributeError:
        primary = None
    return primary if primary in ("argon", "opus") else "argon"


def frontier_alternate(board_config: Optional[dict]) -> str:
    return "opus" if frontier_primary(board_config) == "argon" else "argon"


def seat_for_class(seat_class: str, board_config: Optional[dict], *, reasoning_effort: Optional[str] = None) -> Seat:
    """Resolve a seat class ('argon'|'opus'|'cheap'|'frontier') to strings."""
    if seat_class == "frontier":
        seat_class = frontier_primary(board_config)
    strings = seat_strings(board_config)[seat_class]
    return Seat(model=strings["model"], provider=strings["provider"], reasoning_effort=reasoning_effort)


def seat_class(model: Optional[str], provider: Optional[str], board_config: Optional[dict] = None) -> str:
    """Classify a (model, provider) pair: 'argon' | 'opus' | 'cheap' | 'other'."""
    if not model and not provider:
        return "other"
    seats = seat_strings(board_config)
    for name in ("argon", "opus", "cheap"):
        s = seats[name]
        if model == s["model"] and (provider is None or provider == s["provider"]):
            return name
    m = (model or "").lower()
    p = (provider or "").lower()
    if "bonsai" in m or "bonsai" in p or "deepseek" in m:
        return "cheap"
    if "argon" in m:
        return "argon"
    if "opus" in m:
        return "opus"
    return "other"


def classify_sa_work_class(decision_row: Any, triggers: Any, assignment_cfg: Optional[dict]) -> str:
    """SA classification precedence (§3.1): basis.work_class > sa_rule_map >
    source default.  All inputs are already-recorded DB fields; no model call."""
    cfg = assignment_cfg or {}
    basis: dict = {}
    matched: list = []
    try:
        raw_reason = decision_row["reason_json"] if decision_row is not None else None
    except Exception:
        raw_reason = None
    if raw_reason:
        try:
            parsed = json.loads(raw_reason)
            if isinstance(parsed, dict):
                basis = parsed
        except Exception:
            basis = {}
    try:
        raw_matched = decision_row["matched_rules_json"] if decision_row is not None else None
    except Exception:
        raw_matched = None
    if raw_matched:
        try:
            parsed_m = json.loads(raw_matched)
            if isinstance(parsed_m, list):
                matched = [str(x) for x in parsed_m]
        except Exception:
            matched = []
    # 1. basis.work_class
    wc = basis.get("work_class")
    if isinstance(wc, str) and wc in WORK_CLASSES:
        return wc
    # 2. sa_rule_map against matched rule ids, in matched order
    rule_map = cfg.get("sa_rule_map") or {}
    if isinstance(rule_map, dict):
        for rid in matched:
            mapped = rule_map.get(rid)
            if isinstance(mapped, str) and mapped in WORK_CLASSES:
                return mapped
    # 3. source default
    defaults = dict(DEFAULT_SA_SOURCE_DEFAULTS)
    cfg_defaults = cfg.get("sa_source_defaults") or {}
    if isinstance(cfg_defaults, dict):
        for k, v in cfg_defaults.items():
            if isinstance(v, str) and v in WORK_CLASSES:
                defaults[str(k)] = v
    source = ""
    rows = list(triggers or [])
    if rows:
        try:
            source = (rows[0]["source"] or "").strip().lower()
        except Exception:
            source = ""
    return defaults.get(source) or defaults.get("default") or "personal"


def _profile_config_for(assignee: Optional[str]) -> Optional[dict]:
    """Parsed config of the target profile's home (a config READ — call sites
    only, never inside a txn).  None when unreadable."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_constants import (
            get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override,
        )
        root = get_default_hermes_root()
        home = root if not assignee or assignee == "default" else root / "profiles" / assignee
        token = set_hermes_home_override(str(home))
        try:
            import copy
            return copy.deepcopy(load_config_readonly())
        finally:
            reset_hermes_home_override(token)
    except Exception:
        return None


def seat_resolvable(seat: Seat, assignee: Optional[str]) -> bool:
    """§3.2 applicability guard: a pin is applied only if the seat's provider
    resolves for the target profile.  Built-in providers always resolve;
    ``custom:<name>`` must be registered in the target profile's config.
    A NULL assignee resolves against the board/default home (the dispatch
    default profile), which carries the shared custom_providers registry."""
    provider = seat.provider or ""
    if not provider.startswith("custom:"):
        # FIX-8: non-custom providers are resolvable iff the name is a known
        # built-in (the set is actually consulted); unknown names take the
        # unresolvable path (§3.2/§4.3).
        return provider.lower() in _BUILTIN_PROVIDERS
    name = provider[len("custom:"):]
    cfg = _profile_config_for(assignee)
    if cfg is None:
        return False
    custom = cfg.get("custom_providers") or []
    if isinstance(custom, list):
        for entry in custom:
            if isinstance(entry, dict) and entry.get("name") == name:
                return True
            if isinstance(entry, str) and entry == name:
                return True
    providers = cfg.get("providers") or {}
    if isinstance(providers, dict) and name in providers:
        return True
    return False


def resolve_assignment(work_class: Optional[str], *, board_config: Optional[dict],
                       explicit: Optional[ExplicitOverrides] = None,
                       assignee: Optional[str] = None,
                       sa_path: bool = False) -> CreationPlan:
    """Precompute the creation assignment for a card (call site, BEFORE the
    creation txn opens — B-9).  Explicit caller pins win per-field (§3.3)."""
    explicit = explicit or ExplicitOverrides()
    effective = validate_work_class(work_class) or "general"
    seat_cls, rule_reasoning = RULE_TABLE[effective]
    has_explicit_pair = explicit.model is not None
    has_explicit_reasoning = explicit.reasoning_effort is not None

    if seat_cls is None and not has_explicit_pair and not has_explicit_reasoning:
        # No-pin rule row.  SA creations still record the classification.
        return CreationPlan(work_class=effective, kind="profile_default" if sa_path else "none",
                            source="profile_default", emit_event=sa_path)

    model = provider = reasoning = None
    wrote_rule = False
    wrote_explicit = False
    if has_explicit_pair:
        wrote_explicit = True  # the row already carries the caller's pair; hook writes nothing for it
    elif seat_cls is not None:
        seat = seat_for_class(seat_cls, board_config, reasoning_effort=rule_reasoning)
        if not seat_resolvable(seat, assignee):
            return CreationPlan(work_class=effective, kind="skipped_unresolvable",
                                source="rule", emit_event=True)
        model, provider = seat.model, seat.provider
        wrote_rule = True
    if has_explicit_reasoning:
        wrote_explicit = True
    elif seat_cls is not None:
        reasoning = rule_reasoning
        wrote_rule = wrote_rule or reasoning is not None

    if model is None and provider is None and reasoning is None:
        # Everything explicit: hook writes nothing but records the event.
        return CreationPlan(work_class=effective, kind="pin", source="explicit", emit_event=True)
    source = "mixed" if (wrote_rule and wrote_explicit) else ("explicit" if wrote_explicit else "rule")
    emit = seat_cls is not None or has_explicit_pair or has_explicit_reasoning
    return CreationPlan(work_class=effective, kind="pin", model=model, provider=provider,
                        reasoning_effort=reasoning, source=source, emit_event=emit)


def apply_creation_assignment(conn: Any, task_id: str, plan: CreationPlan) -> None:
    """The ``create_task(assignment_hook=...)`` implementation.  Runs INSIDE the
    caller's already-open creation write_txn: raw UPDATEs + ``_append_event``
    ONLY — no config reads, no file I/O, no own-txn helpers (B-9)."""
    from hermes_cli import kanban_db as _kb

    existing = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind IN "
        "('assignment_applied', 'assignment_skipped_unresolvable') LIMIT 1", (task_id,),
    ).fetchone()
    if existing:
        return  # idempotency (§3.3): at most one assignment outcome per task
    if plan.kind == "skipped_unresolvable":
        _kb._append_event(conn, task_id, "assignment_skipped_unresolvable",
                          {"work_class": plan.work_class, "source": "rule"})
        return
    if plan.kind == "pin":
        sets, params = [], []
        if plan.model is not None:
            sets.append("model_override = ?")
            params.append(plan.model)
            sets.append("provider_override = ?")
            params.append(plan.provider)
        if plan.reasoning_effort is not None:
            sets.append("reasoning_effort = ?")
            params.append(plan.reasoning_effort)
        if sets:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
    if plan.emit_event:
        row = conn.execute(
            "SELECT model_override, provider_override, reasoning_effort FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        _kb._append_event(conn, task_id, "assignment_applied", {
            "work_class": plan.work_class,
            "source": plan.source,
            "model": row["model_override"] if row else plan.model,
            "provider": row["provider_override"] if row else plan.provider,
            "reasoning_effort": row["reasoning_effort"] if row else plan.reasoning_effort,
        })
