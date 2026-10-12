#!/usr/bin/env bash
# infra/host-setup.sh: Fedora-only host tuning for the Ledgerline benchmark.
#
# RUNS ONLY ON THE FEDORA MEASUREMENT BOX. Never executed on a development machine
# or in CI. It mutates host kernel/scheduler/systemd state, so it is GATED behind
# --tune: without --tune it ONLY detects and reports current state (no mutation).
#
# Design contract:
#   * idempotent: re-running --tune is a no-op for already-applied steps.
#   * reversible: every applied mutation writes its ORIGINAL value into the state
#     file ($STATE_FILE); host-restore.sh replays that file to undo everything.
#   * observable: every action (applied, skipped, detected) is emitted as ONE
#     JSON line on stdout, for the operator and for the run ledger. The runner
#     keeps those lines under the `host_setup` phase in ledger.json. They say
#     what this script DID; what the box then READS BACK is the runner's
#     `host_readback` phase (bench/ledgerbench/hostread.py), which runs
#     immediately after this script on every run start and every resume and
#     fails the run on a Tier-1 mismatch. Anything gated there has its setter
#     HERE -- adding a gate for a knob this script does not write makes every
#     run abort with no operator remedy.
#   * --isolate only CHECKS the kernel cmdline (isolcpus/nohz_full need a reboot);
#     it never edits the bootloader live.
#
# Tiering (the tier emitted per action here; the tier that GATES a run is the
# read-back's, and the two are not the same set -- see hostread.py):
#   Tier-1 (this hardware, exact): governor, boost-off, C-states, THP, IRQ
#     steering, systemd CPUAffinity drop-in, pgdata chattr +C / noatime, service
#     stops.
#   Tier-2 (directional): anything that detect-and-skips if the knob is absent on
#     a different box.
#   Several Tier-1 steps here are DETECT-ONLY or do not take effect retroactively
#     (per-IRQ affinity, noatime, the drop-in for already-running units), so the
#     read-back records them rather than gating on them.
#
# Exit non-zero on a real failure (so the runner's `make bench-*` precondition
# gate refuses to record headline numbers). Detection-only mode never fails.

set -euo pipefail

# --------------------------------------------------------------------------
# Configuration (env-overridable; defaults match the resource ledger). The CCD
# core ids mirror compose.bench.yaml and are finalized against the real
# topology on the box. SUT cores are KEPT CLEAN: housekeeping is steered to the
# CCD1 housekeeping core.
#
# EVERY knob below is env-overridable, including SYSTEMD_DROPIN, and the five
# non-STATE_FILE ones are mirrored in bench/ledgerbench/hostread.py's
# HOST_SETUP_ENV_DEFAULTS. The run forwards them explicitly through `sudo -n
# env ...` (sudo resets the environment) and a test compares the defaults on
# both sides, so the value this script writes and the value the read-back
# expects cannot diverge. A hardcoded path here would be worse than a knob: it
# would let SYSTEMD_DROPIN turn the read-back's Tier-1 drop-in check into a
# read of a file nothing writes.
# --------------------------------------------------------------------------
STATE_FILE="${LEDGER_HOST_STATE:-/var/tmp/ledgerline-host-state.json}"
SUT_CPUS="${SUT_CPUS:-0-5}"                  # SUT cores, kept clean of housekeeping
HOUSEKEEPING_CPUS="${HOUSEKEEPING_CPUS:-15}" # systemd CPUAffinity target (CCD1)
PGDATA_PATH="${PGDATA_PATH:-/var/lib/docker/volumes/ledgerline_pgdata/_data}"
STOP_SERVICES="${STOP_SERVICES:-tuned power-profiles-daemon irqbalance}"
SYSTEMD_DROPIN="${SYSTEMD_DROPIN:-/etc/systemd/system.conf.d/99-ledgerline-affinity.conf}"

TUNE=0
ISOLATE=0

