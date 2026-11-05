"""Enforced script gates for kanban cards (DESIGN rev 4, §2).

Pure with respect to the DB: this module never opens a transaction and never
writes the DB.  The caller (``complete_task``) owns all transactions, the
gate-evaluation lease, and every failure sequence (§2.4).

Fingerprint definition (§2.2, rev 3 B-2 — the ONLY definition): a filesystem
manifest over every file under the workspace (excluding ``.git/`` subtrees,
caller-supplied exclusion roots, and the task spec's ``fingerprint_exclude``
globs), content-hashed in the strong form, ``(path, size, mtime_ns)`` in the
weak form when the caps are exceeded, plus the git HEAD sha when present.

Errata (T4-CLOSEOUT F-3): the G6a freshness guarantee is bounded per file —
an undetected mutation window runs from each file's hash moment through the
completion transaction's commit (Stage-1 walk + txn duration).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# Same value as goals.py's _GATE_OUTPUT_TAIL_CHARS, duplicated deliberately (§7).
_GATE_OUTPUT_TAIL_CHARS = 3000

_MAX_FILES_STRONG = 10_000
_MAX_BYTES_STRONG = 2 * 1024 * 1024 * 1024  # 2 GiB
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GateSpecError(ValueError):
    """A stored gate spec failed validation at completion time (``spec_corrupt``)."""


@dataclass
class CheckResult:
    id: str
    argv: list
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    output_tail: str
    classification: str  # "pass" | "worker" | "environment"
    env_reason: Optional[str] = None
    skipped: bool = False


@dataclass
class GateRunResult:
    spec_hash: str
    results: list
    verdict: str  # "pass" | "worker_fail" | "env_fail"
    started_at: int
    duration_ms: int
    fingerprint: dict          # AFTER fingerprint
    pre_gate_fingerprint: dict  # PRE-GATE fingerprint (fix-attempt / anchor state)
    env_reason: Optional[str] = None
    failed_check: Optional[str] = None
    # Manifest entries (path, size, content-hash-or-mtime) for the pre-gate and
    # after states; used by the caller's fix-attempt comparison (F-1 check-written
    # exclusion).  Not serialized into events wholesale.
    pre_entries: list = field(default_factory=list)
    after_entries: list = field(default_factory=list)
    check_written_paths: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Validation (§2.1)
# --------------------------------------------------------------------------

_TOP_KEYS = {"version", "checks", "fingerprint_exclude"}
_CHECK_KEYS = {"id", "argv", "timeout_seconds"}


def spec_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_exclude_glob(glob: Any) -> str:
    if not isinstance(glob, str) or not glob:
        raise ValueError("fingerprint_exclude entries must be non-empty strings")
    if len(glob) > 200:
        raise ValueError("fingerprint_exclude entry exceeds 200 chars")
    if glob.startswith("/"):
        raise ValueError(f"fingerprint_exclude entry {glob!r} must be relative")
    if any(ch in glob for ch in ("\x00", "\n", "\r")):
        raise ValueError("fingerprint_exclude entry contains a forbidden character")
    if ".." in glob.replace("\\", "/").split("/"):
        raise ValueError(f"fingerprint_exclude entry {glob!r} contains a '..' segment")
    return glob


def validate_gate_spec(value: Any, *, default_timeout_seconds: int = 300) -> Optional[str]:
    """Validate a gate spec; return its canonical JSON string, or None when the
    argument was not supplied (``None`` -> stores NULL, no gate).

    Any other input is "present" and must validate in full or raise ValueError
    (F-13: ``""``, whitespace, ``"null"``, ``[]`` and ``{}`` are all errors).
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("gate_spec: empty string is not a valid spec (omit the argument for no spec)")
        try:
            value = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"gate_spec: not valid JSON: {exc}") from exc
    if isinstance(value, (list, tuple)):
        raise ValueError("gate_spec: must be an object with version + checks, not a list")
    if not isinstance(value, dict):
        raise ValueError(f"gate_spec: must be an object, got {type(value).__name__}")
    if not value:
        raise ValueError("gate_spec: empty object is not a valid spec (omit the argument for no spec)")
    unknown = set(value) - _TOP_KEYS
    if unknown:
        raise ValueError(f"gate_spec: unknown top-level key(s): {sorted(unknown)}")
    if value.get("version") != 1:
        raise ValueError("gate_spec: version must be exactly 1")
    checks = value.get("checks")
    if not isinstance(checks, list) or not (1 <= len(checks) <= 8):
        raise ValueError("gate_spec: checks must be a list of 1-8 checks")
    exclude = value.get("fingerprint_exclude", [])
    if not isinstance(exclude, list) or len(exclude) > 32:
        raise ValueError("gate_spec: fingerprint_exclude must be a list of at most 32 globs")
    exclude = [_validate_exclude_glob(g) for g in exclude]

    seen_ids: set[str] = set()
    canon_checks: list[dict] = []
    total_timeout = 0
    for raw in checks:
        if not isinstance(raw, dict):
            raise ValueError("gate_spec: each check must be an object")
        cunknown = set(raw) - _CHECK_KEYS
        if cunknown:
            raise ValueError(f"gate_spec: unknown key(s) in check {raw.get('id')!r}: {sorted(cunknown)}")
        cid = raw.get("id")
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            raise ValueError(f"gate_spec: invalid check id {cid!r}")
        if cid in seen_ids:
            raise ValueError(f"gate_spec: duplicate check id {cid!r}")
        seen_ids.add(cid)
        argv = raw.get("argv")
        if isinstance(argv, str) or not isinstance(argv, list) or not argv:
            raise ValueError(f"gate_spec check {cid!r}: argv must be a non-empty array of strings (shell strings are rejected)")
        for el in argv:
            if not isinstance(el, str) or not el:
                raise ValueError(f"gate_spec check {cid!r}: argv elements must be non-empty strings")
            if any(ch in el for ch in ("\x00", "\n", "\r")):
                raise ValueError(f"gate_spec check {cid!r}: argv element contains a forbidden character")
        argv0 = argv[0]
        if "/" in argv0 and not argv0.startswith("/"):
            raise ValueError(
                f"gate_spec check {cid!r}: argv[0] {argv0!r} must be a bare executable name or an absolute path")
        timeout = raw.get("timeout_seconds", default_timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not (1 <= timeout <= 1800):
            raise ValueError(f"gate_spec check {cid!r}: timeout_seconds must be an integer 1-1800")
        total_timeout += timeout
        canon_checks.append({"argv": list(argv), "id": cid, "timeout_seconds": timeout})
    if total_timeout > 3600:
        raise ValueError(f"gate_spec: sum of check timeouts ({total_timeout}) exceeds 3600")
    canon = {"checks": canon_checks, "fingerprint_exclude": exclude, "version": 1}
    return json.dumps(canon, sort_keys=True, separators=(",", ":"))


def spec_timeouts(canonical: str) -> list[int]:
    """Effective per-check timeouts of a canonical spec (for the lease TTL)."""
    try:
        parsed = json.loads(canonical)
        return [int(c.get("timeout_seconds", 300)) for c in parsed.get("checks", [])]
    except Exception:
        return []


def spec_exclude_globs(canonical: Optional[str]) -> list[str]:
    if not canonical:
        return []
    try:
        parsed = json.loads(canonical)
        globs = parsed.get("fingerprint_exclude") or []
        return [g for g in globs if isinstance(g, str)]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Workspace fingerprint (§2.2)
# --------------------------------------------------------------------------

def _excluded_by_glob(rel_posix: str, globs: Sequence[str]) -> bool:
    parts = rel_posix.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for glob in globs:
        for cand in prefixes:
            if fnmatch.fnmatchcase(cand, glob):
                return True
    return False


def _enumerate(cwd: str, exclude_roots: Sequence[Path], globs: Sequence[str]):
    """First pass: sorted (rel_posix, size, mtime_ns) for every included file."""
    root = Path(cwd)
    resolved_roots: list[Path] = []
    for r in exclude_roots or ():
        try:
            resolved_roots.append(Path(r).resolve())
        except Exception:
            continue
    entries: list[tuple[str, int, int]] = []
    total = 0
    if not root.is_dir():
        return entries, total
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        dpath = Path(dirpath)
        # Prune excluded roots early.
        pruned = []
        for d in list(dirnames):
            cand = dpath / d
            try:
                rc = cand.resolve()
            except Exception:
                continue
            if any(rc == rr or rr in rc.parents for rr in resolved_roots):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)
        for name in sorted(filenames):
            fpath = dpath / name
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                rf = fpath.resolve()
            except Exception:
                rf = fpath
            if any(rf == rr or rr in rf.parents for rr in resolved_roots):
                continue
            if globs and _excluded_by_glob(rel, globs):
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            # FIX-13: skip non-regular files (FIFO/socket/device) via stat —
            # opening a FIFO for content hashing would block forever.
            if not stat_is_regular(st.st_mode):
                continue
            entries.append((rel, int(st.st_size), int(st.st_mtime_ns)))
            total += int(st.st_size)
    entries.sort(key=lambda e: e[0])
    return entries, total


