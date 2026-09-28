#!/usr/bin/env bash

# Shared fail-path diagnostics for the disposable Linux installer cycle.
# The caller must define EVIDENCE_ROOT, EXIT_DIR, INSTALL_UNIT and
# JOURNAL_HELPER before invoking these functions.

SSEQA_DIAG_CURRENT_LABEL=''
SSEQA_DIAG_LAST_PHASE=''
SSEQA_DIAG_CHECKPOINT_STATE='not_started'
SSEQA_DIAG_PRIMARY_KIND=''
SSEQA_DIAG_PRIMARY_EXIT=''
SSEQA_DIAG_POST_STOP_STATUS=0

sseqa_diag_validate_label() {
  [[ "${1:-}" =~ ^[a-z0-9][a-z0-9._-]{0,79}$ ]]
}

sseqa_diag_attempt_dir() {
  local label="${1:?label required}"
  sseqa_diag_validate_label "$label"
  printf '%s/install-attempts/%s\n' "$EVIDENCE_ROOT" "$label"
}

sseqa_diag_begin_attempt() {
  local label="${1:?label required}" directory
  sseqa_diag_validate_label "$label"
  directory="$(sseqa_diag_attempt_dir "$label")"
  mkdir -p "$directory"
  chmod 0700 "$EVIDENCE_ROOT/install-attempts" "$directory"
  printf 'label=%s\nstarted_utc=%s\n' "$label" "$(date -u +%FT%TZ)" \
    >"$directory/attempt.txt"
  SSEQA_DIAG_CURRENT_LABEL="$label"
  SSEQA_DIAG_LAST_PHASE=''
  SSEQA_DIAG_CHECKPOINT_STATE='waiting'
  SSEQA_DIAG_PRIMARY_KIND=''
  SSEQA_DIAG_PRIMARY_EXIT=''
  SSEQA_DIAG_POST_STOP_STATUS=0
}

sseqa_diag_record_invocation() {
  local label="${1:?label required}" invocation="${2:-}" directory
  directory="$(sseqa_diag_attempt_dir "$label")"
  if [[ "$invocation" =~ ^[0-9a-fA-F]{32}$ ]]; then
    printf '%s\n' "${invocation,,}" >"$directory/invocation.txt"
    printf 'available\n' >"$directory/invocation-status.txt"
    return 0
  fi
  printf 'unavailable\n' >"$directory/invocation-status.txt"
  return 1
}