# --------------------------------------------------------------------------
# JSON line emitter. Field order is irrelevant; names/values are the contract.
# action: the step id. status: applied|skipped|detected|noop|error.
# tier: "1"|"2". Optional from/to capture the reversible original/new value.
# --------------------------------------------------------------------------
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
emit() {
  # emit <action> <status> <tier> [key=value ...]
  local action="$1" status="$2" tier="$3"; shift 3
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"
  local extra=""
  local kv
  for kv in "$@"; do
    local k="${kv%%=*}" v="${kv#*=}"
    extra="${extra},\"$(json_escape "$k")\":\"$(json_escape "$v")\""
  done
  printf '{"ts":"%s","component":"host-setup","action":"%s","status":"%s","tier":"%s"%s}\n' \
    "$ts" "$(json_escape "$action")" "$(json_escape "$status")" "$(json_escape "$tier")" "$extra"
}

# --------------------------------------------------------------------------
# State file: a JSON-lines ledger of reversible operations. host-restore.sh
# reads it back. We append one record per applied mutation BEFORE mutating, so a
# crash mid-step is still recoverable. Detection-only mode never writes here.
# --------------------------------------------------------------------------
state_init() {
  [ "$TUNE" -eq 1 ] || return 0
  if [ ! -f "$STATE_FILE" ]; then
    : > "$STATE_FILE"
    emit state-file create 1 "path=$STATE_FILE"
  else
    emit state-file exists 1 "path=$STATE_FILE"
  fi
}
state_record() {
  # state_record <op> <target> <original> : appends a reversal hint.
  [ "$TUNE" -eq 1 ] || return 0
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"
  printf '{"ts":"%s","op":"%s","target":"%s","original":"%s"}\n' \
    "$ts" "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" >> "$STATE_FILE"
}

usage() {
  cat <<'EOF'
Usage: host-setup.sh [--tune] [--isolate]
  (no flags)  detect-and-report ONLY; emits current state as JSON lines, mutates nothing.
  --tune      apply the Tier-1/Tier-2 host tuning (reversible via host-restore.sh).
  --isolate   additionally CHECK (never apply) the isolcpus/nohz_full kernel cmdline.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tune)    TUNE=1 ;;
    --isolate) ISOLATE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------
# Guard: refuse to mutate anywhere but Linux, and require root for --tune.
# --------------------------------------------------------------------------
if [ "$(uname -s)" != "Linux" ]; then
  emit platform-check skipped 1 "uname=$(uname -s)" "reason=linux-only"
  echo "host-setup.sh: not Linux; detection/tuning skipped." >&2
  exit 0
fi
if [ "$TUNE" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  emit root-check error 1 "reason=tune-requires-root"
  echo "host-setup.sh --tune must run as root." >&2
  exit 1
fi

state_init

# --------------------------------------------------------------------------
# Tier-1: CPU governor -> performance (per online CPU).
# --------------------------------------------------------------------------
tune_governor() {
  local any=0 g f cur
  for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -e "$f" ] || continue
    any=1
    cur="$(cat "$f")"
    if [ "$cur" = "performance" ]; then
      emit governor noop 1 "cpu=$f" "value=performance"
      continue
    fi
    if [ "$TUNE" -eq 1 ]; then
      state_record write-file "$f" "$cur"
      echo performance > "$f" || { emit governor error 1 "cpu=$f"; return 1; }
      emit governor applied 1 "cpu=$f" "from=$cur" "to=performance"
    else
      emit governor detected 1 "cpu=$f" "value=$cur"
    fi
  done
  [ "$any" -eq 1 ] || emit governor skipped 1 "reason=no-cpufreq-governor"
}

