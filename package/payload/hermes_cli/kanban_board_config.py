"""Board-config path for the Super Agent kanban sub-features (DESIGN rev 4, §5.1).

All gate / assignment / escalation configuration lives under the top-level
``kanban:`` block of the BOARD-OWNING home's config (``kanban_home()``, the
root/default profile home, ``~/.hermes``) — never in the completing process's
ambient profile config.  :func:`load_board_config` is the single read path.

The board home's ``config.yaml`` is read DIRECTLY through the low-level
config-document primitive (``hermes_cli.config_backend``), never through the
snapshot-intercepted ``load_config`` loaders: a managed worker's frozen
session snapshot is the authority for its own execution config, not for
board policy, so routing this read through it silently answered with the
wrong authority's config (FIX-DESIGN-REV2, Option D).  Only the ``kanban:``
section of the returned dict is board-authoritative.

Contract (rev 3, B-6): this loader NEVER raises for validation problems.  It
returns ``(config, validation_errors)``; a feature whose section carries any
error is DISABLED for its consumers, and errors surface once per process as a
``board_config_invalid`` task event on first task-scoped consumption.
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Optional

_log = logging.getLogger(__name__)

_SECTIONS = ("gates", "assignment", "escalation")

_GATES_KEYS = {
    "enabled", "max_attempts_per_run", "default_timeout_seconds",
    "env_failure_limit", "max_refused_runs", "apply_default_spec", "default_specs",
}
_ASSIGNMENT_KEYS = {"enabled", "frontier_primary", "seats", "sa_rule_map", "sa_source_defaults"}
_SEAT_NAMES = {"argon", "opus", "cheap"}
_ESCALATION_KEYS = {"enabled", "threshold", "ceilings"}

# Per-process set of (section, key) signatures already surfaced as task events.
_surfaced: set[tuple[str, str]] = set()

# Signature memo for the board home's raw config document (FIX-DESIGN-REV2
# §10.1, rules M-1..M-5): absolute path string -> (pre-read signature, raw
# parsed document).  Only successful reads of a mapping root are stored;
# failures are NEVER memoized.  The stored document is never handed out and
# never mutated — every caller works on a deepcopy, and the managed-overlay
# merge + ${VAR} expansion run per call AFTER memo retrieval (M-4).
_FILE_MEMO: dict[str, tuple[tuple, dict]] = {}
_FILE_MEMO_LOCK = threading.Lock()


class _BoardReadFailure(Exception):
    """A board-config read/parse failure that must take the B-6 breakage
    branch (defaults + one error per section, never a raise).  Carries the
    original exception so the breakage message stays byte-identical."""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


def _breakage_result(exc: BaseException) -> tuple[dict, list[dict]]:
    """The existing parse-breakage branch: DEFAULT_CONFIG + one error per
    section, logged at ERROR, never a raise (B-6)."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    _log.error("kanban board config unreadable; all kanban sub-features disabled: %s", exc)
    errors = [
        _err(section, "config", f"board config could not be loaded ({exc}); feature disabled")
        for section in _SECTIONS
    ]
    return copy.deepcopy(DEFAULT_CONFIG), errors


def _read_board_raw(path) -> dict:
    """Return the board home config's raw parsed document (``{}`` for an
    absent or empty file), signature-memoized per M-1..M-5.

    Raises :class:`_BoardReadFailure` for parse failures, unreadable files
    (OSError other than not-found), and a non-mapping root — none of which
    are memoized (M-3).  All file access goes through
    ``hermes_cli.config_backend`` (the sanctioned primitive); no direct
    stat/open of the config path happens here.
    """
    from hermes_cli.config_backend import config_version, read_config_doc

    key = str(path)
    with _FILE_MEMO_LOCK:
        try:
            sig = config_version(path)  # M-1: signature BEFORE the read
        except FileNotFoundError:
            _FILE_MEMO.pop(key, None)  # M-5: absent file evicts
            return {}
        except OSError as exc:
            raise _BoardReadFailure(exc) from exc
        hit = _FILE_MEMO.get(key)
        if hit is not None and hit[0] == sig:
            return copy.deepcopy(hit[1])  # M-2: stored object never handed out
        try:
            doc = read_config_doc(path)
        except FileNotFoundError:
            _FILE_MEMO.pop(key, None)  # M-5: deleted between stat and read
            return {}
        except Exception as exc:
            raise _BoardReadFailure(exc) from exc  # M-3: never memoized
        if doc is None:  # empty file: a successful read of {}
            doc = {}
        if not isinstance(doc, dict):
            # A parse-class failure for memo purposes (M-3): caching it
            # would freeze the breakage under its signature.
            raise _BoardReadFailure(
                ValueError(f"board config root is not a mapping (got {type(doc).__name__})"))
        _FILE_MEMO[key] = (sig, copy.deepcopy(doc))  # M-1/M-2: atomic store under lock
        return copy.deepcopy(doc)