sseqa_diag_record_primary() {
  local label="${1:?label required}" kind="${2:?kind required}"
  local primary_exit="${3:?exit required}" phase="${4:-}" checkpoint="${5:-failed}"
  local directory
  directory="$(sseqa_diag_attempt_dir "$label")"
  [[ "$primary_exit" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]
  [[ "$kind" =~ ^[a-z0-9_]+$ ]]
  printf 'kind=%s\nexit=%s\ncheckpoint=%s\nobserved_phase=%s\nrecorded_utc=%s\n' \
    "$kind" "$primary_exit" "$checkpoint" "$phase" "$(date -u +%FT%TZ)" \
    >"$directory/primary-failure.txt"
  printf '%s\n' "$phase" >"$directory/last-observed-phase.txt"
  SSEQA_DIAG_PRIMARY_KIND="$kind"
  SSEQA_DIAG_PRIMARY_EXIT="$primary_exit"
  SSEQA_DIAG_LAST_PHASE="$phase"
  SSEQA_DIAG_CHECKPOINT_STATE="$checkpoint"
}

sseqa_diag_systemctl_snapshot() {
  local label="${1:?label required}" stage="${2:?stage required}" directory temporary status had_errexit=0
  directory="$(sseqa_diag_attempt_dir "$label")"
  temporary="$directory/.systemctl-$stage.tmp"
  [[ $- == *e* ]] && had_errexit=1
  set +e
  systemctl show "$INSTALL_UNIT" \
    -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus \
    -p MainPID -p InvocationID >"$temporary" 2>/dev/null
  status=$?
  test "$had_errexit" -eq 0 || set -e
  {
    printf 'capture_stage=%s\nsystemctl_exit=%s\n' "$stage" "$status"
    if test "$status" -eq 0; then
      LC_ALL=C awk -F= '
        /^(ActiveState|SubState|Result|ExecMainCode|ExecMainStatus|MainPID|InvocationID)=/ {
          key=$1; sub(/^[^=]*=/, "", $0); value=$0
          if (value ~ /^[A-Za-z0-9_.:\/-]*$/ && length(value) <= 128) {
            print key "=" value
          } else {
            print key "=<invalid>"
          }
        }
      ' "$temporary"
    fi
  } >"$directory/systemd-$stage.txt"
  rm -f "$temporary"
  return 0
}

sseqa_diag_read_invocation() {
  local label="${1:?label required}" evidence_file runtime_file invocation=''
  evidence_file="$(sseqa_diag_attempt_dir "$label")/invocation.txt"
  runtime_file="$EXIT_DIR/$label.invocation"
  if test -s "$evidence_file"; then
    invocation="$(cat "$evidence_file")"
  elif test -s "$runtime_file"; then
    invocation="$(cat "$runtime_file")"
  fi
  if ! [[ "$invocation" =~ ^[0-9a-f]{32}$ ]]; then
    return 1
  fi
  printf '%s\n' "$invocation"
}

sseqa_diag_wrapper_exit() {
  local label="${1:?label required}" exit_file="$EXIT_DIR/$label.exit" value
  if test ! -f "$exit_file"; then
    printf 'missing\n'
    return 1
  fi
  value="$(cat "$exit_file" 2>/dev/null || true)"
  if [[ "$value" =~ ^([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  printf 'invalid\n'
  return 1
}

sseqa_diag_export_journal() {
  local label="${1:?label required}" stage="${2:?stage required}"
  local directory invocation temporary status python_bin had_errexit=0
  directory="$(sseqa_diag_attempt_dir "$label")"
  if ! invocation="$(sseqa_diag_read_invocation "$label")"; then
    printf 'status=unavailable\nexit=not_run\n' >"$directory/journal-$stage.status.txt"
    return 125
  fi
  temporary="$directory/.journal-$stage.tmp"
  python_bin="${SSEQA_DIAG_PYTHON_BIN:-/usr/bin/python3.12}"
  [[ $- == *e* ]] && had_errexit=1
  set +e
  timeout 20s "$python_bin" "$JOURNAL_HELPER" \
    --invocation "$invocation" --output short-iso-precise \
    >"$temporary" 2>/dev/null
  status=$?
  test "$had_errexit" -eq 0 || set -e
  if test "$status" -eq 0; then
    mv "$temporary" "$directory/journal-$stage.log"
    printf 'status=ok\nexit=0\n' >"$directory/journal-$stage.status.txt"
  else
    rm -f "$temporary"
    printf 'status=failed\nexit=%s\n' "$status" >"$directory/journal-$stage.status.txt"
    return "$status"
  fi
  return 0
}

sseqa_diag_capture_attempt() {
  local label="${1:?label required}" stage="${2:?stage required}"
  local fallback_exit="${3:?fallback exit required}" directory wrapper='missing' journal_status=0
  directory="$(sseqa_diag_attempt_dir "$label")"
  printf '%s\n' "$stage" >>"$directory/capture-order.log"
  sseqa_diag_systemctl_snapshot "$label" "$stage"
  wrapper="$(sseqa_diag_wrapper_exit "$label" || true)"
  printf 'wrapper_exit=%s\nfallback_exit=%s\ncheckpoint=%s\nobserved_phase=%s\n' \
    "$wrapper" "$fallback_exit" "$SSEQA_DIAG_CHECKPOINT_STATE" "$SSEQA_DIAG_LAST_PHASE" \
    >"$directory/result-$stage.txt"
  sseqa_diag_export_journal "$label" "$stage" || journal_status=$?
  printf 'diagnostic_exit=%s\n' "$journal_status" >"$directory/diagnostic-$stage.txt"
  return "$journal_status"
}

sseqa_diag_capture_post_stop() {
  local label="${1:?label required}" directory journal_status=0
  directory="$(sseqa_diag_attempt_dir "$label")"
  printf 'post_stop\n' >>"$directory/capture-order.log"
  sseqa_diag_systemctl_snapshot "$label" post-stop
  sseqa_diag_export_journal "$label" post-stop || journal_status=$?
  printf 'diagnostic_exit=%s\n' "$journal_status" >"$directory/diagnostic-post-stop.txt"
  SSEQA_DIAG_POST_STOP_STATUS="$journal_status"
  return "$journal_status"
}

sseqa_diag_record_cleanup() {
  local label="${1:?label required}" status="${2:?status required}" directory
  directory="$(sseqa_diag_attempt_dir "$label")"
  printf 'cleanup\n' >>"$directory/capture-order.log"
  printf 'cleanup_exit=%s\nrecorded_utc=%s\n' "$status" "$(date -u +%FT%TZ)" \
    >"$directory/cleanup-result.txt"
}

sseqa_diag_record_emergency_cleanup() {
  local label="${1:?label required}" status="${2:?status required}" directory
  directory="$(sseqa_diag_attempt_dir "$label")"
  printf 'emergency_cleanup\n' >>"$directory/capture-order.log"
  printf 'emergency_cleanup_exit=%s\nrecorded_utc=%s\n' "$status" "$(date -u +%FT%TZ)" \
    >"$directory/emergency-cleanup-result.txt"
}

sseqa_wait_install_checkpoint() {
  local expected_phase="${1:?phase required}" timeout_seconds="${2:-300}" grace_seconds="${3:-3}"
  local label="${SSEQA_DIAG_CURRENT_LABEL:?diagnostic attempt not started}"
  local deadline=$((SECONDS + timeout_seconds)) grace_deadline=$((SECONDS + grace_seconds))
  local active='' sub='' result='' exec_status='' pid='' phase='' proc_cgroup=''
  local seen_started=0 wrapper='' failure_exit=1
  while ((SECONDS < deadline)); do
    active="$(systemctl show "$INSTALL_UNIT" -p ActiveState --value 2>/dev/null || true)"
    sub="$(systemctl show "$INSTALL_UNIT" -p SubState --value 2>/dev/null || true)"
    result="$(systemctl show "$INSTALL_UNIT" -p Result --value 2>/dev/null || true)"
    exec_status="$(systemctl show "$INSTALL_UNIT" -p ExecMainStatus --value 2>/dev/null || true)"
    pid="$(systemctl show "$INSTALL_UNIT" -p MainPID --value 2>/dev/null || true)"
    phase="$(ownership_phase)"
    SSEQA_DIAG_LAST_PHASE="$phase"
    if test "$active" = active || [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
      seen_started=1
    fi
    if test "$active" = active && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && test "$phase" = "$expected_phase"; then
      assert_slice_child "$INSTALL_UNIT"
      proc_cgroup="$(awk -F: '$1 == "0" { print $3 }' "/proc/$pid/cgroup")"
      test "$proc_cgroup" = "$QA_CGROUP/$INSTALL_UNIT"
      SSEQA_DIAG_CHECKPOINT_STATE='reached'
      printf '%s\n' "$phase" >"$(sseqa_diag_attempt_dir "$label")/last-observed-phase.txt"
      printf 'INSTALL_CHECKPOINT_OK active=%s main_pid=%s phase=%s cgroup=%s\n' \
        "$active" "$pid" "$phase" "$proc_cgroup"
      return 0
    fi
    wrapper="$(sseqa_diag_wrapper_exit "$label" || true)"
    if [[ "$wrapper" =~ ^[0-9]+$ ]]; then
      failure_exit="$wrapper"
      test "$failure_exit" -ne 0 || failure_exit=1
      sseqa_diag_record_primary "$label" exit_marker "$failure_exit" "$phase" failed
      echo "install exited before checkpoint: phase=$expected_phase exit=$wrapper observed_phase=$phase" >&2
      return "$failure_exit"
    fi
    if test "$active" = failed || { test "$active" = inactive && test "$seen_started" -eq 1; }; then
      if [[ "$exec_status" =~ ^[1-9][0-9]*$ ]] && test "$exec_status" -le 255; then
        failure_exit="$exec_status"
      fi
      sseqa_diag_record_primary "$label" terminal_failure "$failure_exit" "$phase" failed
      echo "install terminated before checkpoint: phase=$expected_phase active=$active sub=$sub result=$result exit=$failure_exit observed_phase=$phase" >&2
      return "$failure_exit"
    fi
    # An inactive/not-yet-loaded state during the initial grace interval is
    # expected for asynchronous systemd-run and is not classified as failure.
    if ((SECONDS >= grace_deadline)) && test "$seen_started" -eq 0 && test -z "$active$sub$result$pid"; then
      sseqa_diag_record_primary "$label" status_unavailable 1 "$phase" failed
      echo "install status unavailable after start grace: phase=$expected_phase observed_phase=$phase" >&2
      return 1
    fi
    sleep 0.25
  done
  if test "$active" = active && [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    sseqa_diag_record_primary "$label" live_timeout 124 "$phase" timeout
    echo "install checkpoint live timeout: phase=$expected_phase active=$active pid=$pid observed_phase=$phase" >&2
    return 124
  fi
  sseqa_diag_record_primary "$label" checkpoint_timeout 1 "$phase" timeout
  echo "install checkpoint timeout: phase=$expected_phase active=$active pid=$pid observed_phase=$phase" >&2
  return 1
}
