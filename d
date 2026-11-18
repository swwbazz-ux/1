#!/usr/bin/env bash
set -euo pipefail
ts=$(date +%Y%m%d-%H%M%S)
install -d -m 700 /root/moya-backup
tar -czf "/root/moya-backup/vasiliy-test-$ts.tgz" -C /var/www vasiliy-test.dj-bazz.ru
d=/root/moya-transfer; mkdir -p "$d"; cd "$d"; rm -f sitepart-* site.html.gz
for i in 00 01 02 03 04 05 06 07 08; do curl -fsSLO "https://raw.githubusercontent.com/swwbazz-ux/1/main/moya-upakovka-transfer/sitepart-$i"; done
cat sitepart-* > site.html.gz
echo "de182fea9a6e3810015285a15bd0276a3c5789629bcb52f53b00d7432b720958  site.html.gz" | sha256sum -c -
gzip -dc site.html.gz > /var/www/vasiliy-test.dj-bazz.ru/index.html.new
cd /var/www/vasiliy-test.dj-bazz.ru
echo "25a1d7b1a8c231a031a1debfb53173e4102d85155a6fde7139a4846e5a2d4abd  index.html.new" | sha256sum -c -
chown www-data:www-data index.html.new; chmod 644 index.html.new; mv index.html.new index.html
nginx -t; systemctl reload nginx; echo DEPLOY_OK
