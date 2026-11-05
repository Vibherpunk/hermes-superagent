#!/bin/sh
# Install the Hermes Super Agent package into a Hermes checkout.
#
# Usage: sh install.sh TARGET
#   TARGET  path to a Hermes checkout (NOT the live install).
#   Any other argument is rejected with a usage message and exit code 2.
#   No database is touched at install time: the patched code
#   self-migrates on next DB open (it creates the sa_* tables itself,
#   IF NOT EXISTS, and adds the two tasks columns via its
#   add-if-missing column path).
#
# What install does NOT do (by design):
#   - It never enables the layer. Defaults ship superagent.enabled=false
#     and accept_mode=observe. Enabling requires editing your own config.
#   - It never writes to a Hermes home dir or config file.
#   - It refuses to run against the live checkout ~/.hermes/hermes-agent,
#     the live home dir ~/.hermes, or anything inside ~/.hermes
#     (see the guard below).
#
# Idempotent: re-running copies the same files and skips patches recorded
# in the backup dir's applied-patches.txt. A pristine copy of every
# patched file is kept in TARGET/.superagent-pkg-backup/ (taken on the
# first install only) so remove.sh can restore the tree exactly.
# Pre-existing files sitting at payload or test paths are backed up under
# TARGET/.superagent-pkg-backup/payload-files/ before they are
# overwritten; the earliest backup copy is kept (never overwritten), so
# it stays pristine across re-runs.
set -eu

PKGDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    -*)
      echo "install: unexpected argument: $1" >&2
      echo "usage: sh install.sh TARGET" >&2
      exit 2
      ;;
    *)
      if [ -z "$TARGET" ]; then
        TARGET=$1
      else
        echo "install: unexpected argument: $1" >&2
        echo "usage: sh install.sh TARGET" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "usage: sh install.sh TARGET" >&2
  exit 2
fi
if [ ! -d "$TARGET" ]; then
  echo "install: target is not a directory: $TARGET" >&2
  exit 2
fi

resolve() {
  python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

RESOLVED_TARGET=$(resolve "$TARGET")
LIVE_CHECKOUT=$(resolve "$HOME/.hermes/hermes-agent")
LIVE_HOME=$(resolve "$HOME/.hermes")

# --- Guard: never touch the live install. ---
if [ "$RESOLVED_TARGET" = "$LIVE_CHECKOUT" ] || [ "$RESOLVED_TARGET" = "$LIVE_HOME" ]; then
  echo "install: REFUSED — target is the live Hermes install ($RESOLVED_TARGET)." >&2
  echo "install: run against a separate checkout or worktree, never the live one." >&2
  exit 3
fi
case "$RESOLVED_TARGET" in
  "$LIVE_CHECKOUT"/*|"$LIVE_HOME"/*)
    echo "install: REFUSED — target is inside the live Hermes install." >&2
    exit 3
    ;;
esac

# --- Sanity: target must look like a Hermes checkout. ---
for f in hermes_cli/kanban_db.py gateway/run_inbound.py web/src/App.tsx toolsets.py; do
  if [ ! -f "$TARGET/$f" ]; then
    echo "install: target does not look like a Hermes checkout (missing $f)." >&2
    exit 2
  fi
done
command -v patch >/dev/null 2>&1 || { echo "install: 'patch' is required." >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "install: 'python3' is required." >&2; exit 2; }

BACKUP="$TARGET/.superagent-pkg-backup"

# --- Backup pristine copies of every file the patches touch (first run only). ---
# Pristine files live under $BACKUP/files/; the applied-patches state file
# lives alongside (NOT under files/), so restoring the backup never copies
# the state file itself into the target tree.
if [ ! -d "$BACKUP" ]; then
  mkdir -p "$BACKUP/files"
  for p in "$PKGDIR"/core-patches/*.patch; do
    grep '^+++ b/' "$p" | sed 's|^+++ b/||' | while IFS= read -r f; do
      [ "$f" = "/dev/null" ] && continue
      if [ -f "$TARGET/$f" ]; then
        mkdir -p "$BACKUP/files/$(dirname -- "$f")"
        cp -p "$TARGET/$f" "$BACKUP/files/$f"
      fi
    done
  done
  echo "install: pristine backup taken at $BACKUP"
else
  echo "install: keeping existing pristine backup at $BACKUP"
fi

# --- Backup pre-existing payload/test files about to be overwritten. ---
# Runs after $BACKUP exists and before the copies below, on the first run
# AND on re-runs. An existing payload backup is never overwritten: the
# earliest copy is the pristine one.
mkdir -p "$BACKUP/payload-files"
( cd "$PKGDIR/payload" && find . -type f ! -name manifest.json -print
  cd "$PKGDIR/tests" && find . -type f -print ) | sed 's|^\./||' | while IFS= read -r f; do
  [ -n "$f" ] || continue
  if [ -f "$TARGET/$f" ] && [ ! -e "$BACKUP/payload-files/$f" ]; then
    mkdir -p "$BACKUP/payload-files/$(dirname -- "$f")"
    cp -p "$TARGET/$f" "$BACKUP/payload-files/$f"
  fi
done
echo "install: pre-existing payload/test files backed up under $BACKUP/payload-files (earliest copy kept)."

# --- Copy payload modules and new test files (root-relative paths). ---
tar --exclude manifest.json -cf - -C "$PKGDIR/payload" . | tar -xf - -C "$TARGET"
tar -cf - -C "$PKGDIR/tests" . | tar -xf - -C "$TARGET"
echo "install: payload + test files copied."

# --- Apply core patches in documented order; skip already-applied ones. ---
# Applied-ness is tracked in the backup dir's state file, NOT probed with
# patch: this platform's patch asks interactive "Assume -R?" / "Ignore -R?"
# questions on a reverse/dry-run probe, and --forward re-application exits 1
# and litters .rej files (both verified empirically). The state file keeps
# re-runs deterministic and non-interactive. If the state file is missing
# but patches are already applied by hand, patch will fail loudly rather
# than silently double-applying — that is the intended fail-closed behavior.
STATE="$BACKUP/applied-patches.txt"
touch "$STATE"
for p in "$PKGDIR"/core-patches/*.patch; do
  name=$(basename -- "$p")
  if grep -qx "$name" "$STATE" 2>/dev/null; then
    echo "install: patch already applied, skipping: $name"
  else
    patch -p1 -s -d "$TARGET" < "$p"
    echo "$name" >> "$STATE"
    echo "install: patch applied: $name"
  fi
done

echo "install: no database touched; the patched code self-migrates on next DB open"
echo "install: (it creates the sa_* tables, IF NOT EXISTS, and adds the tasks"
echo "install: columns via its add-if-missing path when the database is opened)."
echo "install: done. Defaults: superagent.enabled=false, accept_mode=observe."
echo "install: the layer is INSTALLED but NOT ENABLED. To enable, edit your own"
echo "install: config (superagent.enabled=true); this script never writes config."
echo "install: panel note: the frontend sources are patched; rebuild the panel"
echo "install: assets with the checkout's normal web build before the Attention"
echo "install: page appears in the served panel."
