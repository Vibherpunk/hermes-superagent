"""Shared Super Agent inbound recording helpers (surfaces Rev 3, unit U3.1).

One implementation of the superagent config / kanban-DB resolution chain,
used by both the gateway inbound hook (``gateway/run_inbound.py``) and the
TUI/desktop seam (``tui_gateway/methods_prompt.py``), plus the bounded
synchronous writers for the local (desktop) surface:

- ``record_local_turn`` — record one accepted desktop/TUI turn as a
  ``source="local"`` trigger candidate.
- ``alias_thread_key`` — move thread subscriptions old -> new session_key
  when compression rotates a TUI session (unit U3b).

Contracts (binding, DESIGN Rev 3 §3/U3.1):
- Every public function here is **never-raise**: a failure loses a
  candidacy (logged), never the user's turn.
- The writers make a bounded synchronous attempt on a dedicated
  connection: opened via the standard kanban connect path, that
  connection's ``PRAGMA busy_timeout`` overridden to 150 ms, closed in a
  ``finally``. At return the row exists or it does not — no background
  landing, no queue, no executor.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_RECORD_BUSY_TIMEOUT_MS = 150


def resolve_superagent_config(
    *,
    config: dict | None = None,
    superagent: dict | None = None,
) -> dict | None:
    """Resolve the superagent config block; ``None`` when unavailable.

    The full chain, verbatim from the gateway hook's original
    ``_hm_resolve_superagent_config`` (run_inbound.py:1242 at
    3d1d704eff): (i) an in-memory ``config`` dict containing a
    ``superagent`` dict; (ii) an in-memory ``superagent`` dict;
    (iii) ``load_config_readonly()``'s ``superagent`` dict.
    A config-load failure is swallowed and yields ``None``.
    """
    try:
        cfg = config
        if isinstance(cfg, dict) and "superagent" in cfg and isinstance(cfg["superagent"], dict):
            return cfg["superagent"]
        if isinstance(superagent, dict):
            return superagent
        try:
            from hermes_cli.config import load_config_readonly

            full_cfg = load_config_readonly()
            if isinstance(full_cfg, dict) and isinstance(full_cfg.get("superagent"), dict):
                return full_cfg["superagent"]
        except Exception:  # fallback when config cannot be loaded
            logger.debug("Cannot load superagent config", exc_info=True)
        return None
    except Exception:  # never-raise contract
        logger.debug("resolve_superagent_config failed", exc_info=True)
        return None


def resolve_kanban_db_path(sa_cfg: dict | None, *fallbacks: Any) -> Path | None:
    """Resolve the kanban DB path; ``None`` rather than raising on failure.

    Order: the config's ``db_path`` / ``kanban_db``, then each fallback
    in order (callers pass their own attrs, e.g. the gateway runner's
    ``_superagent_db_path`` / ``_kanban_db_path``), then the standard
    ``kanban_db_path()`` resolution.
    """
    try:
        candidates: list[Any] = []
        if isinstance(sa_cfg, dict):
            candidates.append(sa_cfg.get("db_path"))
            candidates.append(sa_cfg.get("kanban_db"))
        candidates.extend(fallbacks)
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return Path(candidate)
        try:
            from hermes_cli.kanban_db import kanban_db_path

            return kanban_db_path()
        except Exception:  # fallback when kanban_db_path resolution fails
            logger.debug("Cannot resolve kanban DB path", exc_info=True)
            return None
    except Exception:  # never-raise contract
        logger.debug("resolve_kanban_db_path failed", exc_info=True)
        return None


def scope_for_profile_home(profile_home: Any) -> str:
    """Profile NAME for a profile home path, or ``"default"``.

    Never returns a filesystem path: ``None`` / the Hermes home ->
    ``"default"``; a home under ``profiles/<name>`` -> ``<name>``;
    anything else -> ``"default"``.
    """
    try:
        if profile_home is None or not str(profile_home).strip():
            return "default"
        try:
            from hermes_constants import profile_name_for_home

            name = profile_name_for_home(profile_home)
            if name:
                return str(name)
        except Exception:
            pass
        path = Path(str(profile_home))
        if path.parent.name == "profiles" and path.name and "/" not in path.name:
            return path.name
        return "default"
    except Exception:  # never-raise contract
        return "default"


def _pilot_set(sa_cfg: dict) -> set[str]:
    pilot = sa_cfg.get("pilot_surfaces") or []
    if isinstance(pilot, str):
        pilot = [pilot]
    return {
        str(getattr(s, "value", s)).strip().lower()
        for s in pilot
        if s
    }


def _local_armed(sa_cfg: dict | None) -> bool:
    return bool(sa_cfg) and bool(sa_cfg.get("enabled")) and "local" in _pilot_set(sa_cfg)


def _open_bounded_connection(db_path: Path):
    """A dedicated connection with this write's busy_timeout overridden.

    The standard connect path lives in ``hermes_cli.kanban_db_connect``
    (``kanban_db`` re-exports only ``init_db``/``write_txn`` from it);
    schema init, WAL, corruption quarantine and the tracked-connection
    registry all behave exactly as for every other kanban caller.
    Only THIS connection's busy_timeout is overridden (150 ms); every
    other connection keeps the 120 s default.
    """
    from hermes_cli.kanban_db_connect import connect

    conn = connect(db_path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_RECORD_BUSY_TIMEOUT_MS}")
    except Exception:
        logger.debug("superagent inbound: busy_timeout override failed", exc_info=True)
    return conn


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def record_local_turn(*, thread_key: str, body: str, scope: str) -> None:
    """Record one accepted local (desktop/TUI) turn as a trigger candidate.

    Bounded synchronous attempt on a dedicated connection (see module
    docstring). Never raises: any failure is logged and the candidacy
    is lost, never the turn.
    """
    try:
        if not thread_key or not isinstance(body, str) or not body.strip():
            return
        sa_cfg = resolve_superagent_config()
        if not _local_armed(sa_cfg):
            return
        db_path = resolve_kanban_db_path(sa_cfg)
        if db_path is None:
            return
        conn = _open_bounded_connection(db_path)
        try:
            from hermes_cli.orchestrator import receive_trigger

            receive_trigger(
                conn,
                source="local",
                scope=scope or "default",
                thread_key=thread_key,
                actor_source_id="local:desktop",
                actor_is_bot=False,
                addressed=True,
                kind="text",
                body=body,
                meta=None,
                correlation=None,
                idempotency_key=f"local:{thread_key}:{uuid4().hex}",
                now=time.time(),
            )
        finally:
            _close_quietly(conn)
    except Exception as exc:  # never break the submit path
        logger.warning("Failed to record superagent local turn (%s): %s", thread_key, exc)


def alias_thread_key(old_key: str, new_key: str) -> None:
    """Move subscriptions from ``old_key`` to ``new_key`` (unit U3b).

    Same gating as ``record_local_turn``: unless superagent is enabled
    and ``"local"`` is a pilot surface, no writes of any kind. The move
    is all-or-none inside one ``write_txn`` on a dedicated bounded
    connection, per addendum A31-1: the read of the rows to move is the
    FIRST statement inside the transaction; each moved row is then
    deleted per-row, keyed by ``(ticket_id, old_key)`` — a blanket
    drop-by-thread-key is never used, so a subscription that lands on
    the old key after the read survives untouched. Never raises.
    """
    try:
        if not old_key or not new_key or old_key == new_key:
            return
        sa_cfg = resolve_superagent_config()
        if not _local_armed(sa_cfg):
            return
        db_path = resolve_kanban_db_path(sa_cfg)
        if db_path is None:
            return
        conn = _open_bounded_connection(db_path)
        try:
            from hermes_cli.kanban_db_connect import write_txn
            from hermes_cli.superagent_subscriptions import subscribe, subscriptions_for

            now = int(time.time())
            with write_txn(conn, allow_nested=True):
                rows = subscriptions_for(conn, old_key)  # statement #1 — INSIDE the txn
                for row in rows:
                    subscribe(
                        conn,
                        ticket_id=row["ticket_id"],
                        thread_key=new_key,
                        context_only=row["context_only"],
                        now=now,
                    )
                for row in rows:
                    conn.execute(
                        "DELETE FROM sa_subscriptions WHERE thread_key = ? AND ticket_id = ?",
                        (old_key, row["ticket_id"]),
                    )
        finally:
            _close_quietly(conn)
    except Exception as exc:  # a lost alias degrades to an unowned candidacy, never a broken rotation
        logger.warning(
            "Failed to move superagent subscriptions %s -> %s: %s", old_key, new_key, exc
        )
