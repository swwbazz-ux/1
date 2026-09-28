#!/usr/bin/env bash
set -Eeuo pipefail

# Independent destructive-test cleanup gate.  It only reads the host and
# refuses to run unless the disposable-runner marker is present.
EVIDENCE_ROOT="${1:?usage: linux_zero_residue_scan.sh EVIDENCE_ROOT LABEL}"
LABEL="${2:?usage: linux_zero_residue_scan.sh EVIDENCE_ROOT LABEL}"
test "$(id -u)" = 0
test -f /run/sse-qa-disposable-test
[[ "$LABEL" =~ ^[a-z0-9_-]+$ ]]

OUT="$EVIDENCE_ROOT/zero-residue/$LABEL"
mkdir -p "$OUT"
umask 077
failures=()

record() {
  local name="${1:?name required}"
  shift
  set +e
  "$@" >"$OUT/$name.stdout" 2>"$OUT/$name.stderr"
  local status=$?
  set -e
  printf '%s\n' "$status" >"$OUT/$name.exit"
  return 0
}

record units-loaded systemctl list-units --all --no-legend --plain
record unit-files systemctl list-unit-files --no-legend --plain
record processes ps -eo pid=,ppid=,user=,cgroup=,comm=
record users getent passwd sseqa
record groups getent group sseqa
record clusters pg_lsclusters --no-header
record mounts findmnt -rn -o SOURCE,TARGET,FSTYPE,OPTIONS
record loops losetup --list --noheadings --output NAME,BACK-FILE
record listeners ss -H -ltnp
record cgroups find /sys/fs/cgroup -maxdepth 4 -type d -name '*sse*qa*' -print

for probe in units-loaded unit-files processes clusters mounts loops listeners cgroups; do
  if test "$(cat "$OUT/$probe.exit")" != 0; then
    failures+=("inspection-$probe")
  fi
done
for probe in users groups; do
  status="$(cat "$OUT/$probe.exit")"
  if test "$status" != 0 && test "$status" != 2; then
    failures+=("inspection-$probe")
  fi
done

unit_pattern='^(sse-qa|redis-sse-qa|postgresql@16-sseqa|srv-sse\\x2dqa)'
if grep -Eq "$unit_pattern" "$OUT/units-loaded.stdout"; then
  failures+=(units-loaded)
fi
if grep -Eq "$unit_pattern" "$OUT/unit-files.stdout"; then
  failures+=(unit-files)
fi

if getent passwd sseqa >/dev/null; then failures+=(user); fi
if getent group sseqa >/dev/null; then failures+=(group); fi
if awk '$1 == "16" && $2 == "sseqa" { found=1 } END { exit !found }' "$OUT/clusters.stdout"; then
  failures+=(postgres-cluster)
fi
if grep -Eq '(^|[[:space:]])/srv/sse-qa([[:space:]]|$)' "$OUT/mounts.stdout"; then
  failures+=(mount)
fi
if grep -Eq '/var/lib/sse-qa/sse-qa\.img([[:space:]]|$)' "$OUT/loops.stdout"; then
  failures+=(loop)
fi
if awk '$4 ~ /:(55432|6381|18080|18082)$/ { found=1 } END { exit !found }' "$OUT/listeners.stdout"; then
  failures+=(listener)
fi
if test -s "$OUT/cgroups.stdout"; then failures+=(cgroup); fi

# Do not collect argv or environment: either may contain a runner token or a
# synthetic secret.  QA processes are identified only by account, cgroup and
# fixed cgroup; the executable name is evidence only and is never used alone.
if awk '
  $3 == "sseqa" ||
  $4 ~ /(^|\/)sse\.slice\/sse-qa\.slice(\/|$)/ { found=1 }
  END { exit !found }
' "$OUT/processes.stdout"; then
  failures+=(process)
fi

managed_paths=(
  /var/lib/sse-qa
  /srv/sse-qa
  /etc/sse-qa
  /etc/credstore.encrypted/sse-qa
  /usr/local/libexec/sse-qa-redis-launcher
  /run/sse-qa-nginx
  /etc/nginx/sites-enabled/sse-qa.conf
  /etc/logrotate.d/sse-qa
  /etc/systemd/system/sse-qa.slice
  /run/systemd/system/sse-qa.slice
  '/etc/systemd/system/srv-sse\x2dqa.mount'
  /etc/systemd/system/sse-qa.target
  /etc/systemd/system/redis-sse-qa.service
  /etc/systemd/system/sse-qa-wsgi.service
  /etc/systemd/system/sse-qa-asgi.service
  /etc/systemd/system/sse-qa-reconcile.service
  /etc/systemd/system/postgresql@16-sseqa.service.d
  /etc/postgresql/16/sseqa
  /var/lib/postgresql/16/sseqa
  /run/sse-qa-cycle
)
printf '%s\n' "${managed_paths[@]}" >"$OUT/managed-paths.checked"
for path in "${managed_paths[@]}"; do
  if test -e "$path" || test -L "$path"; then
    printf '%s\n' "$path" >>"$OUT/managed-paths.remaining"
    failures+=(managed-path)
  fi
done

if ((${#failures[@]})); then
  printf 'ZERO_RESIDUE_FAIL scenario=%s findings=%s\n' "$LABEL" "$(IFS=,; echo "${failures[*]}")" \
    | tee "$OUT/result.txt"
  exit 1
fi
printf 'ZERO_RESIDUE_OK scenario=%s units=0 processes=0 users=0 postgres=0 mounts=0 loops=0 files=0 listeners=0 cgroups=0\n' \
  "$LABEL" | tee "$OUT/result.txt"
