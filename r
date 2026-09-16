#!/bin/sh
set -eu
src='https://raw.githubusercontent.com/swwbazz-ux/1/b0e431707ef0bf67676d39f55ca0fffdaaae6591/deployment/server/accounting_github_deploy_receiver.py'
dst='/usr/local/sbin/accounting-github-deploy-receiver'
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$src" -o "$tmp"
python3 -m py_compile "$tmp"
if [ -f "$dst" ]; then
  cp -a "$dst" "${dst}.before-v2-$(date -u +%Y%m%dT%H%M%SZ)"
fi
install -o root -g root -m 0755 "$tmp" "$dst"
"$dst" --help >/dev/null
sha256sum "$dst"