def stat_is_regular(mode: int) -> bool:
    import stat as _stat
    return _stat.S_ISREG(mode)


def _content_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        # Belt-and-braces (FIX-13): never open() a non-regular file.
        if not stat_is_regular(path.stat().st_mode):
            return "unreadable"
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "unreadable"


def _manifest_hash(entries: list) -> str:
    ser = json.dumps(entries, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode("utf-8")).hexdigest()


def _git_head(cwd: str) -> tuple[Optional[str], bool]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None, False
    if proc.returncode != 0:
        return None, False
    sha = (proc.stdout or "").strip()
    if _SHA_RE.match(sha):
        return sha, True
    return None, False


def _fingerprint_and_entries(cwd: str, *, exclude_roots: Sequence[Path] = (),
                             fingerprint_exclude: Sequence[str] = (),
                             force_strong: bool = False):
    """(fingerprint dict, manifest entries) — entries are (rel, size, hash) in
    the strong form and (rel, size, mtime_ns) in the weak form."""
    raw_entries, total = _enumerate(cwd, exclude_roots, fingerprint_exclude)
    weak = (not force_strong) and (len(raw_entries) > _MAX_FILES_STRONG or total > _MAX_BYTES_STRONG)
    root = Path(cwd)
    if weak:
        entries = [(rel, size, mtime) for (rel, size, mtime) in raw_entries]
    else:
        entries = [(rel, size, _content_hash(root / rel)) for (rel, size, _mtime) in raw_entries]
    head_sha, is_git = _git_head(cwd)
    fp = {
        "head_sha": head_sha,
        "manifest_hash": _manifest_hash(entries),
        "is_git": is_git,
        "weak": bool(weak),
    }
    return fp, entries


