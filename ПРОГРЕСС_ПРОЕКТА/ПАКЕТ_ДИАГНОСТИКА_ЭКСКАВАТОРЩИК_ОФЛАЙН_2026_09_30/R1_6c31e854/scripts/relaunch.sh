#!/bin/bash
# Full app restart: close the Chrome process, start a new one with the same profile, set the session cookie.
# usage: relaunch.sh <cdpPort> <profileDir> <sessionKeyFile>
D="$(cd "$(dirname "$0")" && pwd)"
node "$D/browser_close.js" "$1" >/dev/null 2>&1
for i in $(seq 1 20); do curl -s -m 1 "http://127.0.0.1:$1/json/version" >/dev/null || break; sleep 0.3; done
("/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --remote-debugging-port="$1" --user-data-dir="$2" --window-size=392,732 --force-device-scale-factor=1 --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &)
for i in $(seq 1 30); do curl -s -m 1 "http://127.0.0.1:$1/json/version" >/dev/null && break; sleep 0.3; done
node "$D/cdpx.js" "$1" any cookie "$(tr -d '\r\n' < "$3")" >/dev/null
echo "relaunched $1"