def _err(section: str, key: str, message: str) -> dict:
    return {"section": section, "key": key, "message": message}


def _is_pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_gates(section: Any, errors: list[dict]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        errors.append(_err("gates", "gates", "kanban.gates must be a mapping"))
        return
    for key in section:
        if key not in _GATES_KEYS:
            errors.append(_err("gates", str(key), f"unknown kanban.gates key {key!r}"))
    for key in ("max_attempts_per_run", "default_timeout_seconds", "env_failure_limit", "max_refused_runs"):
        if key in section and not _is_pos_int(section[key]):
            errors.append(_err("gates", key, f"kanban.gates.{key} must be a positive integer"))
    if "default_timeout_seconds" in section and _is_pos_int(section.get("default_timeout_seconds")):
        if section["default_timeout_seconds"] > 1800:
            errors.append(_err("gates", "default_timeout_seconds",
                               "kanban.gates.default_timeout_seconds must be <= 1800"))
    if "default_specs" in section and not isinstance(section["default_specs"], dict):
        errors.append(_err("gates", "default_specs", "kanban.gates.default_specs must be a mapping"))


def _validate_assignment(section: Any, errors: list[dict]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        errors.append(_err("assignment", "assignment", "kanban.assignment must be a mapping"))
        return
    # Ruling 9: the work_class -> seat-class mapping is a code constant.  Any
    # config key whose effect would be remapping a class is a validation error
    # (which disables assignment; it never takes effect and never raises).
    if "rules" in section:
        errors.append(_err("assignment", "rules",
                           "kanban.assignment.rules is forbidden: the work_class -> seat-class "
                           "mapping is a code constant (ruling 9)"))
    for key in section:
        if key == "rules":
            continue
        if key not in _ASSIGNMENT_KEYS:
            errors.append(_err("assignment", str(key),
                               f"unknown kanban.assignment key {key!r} (per-class seat/reasoning "
                               "overrides are forbidden: the mapping is a code constant)"))
    primary = section.get("frontier_primary")
    if primary is not None and primary not in ("argon", "opus"):
        errors.append(_err("assignment", "frontier_primary",
                           "kanban.assignment.frontier_primary must be 'argon' or 'opus'"))
    seats = section.get("seats")
    if seats is not None:
        if not isinstance(seats, dict):
            errors.append(_err("assignment", "seats", "kanban.assignment.seats must be a mapping"))
        else:
            for name, entry in seats.items():
                if name not in _SEAT_NAMES:
                    errors.append(_err("assignment", f"seats.{name}",
                                       f"unknown seat {name!r}; seats are argon|opus|cheap"))
                    continue
                if not isinstance(entry, dict):
                    errors.append(_err("assignment", f"seats.{name}",
                                       f"kanban.assignment.seats.{name} must be a mapping"))
                    continue
                for ekey in entry:
                    if ekey == "reasoning_effort":
                        errors.append(_err("assignment", f"seats.{name}.reasoning_effort",
                                           "reasoning_effort inside a seat entry is forbidden: "
                                           "reasoning lives only in the code rule table (ruling 9)"))
                    elif ekey not in ("model", "provider"):
                        errors.append(_err("assignment", f"seats.{name}.{ekey}",
                                           f"unknown seat key {ekey!r}; seats carry model/provider only"))
    for key in ("sa_rule_map", "sa_source_defaults"):
        if key in section and not isinstance(section[key], dict):
            errors.append(_err("assignment", key, f"kanban.assignment.{key} must be a mapping"))


def _validate_escalation(section: Any, errors: list[dict]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        errors.append(_err("escalation", "escalation", "kanban.escalation must be a mapping"))
        return
    for key in section:
        if key not in _ESCALATION_KEYS:
            errors.append(_err("escalation", str(key), f"unknown kanban.escalation key {key!r}"))
    if "threshold" in section and not _is_pos_int(section["threshold"]):
        errors.append(_err("escalation", "threshold", "kanban.escalation.threshold must be a positive integer"))
    ceilings = section.get("ceilings")
    if ceilings is not None:
        if not isinstance(ceilings, dict):
            errors.append(_err("escalation", "ceilings", "kanban.escalation.ceilings must be a mapping"))
        else:
            for name, row in ceilings.items():
                if not isinstance(row, dict):
                    errors.append(_err("escalation", f"ceilings.{name}",
                                       f"kanban.escalation.ceilings.{name} must be a mapping"))
                    continue
                for ckey in row:
                    if ckey not in ("max_total_tokens", "max_runs"):
                        errors.append(_err("escalation", f"ceilings.{name}.{ckey}",
                                           f"unknown ceiling key {ckey!r}; ceilings are tokens + runs only"))
                for ckey in ("max_total_tokens", "max_runs"):
                    if ckey in row and not _is_pos_int(row[ckey]):
                        errors.append(_err("escalation", f"ceilings.{name}.{ckey}",
                                           f"kanban.escalation.ceilings.{name}.{ckey} must be a positive integer"))


_VALIDATORS = {"gates": _validate_gates, "assignment": _validate_assignment, "escalation": _validate_escalation}


def load_board_config() -> tuple[dict, list[dict]]:
    """Return ``(merged config, validation_errors)`` from the board-owning home.

    NEVER raises for validation problems (B-6).  On parse-level breakage the
    DEFAULT_CONFIG values (all three features OFF) are returned plus a
    validation error per section — fail-closed toward pre-feature behavior.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli.config import _deep_merge, _expand_env_vars

        # The board home is the ONLY input (FIX-DESIGN-REV2 §7): the path is
        # a pure function of kanban_home(), computed on every call.  No
        # Hermes-home override is consulted — it existed only to steer the
        # snapshot-intercepted loader, which this read no longer uses.
        path = _kb.kanban_home() / "config.yaml"
        try:
            raw = _read_board_raw(path)
        except _BoardReadFailure as failure:
            return _breakage_result(failure.original)

        raw_kanban = raw.get("kanban")
        if isinstance(raw_kanban, dict):
            file_kanban: dict = raw_kanban
        elif raw_kanban is None:
            # Absent block, or a bare ``kanban:`` head (YAML null): the
            # legacy pipeline's _deep_merge ignores a None override over a
            # dict section, so defaults stand with no error either way.
            file_kanban = {}
        else:
            # Top-level kanban: present but not a mapping -> the existing
            # not-a-mapping error triple; validators are not run.
            config = copy.deepcopy(DEFAULT_CONFIG)
            errors = [
                _err(section, "kanban", "top-level kanban: block is not a mapping")
                for section in _SECTIONS
            ]
            return config, errors

        # M-4: merge + expansion run on EVERY call, after memo retrieval.
        # No dict-only guard at section level: a non-dict section value
        # clobbers through _deep_merge exactly as on the legacy path, so
        # the unchanged validators emit "must be a mapping" (P-2c).
        merged_kanban = _deep_merge(copy.deepcopy(DEFAULT_CONFIG["kanban"]),
                                    copy.deepcopy(file_kanban))
        # Managed-overlay parity (§8.2.1): an admin-pinned kanban: mapping
        # deep-merges on top, fresh on every call.  Fail-open, matching
        # load_managed_config's own posture.
        try:
            from hermes_cli.managed_scope import load_managed_config

            managed = load_managed_config()
        except Exception as exc:
            _log.warning("kanban board config: managed overlay unreadable, "
                         "proceeding with file values only: %s", exc)
            managed = None
        if isinstance(managed, dict) and "kanban" in managed:
            managed_kanban_value = managed.get("kanban")
            if isinstance(managed_kanban_value, dict):
                merged_kanban = _deep_merge(merged_kanban,
                                            copy.deepcopy(managed_kanban_value))
            else:
                _log.debug("kanban board config: managed kanban: block is not "
                           "a mapping; ignoring it")
        merged_kanban = _expand_env_vars(merged_kanban)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["kanban"] = merged_kanban
    except Exception as exc:  # parse-level breakage: defaults + errors, never a raise
        return _breakage_result(exc)

    kanban = config.get("kanban") if isinstance(config, dict) else None
    errors: list[dict] = []
    if not isinstance(kanban, dict):
        errors.append(_err("gates", "kanban", "top-level kanban: block is not a mapping"))
        errors.append(_err("assignment", "kanban", "top-level kanban: block is not a mapping"))
        errors.append(_err("escalation", "kanban", "top-level kanban: block is not a mapping"))
        return config, errors
    for section in _SECTIONS:
        _VALIDATORS[section](kanban.get(section), errors)
    for e in errors:
        _log.error("kanban board config invalid: [%s] %s: %s", e["section"], e["key"], e["message"])
    return config, errors


def section_errors(errors: list[dict], section: str) -> list[dict]:
    """The validation errors belonging to one section only (per-section isolation, B-6)."""
    return [e for e in errors if e.get("section") == section]


def feature_enabled(config: dict, errors: list[dict], section: str) -> bool:
    """The single enable condition for a sub-feature (§5): the section's
    ``enabled`` flag is true AND the section carries no validation error."""
    if section_errors(errors, section):
        return False
    kanban = config.get("kanban") if isinstance(config, dict) else None
    if not isinstance(kanban, dict):
        return False
    sec = kanban.get(section)
    if not isinstance(sec, dict):
        return False
    return sec.get("enabled") is True


def section_config(config: dict, section: str) -> dict:
    kanban = config.get("kanban") if isinstance(config, dict) else None
    if not isinstance(kanban, dict):
        return {}
    sec = kanban.get(section)
    return sec if isinstance(sec, dict) else {}


def surface_if_exercised(conn: Any, task_id: Optional[str], config: dict,
                         errors: list[dict], section: str) -> None:
    """Per-section surfacing (FIX-4, §5.1): a consumer surfaces ONLY its own
    section's errors, and only when that feature is enabled in config and the
    consumer is actually exercised.  A section whose ``enabled`` flag is not
    true emits nothing — disabled paths are byte-for-byte pre-feature."""
    sec = section_config(config, section)
    if sec.get("enabled") is not True:
        return
    if section_errors(errors, section):
        surface_errors(conn, task_id, errors, section)


def surface_errors(conn: Any, task_id: Optional[str], errors: list[dict], section: str) -> None:
    """Surface a section's errors as ONE ``board_config_invalid`` task event per
    (section, key) per process, on first task-scoped consumption (B-6).

    Caller must NOT hold a write txn: the event is appended in its own txn.
    Best-effort — a surfacing failure never breaks the consuming operation.
    """
    if task_id is None:
        return
    pending = [
        e for e in section_errors(errors, section)
        if (e["section"], e["key"]) not in _surfaced
    ]
    if not pending:
        return
    try:
        from hermes_cli import kanban_db as _kb

        with _kb.write_txn(conn):
            for e in pending:
                _kb._append_event(conn, task_id, "board_config_invalid",
                                  {"section": e["section"], "key": e["key"], "message": e["message"]})
        for e in pending:
            _surfaced.add((e["section"], e["key"]))
    except Exception as exc:  # surfacing must never break the consumer
        _log.debug("board_config_invalid surfacing failed: %s", exc)
