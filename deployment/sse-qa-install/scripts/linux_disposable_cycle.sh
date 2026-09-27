#!/usr/bin/env bash
set -Eeuo pipefail

# Destructive integration cycle for a NEW disposable Ubuntu 24.04 runner only.
# No production network access, load test, receiver, SSH, DNS or Firebase is used.
PACKAGE_ROOT="${1:?usage: linux_disposable_cycle.sh PACKAGE_ROOT SECRETS_JSON NETWORK_SMOKE_JSON [EVIDENCE_ROOT]}"
SECRETS_JSON="${2:?usage: linux_disposable_cycle.sh PACKAGE_ROOT SECRETS_JSON NETWORK_SMOKE_JSON [EVIDENCE_ROOT]}"
NETWORK_SMOKE_JSON="${3:?usage: linux_disposable_cycle.sh PACKAGE_ROOT SECRETS_JSON NETWORK_SMOKE_JSON [EVIDENCE_ROOT]}"
EVIDENCE_ROOT="${4:-$PWD/sse-qa-linux-evidence}"
PACKAGE_ROOT="$(realpath "$PACKAGE_ROOT")"
SECRETS_JSON="$(realpath "$SECRETS_JSON")"
NETWORK_SMOKE_JSON="$(realpath "$NETWORK_SMOKE_JSON")"
EVIDENCE_ROOT="$(realpath -m "$EVIDENCE_ROOT")"
CTL="$PACKAGE_ROOT/scripts/sse_qa_ctl.py"
ZERO_SCAN="$PACKAGE_ROOT/scripts/linux_zero_residue_scan.sh"
NETWORK_SMOKE="$PACKAGE_ROOT/scripts/linux_network_login_smoke.py"
REDIS_METRICS="$PACKAGE_ROOT/scripts/linux_redis_metrics.py"
JOURNAL_HELPER="$PACKAGE_ROOT/scripts/linux_install_journal.py"
RUNTIME_SLICE=/run/systemd/system/sse-qa.slice
PERSISTENT_SLICE=/etc/systemd/system/sse-qa.slice
QA_CGROUP=/sse.slice/sse-qa.slice
QA_CGROUP_FS=/sys/fs/cgroup/sse.slice/sse-qa.slice
INSTALL_UNIT=sse-qa-install.service
EXIT_DIR=/run/sse-qa-cycle
TLS_DIR=/etc/letsencrypt/live/sse-qa.driverform.ru

test "$(id -u)" = 0
test -f /run/sse-qa-disposable-test
test -f "$CTL"
test -f "$ZERO_SCAN"
test -f "$JOURNAL_HELPER"
test -f "$SECRETS_JSON"
test "$NETWORK_SMOKE_JSON" = /run/sse-qa-network-smoke.json
test -f "$NETWORK_SMOKE_JSON"
test "$(stat -c '%u:%a' "$NETWORK_SMOKE_JSON")" = 0:600
test -f "$PACKAGE_ROOT/generated/wheelhouse.sha256"
test ! -e /srv/accounting-mvp
test ! -e /etc/accounting-mvp.env
test ! -e /var/lib/sse-qa
test ! -e /srv/sse-qa
test ! -e "$TLS_DIR"
mkdir -p "$EVIDENCE_ROOT"/{raw,cgroup,metadata,zero-residue}
chmod 0700 "$EVIDENCE_ROOT"
umask 077

exec > >(tee -a "$EVIDENCE_ROOT/raw/cycle.stdout.log") \
  2> >(tee -a "$EVIDENCE_ROOT/raw/cycle.stderr.log" >&2)

printf 'utc_start=%s\n' "$(date -u +%FT%TZ)" | tee "$EVIDENCE_ROOT/metadata/run.txt"
printf 'package_root=%s\nproduction_access=0\nload_clients=0\n' "$PACKAGE_ROOT" \
  >>"$EVIDENCE_ROOT/metadata/run.txt"

