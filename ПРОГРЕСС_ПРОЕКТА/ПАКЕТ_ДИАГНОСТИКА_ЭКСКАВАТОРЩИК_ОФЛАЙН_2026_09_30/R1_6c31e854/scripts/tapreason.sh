#!/bin/bash
# tapreason.sh <cdpPort> <urlPart> <reasonRegex>  — real tap on a downtime reason (switches to the downtime tab first)
D="$(cd "$(dirname "$0")" && pwd)"
P=$(node "$D/cdpx.js" "$1" "$2" eval "(()=>{document.querySelector('[data-eo-tab=events]').click();const b=[...document.querySelectorAll('[data-eo-downtime-reason-id]')].find(x=>new RegExp('$3').test(x.textContent));const r=b.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
sleep 0.5; node "$D/cdpx.js" "$1" "$2" tap $P >/dev/null; date -u "+tap $3 %H:%M:%S.%3N UTC"
