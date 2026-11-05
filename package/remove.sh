#!/bin/sh
# Remove the Hermes Super Agent package from a Hermes checkout.
#
# Usage: sh remove.sh TARGET
#
# Restores every patched file from the pristine backup taken by install.sh
# (TARGET/.superagent-pkg-backup/), deletes the payload + test payload
# files — restoring, instead of deleting, any pre-existing file that
# install.sh backed up under TARGET/.superagent-pkg-backup/payload-files/
# because a payload or test file overwrote it — and removes the backup
# directory.
#
# "Already removed" is reported (exit 0) only when no payload files
# exist, no backup directory exists, AND no patch content is detectable
# in the target (no "superagent" match in any patch-target file). If
# patch content IS present but there is no backup, removal falls through
# to the reverse-patch path; if any reverse patch fails and patch
# content is still detected afterwards, remove.sh reports the failure
# and exits 1 — it never claims a clean removal it did not achieve.
#
# Migration tables are LEFT IN PLACE by design: the sa_* tables and the
# mandate_id/ticket_kind tasks columns are additive-only, and dropping them
# would destroy recorded Super Agent data. They are inert when the layer is
# removed (nothing in stock Hermes reads them) and are re-used if the layer
# is installed again. This choice is documented in README-PACKAGE.md.
#
# Same live-install guard as install.sh: refuses the live checkout, the
# live home dir, and anything inside ~/.hermes.
set -eu

PKGDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=${1:-}

if [ -z "$TARGET" ]; then
  echo "usage: sh remove.sh TARGET" >&2
  exit 2
fi
if [ ! -d "$TARGET" ]; then
  echo "remove: target is not a directory: $TARGET" >&2
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
  echo "remove: REFUSED — target is the live Hermes install ($RESOLVED_TARGET)." >&2
  exit 3
fi
case "$RESOLVED_TARGET" in
  "$LIVE_CHECKOUT"/*|"$LIVE_HOME"/*)
    echo "remove: REFUSED — target is inside the live Hermes install." >&2
    exit 3
    ;;
esac

BACKUP="$TARGET/.superagent-pkg-backup"

# --- Collect payload files (payload minus manifest, plus tests). ---
PAYLOAD_LIST=$(mktemp)
PATCH_LIST=$(mktemp)
trap 'rm -f "$PAYLOAD_LIST" "$PATCH_LIST"' EXIT INT TERM
( cd "$PKGDIR/payload" && find . -type f ! -name manifest.json -print ) | sed 's|^\./||' >> "$PAYLOAD_LIST"
( cd "$PKGDIR/tests" && find . -type f -print ) | sed 's|^\./||' >> "$PAYLOAD_LIST"

# --- Collect patch-target files and detect patch content in them. ---
for p in "$PKGDIR"/core-patches/*.patch; do
  grep '^+++ b/' "$p" | sed 's|^+++ b/||' >> "$PATCH_LIST"
done

detect_patch_content() {
  PATCH_CONTENT=0
  while IFS= read -r tf; do
    [ -n "$tf" ] || continue
    [ "$tf" = "/dev/null" ] && continue
    if [ -f "$TARGET/$tf" ] && grep -qi 'superagent' "$TARGET/$tf" 2>/dev/null; then
      PATCH_CONTENT=1
      break
    fi
  done < "$PATCH_LIST"
}

FOUND=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if [ -f "$TARGET/$f" ]; then
    FOUND=1
    break
  fi
done < "$PAYLOAD_LIST"

detect_patch_content

if [ "$FOUND" -eq 0 ] && [ ! -d "$BACKUP" ] && [ "$PATCH_CONTENT" -eq 0 ]; then
  echo "remove: nothing to remove (no payload files, no backup, no patch content) — already removed."
  exit 0
fi

# --- Delete payload files, restoring pre-existing files from backup. ---
# For each payload path: if install.sh backed up a pre-existing file at
# $BACKUP/payload-files/<path>, restore that copy instead of deleting;
# otherwise delete the payload file.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if [ -f "$BACKUP/payload-files/$f" ]; then
    mkdir -p "$TARGET/$(dirname -- "$f")"
    cp -p "$BACKUP/payload-files/$f" "$TARGET/$f"
  else
    rm -f "$TARGET/$f"
  fi
done < "$PAYLOAD_LIST"
rmdir "$TARGET/tests/superagent" 2>/dev/null || true
rmdir "$TARGET/tests/hermes_cli" 2>/dev/null || true
rmdir "$TARGET/tests/tui_gateway" 2>/dev/null || true
rmdir "$TARGET/prompts/superagent" 2>/dev/null || true
rmdir "$TARGET/prompts" 2>/dev/null || true
echo "remove: payload + test payload files deleted (pre-existing files restored from backup where present)."

# --- Restore patched files. ---
if [ -d "$BACKUP" ]; then
  if [ -d "$BACKUP/files" ]; then
    tar -cf - -C "$BACKUP/files" . | tar -xf - -C "$TARGET"
  else
    # Pre-fix backup layout (state file era, files at backup root): restore
    # everything except the state file.
    tar --exclude applied-patches.txt -cf - -C "$BACKUP" . | tar -xf - -C "$TARGET"
    rm -f "$TARGET/applied-patches.txt"
  fi
  rm -rf "$BACKUP"
  echo "remove: patched files restored from pristine backup."
else
  echo "remove: WARNING — no pristine backup found; attempting reverse patches (best effort)."
  REVERSE_FAILED=0
  for p in "$PKGDIR"/core-patches/*.patch; do
    # -i + stdin /dev/null: prompts (if any) hit EOF and fail safe instead of
    # reading patch text as answers.
    patch -p1 -R -s -d "$TARGET" -i "$p" < /dev/null 2>/dev/null || \
      { echo "remove: reverse patch did not apply (may already be reverted): $(basename -- "$p")"; REVERSE_FAILED=1; }
  done
  if [ "$REVERSE_FAILED" -ne 0 ]; then
    detect_patch_content
    if [ "$PATCH_CONTENT" -ne 0 ]; then
      echo "remove: ERROR — reverse patches failed and patch content is still present in $TARGET; removal is NOT complete." >&2
      exit 1
    fi
    echo "remove: reverse patches reported failures, but no patch content remains; treating as already reverted."
  fi
fi

echo "remove: done. NOTE — migration tables left in place by design:"
echo "remove: sa_* tables and the tasks.mandate_id / tasks.ticket_kind columns are"
echo "remove: additive-only and inert under stock Hermes; dropping them would destroy"
echo "remove: recorded data, so remove.sh never drops them."