run_logged() {
  local label="${1:?label required}"
  shift
  local started status ended
  started="$(date -u +%FT%TZ)"
  printf 'PHASE_START label=%s utc=%s command=' "$label" "$started" \
    | tee -a "$EVIDENCE_ROOT/metadata/phases.log"
  printf '%q ' "$@" | tee -a "$EVIDENCE_ROOT/metadata/phases.log"
  printf '\n' | tee -a "$EVIDENCE_ROOT/metadata/phases.log"
  set +e
  "$@" > >(tee "$EVIDENCE_ROOT/raw/$label.stdout.log") \
       2> >(tee "$EVIDENCE_ROOT/raw/$label.stderr.log" >&2)
  status=$?
  set -e
  ended="$(date -u +%FT%TZ)"
  printf 'PHASE_END label=%s utc=%s exit=%s\n' "$label" "$ended" "$status" \
    | tee -a "$EVIDENCE_ROOT/metadata/phases.log"
  return "$status"
}

assert_production_names_loopback() {
  local host address resolved
  for host in driverform.ru www.driverform.ru sse-qa.driverform.ru; do
    resolved="$(getent ahosts "$host")"
    test -n "$resolved"
    while read -r address; do
      case "$address" in
        127.*|::1) ;;
        *) echo "non-loopback address forbidden for $host: $address" >&2; return 1 ;;
      esac
    done < <(printf '%s\n' "$resolved" | awk '{print $1}' | sort -u)
  done
  echo 'PRODUCTION_NETWORK_GUARD_OK hosts=3 loopback_only=true'
}

capture_environment() {
  {
    date -u +%FT%TZ
    uname -a
    cat /etc/os-release
    systemd --version
    stat -fc 'cgroup_fs=%T' /sys/fs/cgroup
    mount | grep ' on /sys/fs/cgroup '
    /usr/bin/python3.12 --version
    psql --version
    redis-server --version
    nginx -v
    sha256sum "$PACKAGE_ROOT/generated/runtime.tar.gz" "$PACKAGE_ROOT/generated/wheelhouse.sha256"
  } >"$EVIDENCE_ROOT/metadata/environment.log" 2>&1
}

stage_install_slice() {
  test ! -e "$RUNTIME_SLICE"
  test ! -e "$PERSISTENT_SLICE"
  install -D -o root -g root -m 0644 \
    "$PACKAGE_ROOT/config/systemd/sse-qa.slice" "$RUNTIME_SLICE"
  systemctl daemon-reload
  systemctl start sse-qa.slice
}

cleanup_install_slice() {
  local install_status="${1:?install status required}"
  if test "$install_status" -ne 0 || test ! -f "$PERSISTENT_SLICE"; then
    systemctl stop sse-qa.slice || true
  fi
  rm -f "$RUNTIME_SLICE"
  systemctl daemon-reload
}

assert_slice_limits() {
  test "$(systemctl show sse-qa.slice -p CPUQuotaPerSecUSec --value)" = 1s
  test "$(systemctl show sse-qa.slice -p CPUQuotaPeriodUSec --value)" = 100ms
  test "$(systemctl show sse-qa.slice -p MemoryHigh --value)" = 1879048192
  test "$(systemctl show sse-qa.slice -p MemoryMax --value)" = 2147483648
  test "$(systemctl show sse-qa.slice -p MemorySwapMax --value)" = 0
  test "$(systemctl show sse-qa.slice -p TasksMax --value)" = 256
  test "$(systemctl show sse-qa.slice -p ControlGroup --value)" = "$QA_CGROUP"
}

assert_slice_child() {
  local unit="${1:?unit required}"
  test "$(systemctl show "$unit" -p Slice --value)" = sse-qa.slice
  test "$(systemctl show "$unit" -p ControlGroup --value)" = "$QA_CGROUP/$unit"
}

ownership_phase() {
  /usr/bin/python3.12 - <<'PY'
import json
from pathlib import Path
p = Path('/var/lib/sse-qa/OWNERSHIP.json')
try:
    print(json.loads(p.read_text(encoding='utf-8')).get('phase', ''))
except (OSError, ValueError):
    print('')
PY
}

wait_install_checkpoint() {
  local expected_phase="${1:?phase required}"
  local deadline=$((SECONDS + 300)) active='' pid='' phase='' proc_cgroup=''
  while ((SECONDS < deadline)); do
    active="$(systemctl show "$INSTALL_UNIT" -p ActiveState --value 2>/dev/null || true)"
    pid="$(systemctl show "$INSTALL_UNIT" -p MainPID --value 2>/dev/null || true)"
    phase="$(ownership_phase)"
    if test "$active" = active && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && test "$phase" = "$expected_phase"; then
      assert_slice_child "$INSTALL_UNIT"
      proc_cgroup="$(awk -F: '$1 == "0" { print $3 }' "/proc/$pid/cgroup")"
      test "$proc_cgroup" = "$QA_CGROUP/$INSTALL_UNIT"
      printf 'INSTALL_CHECKPOINT_OK active=%s main_pid=%s phase=%s cgroup=%s\n' \
        "$active" "$pid" "$phase" "$proc_cgroup"
      return 0
    fi
    sleep 0.25
  done
  echo "install checkpoint timeout: phase=$expected_phase active=$active pid=$pid observed_phase=$phase" >&2
  return 1
}

