#!/usr/bin/env bash
set -euo pipefail
cfg=/etc/nginx/sites-available/vasiliy-test.dj-bazz.ru.conf
cp -a "$cfg" "$cfg.before-public-$(date +%Y%m%d-%H%M%S)"
sed -i '/^[[:space:]]*auth_basic[[:space:]]/d; /^[[:space:]]*auth_basic_user_file[[:space:]]/d' "$cfg"
nginx -t
systemctl reload nginx
curl -fsS -H 'Host: vasiliy-test.185.149.144.169.sslip.io' http://127.0.0.1/ >/dev/null
echo PUBLIC_OK