# --------------------------------------------------------------------------
# Tier-1: turbo boost OFF on primary cores. Two mechanisms: legacy
# cpufreq/boost and amd_pstate's per-policy boost. NOTE: this takes the first
# branch and returns, so on a box exposing BOTH knobs the per-policy ones are
# never written. That is why hostread.read_boost reads every knob that answers
# instead of the one this wrote -- a non-propagating write is invisible
# otherwise. (No turbostat snapshot is taken by anything: scaling_cur_freq plus
# thermal_throttle/core_throttle_count are what the read-back records.)
# --------------------------------------------------------------------------
tune_boost() {
  local f="/sys/devices/system/cpu/cpufreq/boost" cur
  if [ -e "$f" ]; then
    cur="$(cat "$f")"
    if [ "$cur" = "0" ]; then
      emit boost noop 1 "path=$f" "value=0"
    elif [ "$TUNE" -eq 1 ]; then
      state_record write-file "$f" "$cur"
      echo 0 > "$f"
      emit boost applied 1 "path=$f" "from=$cur" "to=0"
    else
      emit boost detected 1 "path=$f" "value=$cur"
    fi
  else
    # amd_pstate exposes boost per scaling policy as cpufreq/boost per-cpu.
    local p any=0 pc
    for p in /sys/devices/system/cpu/cpufreq/policy*/boost; do
      [ -e "$p" ] || continue
      any=1
      pc="$(cat "$p")"
      if [ "$pc" = "0" ]; then
        emit boost noop 1 "path=$p" "value=0"
      elif [ "$TUNE" -eq 1 ]; then
        state_record write-file "$p" "$pc"
        echo 0 > "$p"
        emit boost applied 1 "path=$p" "from=$pc" "to=0"
      else
        emit boost detected 1 "path=$p" "value=$pc"
      fi
    done
    [ "$any" -eq 1 ] || emit boost skipped 2 "reason=no-boost-knob"
  fi
}

# --------------------------------------------------------------------------
# Tier-1: shallow C-states. Prefer cpupower idle-set to disable deep states
# (keeps wakeup latency low/deterministic). Detect-and-skip if cpupower absent.
# --------------------------------------------------------------------------
tune_cstates() {
  if command -v cpupower >/dev/null 2>&1; then
    if [ "$TUNE" -eq 1 ]; then
      # Disable all but C0/C1 (state index >= 2). Reversal = re-enable on restore.
      state_record cpupower-idle "all" "enabled"
      cpupower idle-set -D 2 >/dev/null 2>&1 || true
      emit cstates applied 1 "method=cpupower-idle-set" "disable_below_latency_us=2"
    else
      emit cstates detected 1 "method=cpupower-available"
    fi
  else
    emit cstates skipped 2 "reason=cpupower-absent"
  fi
}

# --------------------------------------------------------------------------
# Tier-1: Transparent Huge Pages -> madvise (avoids khugepaged jitter under
# defrag=always).
# --------------------------------------------------------------------------
tune_thp() {
  local f="/sys/kernel/mm/transparent_hugepage/enabled" cur
  if [ ! -e "$f" ]; then emit thp skipped 2 "reason=no-thp-sysfs"; return 0; fi
  # The active value is the token inside [brackets].
  cur="$(sed -n 's/.*\[\(.*\)\].*/\1/p' "$f")"
  if [ "$cur" = "madvise" ]; then
    emit thp noop 1 "path=$f" "value=madvise"
  elif [ "$TUNE" -eq 1 ]; then
    state_record write-file "$f" "$cur"
    echo madvise > "$f"
    emit thp applied 1 "path=$f" "from=$cur" "to=madvise"
  else
    emit thp detected 1 "path=$f" "value=$cur"
  fi
}