capture_install_invocation() {
  local label="${1:?label required}"
  local destination="$EXIT_DIR/$label.invocation"
  local deadline=$((SECONDS + 30)) invocation=''
  while ((SECONDS < deadline)); do
    invocation="$(systemctl show "$INSTALL_UNIT" -p InvocationID --value 2>/dev/null || true)"
    if [[ "$invocation" =~ ^[0-9a-fA-F]{32}$ ]]; then
      printf '%s\n' "${invocation,,}" >"$destination"
      return 0
    fi
    sleep 0.05
  done
  echo "install InvocationID unavailable: label=$label" >&2
  return 1
}

export_install_journal() {
  local label="${1:?label required}"
  local output_format="${2:?output format required}"
  local destination="${3:?destination required}"
  local invocation_file="$EXIT_DIR/$label.invocation"
  local invocation
  test -s "$invocation_file"
  invocation="$(cat "$invocation_file")"
  /usr/bin/python3.12 "$JOURNAL_HELPER" \
    --invocation "$invocation" --output "$output_format" >"$destination"
}

capture_cgroup_snapshot() {
  local label="${1:?label required}"
  shift
  local out="$EVIDENCE_ROOT/cgroup/$label"
  mkdir -p "$out"
  systemctl show sse-qa.slice \
    -p Id -p ActiveState -p ControlGroup -p CPUQuotaPerSecUSec -p CPUQuotaPeriodUSec \
    -p MemoryHigh -p MemoryMax -p MemorySwapMax -p TasksMax >"$out/parent.systemctl"
  for file in cpu.stat memory.current memory.events pids.current pids.events io.stat; do
    test -r "$QA_CGROUP_FS/$file"
    cp "$QA_CGROUP_FS/$file" "$out/$file"
  done
  # Never write argv or environment to artifacts.  They may contain tokens.
  ps -eo pid=,ppid=,user=,cgroup=,comm= >"$out/process-tree.log"
  local unit
  for unit in "$@"; do
    assert_slice_child "$unit"
    systemctl show "$unit" -p Id -p ActiveState -p MainPID -p Slice -p ControlGroup \
      >"$out/$unit.systemctl"
  done
  echo "CGROUP_SNAPSHOT_OK label=$label units=$#" | tee "$out/result.txt"
}

assert_lag_summary() {
  local path="${1:?summary path required}"
  local start_ns="${2:?start required}"
  local end_ns="${3:?end required}"
  /usr/bin/python3.12 - "$path" "$start_ns" "$end_ns" <<'PY'
import json
import sys
value = json.loads(open(sys.argv[1], encoding='utf-8').read())
assert value['samples'] >= 3
assert value['workers'] == 1
assert value['window']['start_unix_ns'] == int(sys.argv[2])
assert value['window']['end_unix_ns'] == int(sys.argv[3])
print(f"EVENT_LOOP_WINDOW_OK samples={value['samples']} workers={value['workers']}")
PY
}

start_install_async() {
  local label="${1:?label required}"
  local fault_point="${2:-}"
  local hold_point="${3:-}"
  local exit_file="$EXIT_DIR/$label.exit"
  local env_args=()
  install -d -o root -g root -m 0700 "$EXIT_DIR"
  rm -f "$exit_file"
  if test -n "$fault_point"; then
    env_args+=(--setenv=SSE_QA_FAULT_INJECTION=1 "--setenv=SSE_QA_FAULT_AT=$fault_point")
  fi
  if test -n "$hold_point"; then
    env_args+=(--setenv=SSE_QA_FAULT_INJECTION=1 "--setenv=SSE_QA_CANCEL_HOLD_AT=$hold_point")
  fi
  stage_install_slice
  assert_slice_limits
  run_logged "$label-systemd-run" /usr/bin/systemd-run --system --quiet --no-block \
    --service-type=exec --unit="$INSTALL_UNIT" --slice=sse-qa.slice \
    --property=CPUQuota=100% --property=MemoryMax=2G --property=MemorySwapMax=0 \
    --property=TasksMax=256 --property=IOWeight=10 --property=TimeoutStopSec=90s \
    "${env_args[@]}" /usr/bin/bash -c \
    'set +e; /usr/bin/python3.12 "$1" install --bundle-root "$2" --secrets-file "$3"; rc=$?; printf "%s\n" "$rc" >"$4"; exit "$rc"' \
    _ "$CTL" "$PACKAGE_ROOT" "$SECRETS_JSON" "$exit_file"
  capture_install_invocation "$label"
}