def workspace_fingerprint(cwd: str, *, exclude_roots: Sequence[Path] = (),
                          fingerprint_exclude: Sequence[str] = ()) -> dict:
    """THE workspace fingerprint (§2.2).  Never raises."""
    try:
        fp, _entries = _fingerprint_and_entries(
            cwd, exclude_roots=exclude_roots, fingerprint_exclude=fingerprint_exclude)
        return fp
    except Exception:
        return {"head_sha": None, "manifest_hash": _manifest_hash([]), "is_git": False, "weak": False}


def entries_hash_excluding(entries: list, exclude_paths: set) -> str:
    """Manifest hash over ``entries`` with the given relative paths removed —
    the F-1 check-written comparison hash."""
    kept = [e for e in entries if e[0] not in exclude_paths]
    return _manifest_hash(kept)


def check_written_paths(pre_entries: list, after_entries: list) -> list[str]:
    """Paths whose manifest entry appeared, disappeared, or changed between the
    pre-gate and after states of one attempt — i.e. files the checks themselves
    wrote during that attempt (T4-CLOSEOUT F-1)."""
    pre = {e[0]: e for e in pre_entries}
    post = {e[0]: e for e in after_entries}
    written = [p for p in set(pre) | set(post) if pre.get(p) != post.get(p)]
    return sorted(written)