# --------------------------------------------------------------------------
# Tier-1: steer IRQ affinity OFF the SUT cores. Conservative: we record the
# current default_smp_affinity mask and push IRQs onto the housekeeping core.
# irqbalance is stopped separately (tune_services) so it can't re-spread.
# --------------------------------------------------------------------------
tune_irq() {
  local f="/proc/irq/default_smp_affinity" cur mask
  if [ ! -e "$f" ]; then emit irq skipped 2 "reason=no-default-smp-affinity"; return 0; fi
  cur="$(cat "$f")"
  # Build a hex mask for the single housekeeping core (simple single-core case).
  # Only the documented 1-core CCD1 housekeeping default is handled here; any
  # other value is skipped below.
  if printf '%s' "$HOUSEKEEPING_CPUS" | grep -qE '^[0-9]+$'; then
    mask="$(printf '%x' $(( 1 << HOUSEKEEPING_CPUS )))"
    if [ "$TUNE" -eq 1 ]; then
      state_record write-file "$f" "$cur"
      printf '%s' "$mask" > "$f" 2>/dev/null || true
      emit irq applied 1 "path=$f" "from=$cur" "to=$mask" "housekeeping_cpu=$HOUSEKEEPING_CPUS"
    else
      emit irq detected 1 "path=$f" "value=$cur"
    fi
  else
    emit irq skipped 2 "reason=non-single-core-housekeeping" "value=$HOUSEKEEPING_CPUS"
  fi
}

# --------------------------------------------------------------------------
# Tier-1 (the key host-hygiene fix): global systemd CPUAffinity drop-in.
# Container cpusets alone do NOT keep host daemons (dockerd, containerd-shims, journald,
# systemd itself) off the SUT cores. A system-wide CPUAffinity pins every
# host-managed process to the housekeeping core. Reversal removes the drop-in.
# Requires `systemctl daemon-reexec` to take effect; we do that under --tune.
# --------------------------------------------------------------------------
tune_systemd_affinity() {
  mkdir -p "$(dirname "$SYSTEMD_DROPIN")"
  local want="[Manager]
CPUAffinity=${HOUSEKEEPING_CPUS}
"
  if [ -f "$SYSTEMD_DROPIN" ] && [ "$(cat "$SYSTEMD_DROPIN")" = "$want" ]; then
    emit systemd-affinity noop 1 "path=$SYSTEMD_DROPIN" "cpus=$HOUSEKEEPING_CPUS"
    return 0
  fi
  if [ "$TUNE" -eq 1 ]; then
    # Record existence so restore can delete (no prior file) or restore content.
    if [ -f "$SYSTEMD_DROPIN" ]; then
      # base64, because this is the ONE record whose original is a whole FILE:
      # raw multi-line content inside a JSON-lines value splits the record
      # across lines, and host-restore's line-oriented parser then truncated
      # the restored file to the first fragment. Every other record's original
      # is a one-line sysfs value and stays plain.
      state_record restore-file-b64 "$SYSTEMD_DROPIN" "$(base64 -w0 "$SYSTEMD_DROPIN")"
    else
      state_record delete-file "$SYSTEMD_DROPIN" "__absent__"
    fi
    printf '%s' "$want" > "$SYSTEMD_DROPIN"
    systemctl daemon-reexec >/dev/null 2>&1 || true
    emit systemd-affinity applied 1 "path=$SYSTEMD_DROPIN" "cpus=$HOUSEKEEPING_CPUS"
  else
    emit systemd-affinity detected 1 "path=$SYSTEMD_DROPIN" \
      "present=$([ -f "$SYSTEMD_DROPIN" ] && echo yes || echo no)"
  fi
}