wait_install_exit() {
  local label="${1:?label required}"
  local exit_file="$EXIT_DIR/$label.exit"
  local deadline=$((SECONDS + 1200)) status
  while ((SECONDS < deadline)); do
    if test -f "$exit_file"; then
      status="$(cat "$exit_file")"
      [[ "$status" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]
      printf '%s\n' "$status"
      return 0
    fi
    sleep 0.5
  done
  echo "install exit timeout: $label" >&2
  return 1
}

collect_install_result() {
  local label="${1:?label required}"
  local exit_file="$EXIT_DIR/$label.exit"
  local status active deadline journal_status=0
  status="$(wait_install_exit "$label")"
  deadline=$((SECONDS + 120))
  while ((SECONDS < deadline)); do
    active="$(systemctl show "$INSTALL_UNIT" -p ActiveState --value 2>/dev/null || true)"
    case "$active" in
      inactive|failed|'') break ;;
    esac
    sleep 0.1
  done
  case "$active" in
    inactive|failed|'') ;;
    *) echo "install unit did not finish after exit marker: $active" >&2; return 1 ;;
  esac
  export_install_journal "$label" short-iso-precise \
    "$EVIDENCE_ROOT/raw/$label.journal.log" || journal_status=$?
  printf '%s\n' "$status" >"$EVIDENCE_ROOT/raw/$label.exit"
  cleanup_install_slice "$status"
  systemctl reset-failed "$INSTALL_UNIT" >/dev/null 2>&1 || true
  rm -f "$exit_file" "$EXIT_DIR/$label.invocation" "$EXIT_DIR"/release-*
  rmdir "$EXIT_DIR"
  if test "$journal_status" -ne 0; then
    echo "install journal export failed: label=$label exit=$journal_status" >&2
    return 254
  fi
  return "$status"
}

run_scoped() {
  local unit="${1:?unit required}"
  local operation="${2:?operation required}"
  local label="${3:-$operation}"
  systemctl start sse-qa.slice
  assert_slice_limits
  run_logged "$label" /usr/bin/systemd-run --system --quiet --wait --pipe --collect \
    --slice=sse-qa.slice --service-type=exec --unit="$unit" \
    --property=TimeoutStopSec=90s \
    /usr/bin/python3.12 "$CTL" "$operation" --bundle-root "$PACKAGE_ROOT"
}

create_disposable_tls() {
  mkdir -p "$TLS_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -subj '/CN=sse-qa.driverform.ru' \
    -keyout "$TLS_DIR/privkey.pem" -out "$TLS_DIR/fullchain.pem" >/dev/null 2>&1
}

remove_disposable_tls() {
  rm -f "$TLS_DIR/fullchain.pem" "$TLS_DIR/privkey.pem"
  rmdir "$TLS_DIR" 2>/dev/null || true
}

