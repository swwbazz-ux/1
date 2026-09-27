#!/usr/bin/env bash
set -Eeuo pipefail

EVIDENCE_ROOT="${1:?usage: seal_evidence.sh EVIDENCE_ROOT}"
EVIDENCE_ROOT="$(realpath "$EVIDENCE_ROOT")"
test -d "$EVIDENCE_ROOT"

manifest="$EVIDENCE_ROOT/SHA256SUMS"
manifest_tmp="$EVIDENCE_ROOT/.SHA256SUMS.tmp"
rm -f "$manifest" "$manifest_tmp"

if find "$EVIDENCE_ROOT" -type f -iname '*secret*' -print -quit | grep -q .; then
  echo 'Evidence sanitization failed: forbidden secret-named file' >&2
  exit 1
fi

# Quiet mode is required: a rejected value must never be echoed to stdout or
# stderr.  GitHub masks are a second independent boundary, not a replacement
# for this content gate.
if grep -qRIE --binary-files=without-match \
  '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|postgres_app_password|postgres_maint_password|redis_password|django_secret_key|Authorization:|Cookie:|Set-Cookie:)' \
  "$EVIDENCE_ROOT"; then
  echo 'Evidence secret/PII scan failed' >&2
  exit 1
fi

(
  cd "$EVIDENCE_ROOT"
  find . -type f ! -name SHA256SUMS ! -name .SHA256SUMS.tmp -print0 \
    | sort -z \
    | xargs -0 -r sha256sum > .SHA256SUMS.tmp
  sha256sum -c --status .SHA256SUMS.tmp
  mv .SHA256SUMS.tmp SHA256SUMS
  sha256sum -c --status SHA256SUMS
)

entries="$(wc -l < "$manifest" | tr -d ' ')"
printf 'EVIDENCE_SEAL_OK files=%s\n' "$entries"