# --------------------------------------------------------------------------
# Tier-1: PGDATA on btrfs -> chattr +C (nodatacow) + noatime. Project decision;
# see spec/postgres/README.md. chattr +C only takes on a (near-)empty dir, so
# it is best applied before the DB volume fills; we apply what we can and
# record it. noatime is a remount concern; we detect-and-report the mount
# option rather than forcibly remounting a live docker volume.
# --------------------------------------------------------------------------
tune_pgdata() {
  if [ ! -d "$PGDATA_PATH" ]; then
    emit pgdata skipped 2 "reason=path-absent" "path=$PGDATA_PATH"
    return 0
  fi
  # chattr +C (only meaningful on btrfs/xfs; harmless detect elsewhere).
  if command -v chattr >/dev/null 2>&1 && command -v lsattr >/dev/null 2>&1; then
    local attrs; attrs="$(lsattr -d "$PGDATA_PATH" 2>/dev/null | awk '{print $1}')"
    if printf '%s' "$attrs" | grep -q 'C'; then
      emit pgdata noop 1 "path=$PGDATA_PATH" "nodatacow=already"
    elif [ "$TUNE" -eq 1 ]; then
      state_record chattr "$PGDATA_PATH" "+C-not-set"
      chattr +C "$PGDATA_PATH" 2>/dev/null \
        && emit pgdata applied 1 "path=$PGDATA_PATH" "attr=+C" \
        || emit pgdata skipped 2 "path=$PGDATA_PATH" "reason=chattr-failed-nonbtrfs"
    else
      emit pgdata detected 1 "path=$PGDATA_PATH" "attrs=$attrs"
    fi
  else
    emit pgdata skipped 2 "reason=chattr-absent"
  fi
  # noatime: report the current mount option for the fs holding PGDATA.
  local opts; opts="$(findmnt -no OPTIONS --target "$PGDATA_PATH" 2>/dev/null || echo unknown)"
  if printf '%s' "$opts" | grep -qw noatime; then
    emit pgdata-noatime noop 1 "path=$PGDATA_PATH"
  else
    # Live-remounting a docker volume's backing fs is intrusive; we report and
    # leave the actual remount to a documented manual/fstab step.
    emit pgdata-noatime detected 2 "path=$PGDATA_PATH" "options=$opts" \
      "note=set-noatime-in-fstab-or-mount-opts"
  fi
}

# --------------------------------------------------------------------------
# Tier-1: stop conflicting services (tuned, power-profiles-daemon, irqbalance).
# Each stop is recorded with its prior active-state so restore can re-start it.
# --------------------------------------------------------------------------
tune_services() {
  local svc active
  for svc in $STOP_SERVICES; do
    if ! systemctl list-unit-files "${svc}.service" >/dev/null 2>&1; then
      emit service skipped 2 "service=$svc" "reason=unit-absent"
      continue
    fi
    active="$(systemctl is-active "${svc}.service" 2>/dev/null || echo unknown)"
    if [ "$active" != "active" ]; then
      emit service noop 1 "service=$svc" "state=$active"
      continue
    fi
    if [ "$TUNE" -eq 1 ]; then
      state_record service-restart "$svc" "active"
      systemctl stop "${svc}.service" >/dev/null 2>&1 || true
      emit service applied 1 "service=$svc" "from=active" "to=stopped"
    else
      emit service detected 1 "service=$svc" "state=$active"
    fi
  done
}

# --------------------------------------------------------------------------
# --isolate: CHECK ONLY. isolcpus/nohz_full need a reboot to take effect, so we
# never edit the bootloader live; we verify the running kernel cmdline already
# carries them for the SUT cores. doctor consumes this signal.
# --------------------------------------------------------------------------
check_isolate() {
  [ "$ISOLATE" -eq 1 ] || return 0
  local cmdline; cmdline="$(cat /proc/cmdline 2>/dev/null || echo "")"
  local iso="absent" noh="absent"
  printf '%s' "$cmdline" | grep -qw "isolcpus=${SUT_CPUS}" && iso="present"
  printf '%s' "$cmdline" | grep -q  "nohz_full=${SUT_CPUS}" && noh="present"
  emit isolate checked 1 "isolcpus=$iso" "nohz_full=$noh" "expected_cpus=$SUT_CPUS"
}

# --------------------------------------------------------------------------
# Run all steps. Detection mode runs the same functions; they branch on $TUNE.
#
# The conflicting services stop first: tuned restores its saved CPU state on
# `systemctl stop`, so stopping it after tune_governor would revert the
# governor.
# --------------------------------------------------------------------------
emit run begin 1 "mode=$([ "$TUNE" -eq 1 ] && echo tune || echo detect)" "isolate=$ISOLATE"
tune_services
tune_governor
tune_boost
tune_cstates
tune_thp
tune_irq
tune_systemd_affinity
tune_pgdata
check_isolate
emit run end 1 "mode=$([ "$TUNE" -eq 1 ] && echo tune || echo detect)"
