#!/usr/bin/env bash
# infra/host-restore.sh: reverse infra/host-setup.sh --tune (Fedora-only).
# Restore is mandatory in the runner's teardown/finally.
#
# RUNS ONLY ON THE FEDORA BOX. Replays the state-file ledger written by
# host-setup.sh in REVERSE ORDER, undoing every applied mutation. Idempotent:
# a missing/empty state file is a clean no-op. Each reversal emits one JSON line
# the runner ingests, so the manifest records that the host was returned to
# baseline. Best-effort per op (one failed restore must not abort the rest), but
# the script exits non-zero if ANY op failed so the runner can flag it.
#
# State-file record shape (JSON lines, appended by host-setup.sh):
#   {"ts":..,"op":"write-file",       "target":"<path>","original":"<value>"}
#   {"ts":..,"op":"restore-file-b64", "target":"<path>","original":"<base64 of content>"}
#   {"ts":..,"op":"delete-file",      "target":"<path>","original":"__absent__"}
#   {"ts":..,"op":"service-restart","target":"<svc>", "original":"active"}
#   {"ts":..,"op":"cpupower-idle",  "target":"all",    "original":"enabled"}
#   {"ts":..,"op":"chattr",         "target":"<path>","original":"+C-not-set"}
#
# restore-file is also accepted and handled like write-file. host-setup.sh does
# not write it: a whole-file original is recorded as restore-file-b64.

set -uo pipefail

STATE_FILE="${LEDGER_HOST_STATE:-/var/tmp/ledgerline-host-state.json}"
HAD_FAILURE=0

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
emit() {
  local action="$1" status="$2"; shift 2
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  local extra="" kv
  for kv in "$@"; do
    local k="${kv%%=*}" v="${kv#*=}"
    extra="${extra},\"$(json_escape "$k")\":\"$(json_escape "$v")\""
  done
  printf '{"ts":"%s","component":"host-restore","action":"%s","status":"%s"%s}\n' \
    "$ts" "$(json_escape "$action")" "$(json_escape "$status")" "$extra"
}

if [ "$(uname -s)" != "Linux" ]; then
  emit platform skipped "uname=$(uname -s)" "reason=linux-only"
  exit 0
fi
if [ ! -s "$STATE_FILE" ]; then
  emit state-file noop "path=$STATE_FILE" "reason=absent-or-empty"
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  emit root-check error "reason=restore-requires-root"
  echo "host-restore.sh must run as root." >&2
  exit 1
fi

emit run begin "path=$STATE_FILE"

# Minimal JSON-line field extractor (no jq dependency on the box). Each record
# is a single flat object with string values; pull a key's value by regex.
field() {
  # field <json-line> <key>
  printf '%s' "$1" | sed -n "s/.*\"$2\":\"\\([^\"]*\\)\".*/\\1/p"
}

# Replay in REVERSE order so nested/overlapping changes unwind correctly.
# `tac` reverses the line stream; fall back to sed if tac is unavailable.
reverse_lines() { tac "$1" 2>/dev/null || sed '1!G;h;$!d' "$1"; }

while IFS= read -r line; do
  [ -n "$line" ] || continue
  op="$(field "$line" op)"
  target="$(field "$line" target)"
  original="$(field "$line" original)"
  case "$op" in
    write-file|restore-file)
      if printf '%s' "$original" > "$target" 2>/dev/null; then
        emit "$op" restored "target=$target"
      else
        emit "$op" error "target=$target"; HAD_FAILURE=1
      fi
      ;;
    restore-file-b64)
      # A whole-FILE original (a pre-existing systemd drop-in) rides base64 so
      # its newlines cannot split the JSON line this parser reads.
      if printf '%s' "$original" | base64 -d > "$target" 2>/dev/null; then
        case "$target" in
          /etc/systemd/*) systemctl daemon-reexec >/dev/null 2>&1 || true ;;
        esac
        emit restore-file-b64 restored "target=$target"
      else
        emit restore-file-b64 error "target=$target"; HAD_FAILURE=1
      fi
      ;;
    delete-file)
      # Original was absent: remove the drop-in we created, then reload systemd.
      if rm -f "$target" 2>/dev/null; then
        case "$target" in
          /etc/systemd/*) systemctl daemon-reexec >/dev/null 2>&1 || true ;;
        esac
        emit delete-file restored "target=$target" "action=removed"
      else
        emit delete-file error "target=$target"; HAD_FAILURE=1
      fi
      ;;
    service-restart)
      if systemctl start "${target}.service" >/dev/null 2>&1; then
        emit service-restart restored "service=$target" "to=active"
      else
        emit service-restart error "service=$target"; HAD_FAILURE=1
      fi
      ;;
    cpupower-idle)
      if command -v cpupower >/dev/null 2>&1; then
        cpupower idle-set -E >/dev/null 2>&1 || true   # re-enable all idle states
        emit cpupower-idle restored "target=$target" "action=enable-all"
      else
        emit cpupower-idle skipped "reason=cpupower-absent"
      fi
      ;;
    chattr)
      # We only ever SET +C; reversal clears it (best-effort; only matters on
      # an emptied dir, otherwise the flag is sticky and harmless).
      if command -v chattr >/dev/null 2>&1; then
        chattr -C "$target" 2>/dev/null || true
        emit chattr restored "target=$target" "attr=-C"
      else
        emit chattr skipped "reason=chattr-absent"
      fi
      ;;
    *)
      emit unknown-op skipped "op=$op" "target=$target"
      ;;
  esac
done < <(reverse_lines "$STATE_FILE")

# Consume the ledger so a second restore is a clean no-op (idempotent).
if rm -f "$STATE_FILE" 2>/dev/null; then
  emit state-file cleared "path=$STATE_FILE"
else
  emit state-file error "path=$STATE_FILE" "reason=could-not-remove"; HAD_FAILURE=1
fi

emit run end "failures=$HAD_FAILURE"
[ "$HAD_FAILURE" -eq 0 ] || exit 1