cleanup_partial() {
  set +e
  systemctl stop "$INSTALL_UNIT" >/dev/null 2>&1
  if test -f /var/lib/sse-qa/OWNERSHIP.json; then
    /usr/bin/python3.12 "$CTL" disable --bundle-root "$PACKAGE_ROOT" >/dev/null 2>&1
    /usr/bin/python3.12 "$CTL" remove --bundle-root "$PACKAGE_ROOT" >/dev/null 2>&1
  fi
  rm -f "$NETWORK_SMOKE_JSON"
  remove_disposable_tls
  systemctl stop sse-qa.slice >/dev/null 2>&1
  rm -f "$RUNTIME_SLICE"
  systemctl daemon-reload >/dev/null 2>&1
  systemctl reset-failed "$INSTALL_UNIT" >/dev/null 2>&1
  rm -f "$EXIT_DIR"/*.exit "$EXIT_DIR"/*.invocation "$EXIT_DIR"/release-*
  rmdir "$EXIT_DIR" >/dev/null 2>&1
  set -e
}

FINALIZED=0
on_exit() {
  local status=$?
  trap - EXIT
  cleanup_partial
  if test "$FINALIZED" -ne 1; then
    /usr/bin/bash "$ZERO_SCAN" "$EVIDENCE_ROOT" emergency-exit || status=1
  fi
  printf 'utc_end=%s\nexit=%s\n' "$(date -u +%FT%TZ)" "$status" >>"$EVIDENCE_ROOT/metadata/run.txt"
  exit "$status"
}
trap on_exit EXIT

capture_environment
run_logged production-network-guard assert_production_names_loopback
run_logged initial-zero-residue /usr/bin/bash "$ZERO_SCAN" "$EVIDENCE_ROOT" initial
run_logged preflight /usr/bin/python3.12 "$CTL" preflight --bundle-root "$PACKAGE_ROOT"

# Normal install: capture the installer, PostgreSQL and Redis concurrently under
# their exact shared parent before allowing the install to finish.
start_install_async normal-install '' dependencies_started
wait_install_checkpoint dependencies_started
capture_cgroup_snapshot normal-install "$INSTALL_UNIT" postgresql@16-sseqa.service redis-sse-qa.service
touch "$EXIT_DIR/release-dependencies_started"
set +e
collect_install_result normal-install
normal_status=$?
set -e
test "$normal_status" -eq 0
grep -Fq 'SSE_QA_INSTALL_OK enabled=false clients=0' "$EVIDENCE_ROOT/raw/normal-install.journal.log"
run_scoped sse-qa-verify.service verify normal-installed-verify
create_disposable_tls
run_scoped sse-qa-enable.service enable normal-enable
run_scoped sse-qa-verify.service verify normal-enabled-verify
LAG_START_NS="$(date +%s%N)"
run_logged normal-network-logins /usr/bin/python3.12 "$NETWORK_SMOKE" "$SECRETS_JSON" "$NETWORK_SMOKE_JSON"
grep -Fq 'SSE_QA_NETWORK_LOGIN_OK logins=2 screens=2 https=1 nginx=1 basic_auth=1' \
  "$EVIDENCE_ROOT/raw/normal-network-logins.stdout.log"
capture_cgroup_snapshot normal-enabled postgresql@16-sseqa.service redis-sse-qa.service \
  sse-qa-reconcile.service sse-qa-wsgi.service sse-qa-asgi.service
run_scoped sse-qa-smoke.service smoke normal-business-smoke
grep -Fq 'SSE_QA_BUSINESS_SMOKE_OK logins=2 screens=2' "$EVIDENCE_ROOT/raw/normal-business-smoke.stdout.log"
grep -Eq 'trip_id=[0-9]+ version=[0-9]+ catchup=1 sse=1' "$EVIDENCE_ROOT/raw/normal-business-smoke.stdout.log"
sleep 4
LAG_END_NS="$(date +%s%N)"
printf 'start_unix_ns=%s\nend_unix_ns=%s\n' "$LAG_START_NS" "$LAG_END_NS" \
  >"$EVIDENCE_ROOT/raw/event-loop-window.log"
cp /srv/sse-qa/metrics/event-loop-lag.jsonl "$EVIDENCE_ROOT/raw/event-loop-lag.jsonl"
run_logged event-loop-summary /usr/bin/python3.12 "$PACKAGE_ROOT/scripts/summarize_event_loop_lag.py" \
  "$EVIDENCE_ROOT/raw/event-loop-lag.jsonl" --start-unix-ns "$LAG_START_NS" --end-unix-ns "$LAG_END_NS"
run_logged event-loop-window-check assert_lag_summary "$EVIDENCE_ROOT/raw/event-loop-summary.stdout.log" \
  "$LAG_START_NS" "$LAG_END_NS"
run_logged postgres-capacity runuser -u postgres -- psql -At -p 55432 -d postgres \
  -c "SHOW max_connections; SELECT count(*) FROM pg_stat_activity WHERE datname='sseqa';"
run_logged redis-capacity /usr/bin/python3.12 "$REDIS_METRICS"
run_logged normal-disable /usr/bin/python3.12 "$CTL" disable --bundle-root "$PACKAGE_ROOT"
run_scoped sse-qa-verify.service verify normal-disabled-verify
run_logged normal-remove /usr/bin/python3.12 "$CTL" remove --bundle-root "$PACKAGE_ROOT"
remove_disposable_tls
run_logged normal-zero-residue /usr/bin/bash "$ZERO_SCAN" "$EVIDENCE_ROOT" normal

# Both fixed install fault points must fail and independently pass cleanup.
for point in after_image_before_marker after_postgres_redis_start; do
  label="fault-$point"
  start_install_async "$label" "$point"
  set +e
  collect_install_result "$label"
  fault_status=$?
  set -e
  test "$fault_status" -ne 0
  grep -Fq "injected failure at $point" "$EVIDENCE_ROOT/raw/$label.journal.log"
  if grep -Fq 'SSE_QA_INSTALL_OK' "$EVIDENCE_ROOT/raw/$label.journal.log"; then
    echo "unexpected install success at fault point $point" >&2
    exit 1
  fi
  printf 'FAULT_MARKER_OK point=%s exit=%s\n' "$point" "$fault_status" \
    | tee "$EVIDENCE_ROOT/raw/$label.marker.log"
  run_logged "fault-$point-zero-residue" /usr/bin/bash "$ZERO_SCAN" "$EVIDENCE_ROOT" "$label"
done

# Cancellation waits for active/MainPID/ownership phase/exact cgroup, not time.
start_install_async cancel '' dependencies_started
wait_install_checkpoint dependencies_started
export_install_journal cancel cat "$EVIDENCE_ROOT/raw/cancel-before-stop.journal.log"
grep -Fq 'SSE_QA_CANCEL_HOLD_READY point=dependencies_started' \
  "$EVIDENCE_ROOT/raw/cancel-before-stop.journal.log"
capture_cgroup_snapshot cancel-before-stop "$INSTALL_UNIT" postgresql@16-sseqa.service redis-sse-qa.service
run_logged cancel-stop systemctl stop "$INSTALL_UNIT"
cancel_state="$(systemctl show "$INSTALL_UNIT" -p ActiveState --value 2>/dev/null || true)"
cancel_pid="$(systemctl show "$INSTALL_UNIT" -p MainPID --value 2>/dev/null || true)"
case "$cancel_state" in inactive|failed|'') ;; *) exit 1 ;; esac
test -z "$cancel_pid" || test "$cancel_pid" = 0
export_install_journal cancel short-iso-precise \
  "$EVIDENCE_ROOT/raw/cancel.journal.log"
grep -Fq 'install cancelled' "$EVIDENCE_ROOT/raw/cancel.journal.log"
if grep -Fq 'SSE_QA_INSTALL_OK' "$EVIDENCE_ROOT/raw/cancel.journal.log"; then
  echo 'cancelled install incorrectly reported success' >&2
  exit 1
fi
cleanup_install_slice 1
systemctl reset-failed "$INSTALL_UNIT" >/dev/null 2>&1 || true
rm -f "$EXIT_DIR"/*.exit "$EXIT_DIR"/*.invocation "$EXIT_DIR"/release-*
rmdir "$EXIT_DIR"
printf 'CANCEL_MARKER_OK active=1 main_pid=1 phase=dependencies_started cgroup=%s success_marker=0\n' \
  "$QA_CGROUP/$INSTALL_UNIT" | tee "$EVIDENCE_ROOT/raw/cancel.marker.log"
run_logged cancel-zero-residue /usr/bin/bash "$ZERO_SCAN" "$EVIDENCE_ROOT" cancel

rm -f "$NETWORK_SMOKE_JSON"
find "$EVIDENCE_ROOT/raw" "$EVIDENCE_ROOT/cgroup" -type f -print0 \
  | sort -z | xargs -0 sha256sum >"$EVIDENCE_ROOT/metadata/evidence.sha256"
printf 'utc_end=%s\nexit=0\n' "$(date -u +%FT%TZ)" >>"$EVIDENCE_ROOT/metadata/run.txt"
FINALIZED=1
trap - EXIT
echo 'SSE_QA_DISPOSABLE_CYCLE_OK normal=1 faults=2 cancel=1 zero_residue=4 production_access=0 load_clients=0'