def fingerprints_equal(a: Optional[dict], b: Optional[dict]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return all(a.get(k) == b.get(k) for k in ("head_sha", "manifest_hash", "is_git", "weak"))


# --------------------------------------------------------------------------
# Runner (§2.2 / §2.4)
# --------------------------------------------------------------------------

def classify_spawn_error(exc: BaseException) -> str:
    """§2.4 spawn taxonomy for OSError-class spawn failures."""
    if isinstance(exc, FileNotFoundError):
        return "command_missing"
    if isinstance(exc, PermissionError):
        return "command_not_executable"
    return "spawn_oserror"


def run_gate_spec(canonical: str, *, cwd: Optional[str],
                  lease_renew: Optional[Callable[[], bool]] = None,
                  exclude_roots: Sequence[Path] = ()) -> GateRunResult:
    """Run a canonical gate spec sequentially in ``cwd`` (shell=False), stopping
    at the first non-pass.  DB-free; the caller owns the lease and all txns."""
    started = time.time()
    started_at = int(started)
    try:
        parsed = json.loads(canonical)
        checks = parsed["checks"]
        globs = parsed.get("fingerprint_exclude") or []
    except Exception as exc:
        raise GateSpecError(f"stored gate_spec is corrupt: {exc}") from exc
    s_hash = spec_hash(canonical)

    def _finish(results, verdict, env_reason=None, failed=None,
                pre_fp=None, pre_entries=None, after_fp=None, after_entries=None):
        if after_fp is None and cwd and os.path.isdir(cwd):
            after_fp, after_entries = _fingerprint_and_entries(
                cwd, exclude_roots=exclude_roots, fingerprint_exclude=globs)
        return GateRunResult(
            spec_hash=s_hash, results=results, verdict=verdict,
            started_at=started_at, duration_ms=int((time.time() - started) * 1000),
            fingerprint=after_fp or {"head_sha": None, "manifest_hash": _manifest_hash([]),
                                     "is_git": False, "weak": False},
            pre_gate_fingerprint=pre_fp or {"head_sha": None, "manifest_hash": _manifest_hash([]),
                                            "is_git": False, "weak": False},
            env_reason=env_reason, failed_check=failed,
            pre_entries=pre_entries or [], after_entries=after_entries or [],
            check_written_paths=check_written_paths(pre_entries or [], after_entries or []),
        )

    if not cwd or not os.path.isdir(cwd):
        results = [CheckResult(id=c["id"], argv=list(c["argv"]), exit_code=None, timed_out=False,
                               duration_ms=0, output_tail="", classification="environment",
                               env_reason="workspace_missing", skipped=True) for c in checks]
        return _finish(results, "env_fail", env_reason="workspace_missing")

    # Pre-gate fingerprint: the runner's FIRST action (§2.2, rev 4 N-5).
    pre_fp, pre_entries = _fingerprint_and_entries(
        cwd, exclude_roots=exclude_roots, fingerprint_exclude=globs)

    results: list[CheckResult] = []
    verdict = "pass"
    env_reason: Optional[str] = None
    failed: Optional[str] = None
    stopped = False
    for check in checks:
        cid = check["id"]
        argv = list(check["argv"])
        timeout = int(check.get("timeout_seconds", 300))
        if stopped:
            results.append(CheckResult(id=cid, argv=argv, exit_code=None, timed_out=False,
                                       duration_ms=0, output_tail="", classification="pass", skipped=True))
            continue
        # Lease renewal BEFORE each check spawn (B-5).
        if lease_renew is not None:
            try:
                renewed = bool(lease_renew())
            except Exception:
                renewed = False
            if not renewed:
                results.append(CheckResult(id=cid, argv=argv, exit_code=None, timed_out=False,
                                           duration_ms=0, output_tail="", classification="environment",
                                           env_reason="lease_lost", skipped=True))
                verdict, env_reason, failed, stopped = "env_fail", "lease_lost", cid, True
                continue
        # Workspace re-check before each spawn (F-15).
        if not os.path.isdir(cwd):
            results.append(CheckResult(id=cid, argv=argv, exit_code=None, timed_out=False,
                                       duration_ms=0, output_tail="", classification="environment",
                                       env_reason="workspace_missing", skipped=True))
            verdict, env_reason, failed, stopped = "env_fail", "workspace_missing", cid, True
            continue
        t0 = time.time()
        try:
            proc = subprocess.run(argv, cwd=cwd, shell=False, capture_output=True,
                                  text=True, encoding="utf-8", timeout=timeout, errors="replace")
            dur = int((time.time() - t0) * 1000)
            out = ((proc.stdout or "") + (proc.stderr or ""))[-_GATE_OUTPUT_TAIL_CHARS:]
            if proc.returncode == 0:
                results.append(CheckResult(id=cid, argv=argv, exit_code=0, timed_out=False,
                                           duration_ms=dur, output_tail=out, classification="pass"))
            else:
                results.append(CheckResult(id=cid, argv=argv, exit_code=int(proc.returncode),
                                           timed_out=False, duration_ms=dur, output_tail=out,
                                           classification="worker"))
                verdict, failed, stopped = "worker_fail", cid, True
        except subprocess.TimeoutExpired as exc:
            dur = int((time.time() - t0) * 1000)
            raw = b""
            if exc.output:
                raw += exc.output if isinstance(exc.output, bytes) else str(exc.output).encode("utf-8", "replace")
            if exc.stderr:
                raw += exc.stderr if isinstance(exc.stderr, bytes) else str(exc.stderr).encode("utf-8", "replace")
            out = raw.decode("utf-8", "replace")[-_GATE_OUTPUT_TAIL_CHARS:]
            results.append(CheckResult(id=cid, argv=argv, exit_code=None, timed_out=True,
                                       duration_ms=dur, output_tail=out, classification="worker"))
            verdict, failed, stopped = "worker_fail", cid, True
        except OSError as exc:
            dur = int((time.time() - t0) * 1000)
            reason = classify_spawn_error(exc)
            results.append(CheckResult(id=cid, argv=argv, exit_code=None, timed_out=False,
                                       duration_ms=dur, output_tail="", classification="environment",
                                       env_reason=reason))
            verdict, env_reason, failed, stopped = "env_fail", reason, cid, True
    return _finish(results, verdict, env_reason=env_reason, failed=failed,
                   pre_fp=pre_fp, pre_entries=pre_entries)
