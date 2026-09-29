#!/bin/bash
# fbaccept.sh <cdpPort> <urlPart> — open free bucket sheet, type 10, pick tmp-10, accept (real taps)
D="$(cd "$(dirname "$0")" && pwd)"; C="node $D/cdpx.js $1 $2"
P=$($C eval "(()=>{const r=document.querySelector('[data-eo-free-bucket-open]').getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
$C tap $P >/dev/null; sleep 1
$C tap 116 542 >/dev/null; $C tap 116 380 >/dev/null; $C tap 250 542 >/dev/null; sleep 1.2
P=$($C eval "(()=>{const b=document.querySelector('[data-eo-free-bucket-result-id]');if(!b)return 'none';const r=b.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
echo "result row: $P ($($C eval "(document.querySelector('[data-eo-free-bucket-result-id]')||{}).textContent||(document.querySelector('[data-eo-free-bucket-input]').closest('[role=dialog]').innerText.replace(/\s+/g,' ').slice(0,160))"))"
[ "$P" = "none" ] && exit 1
$C tap $P >/dev/null; sleep 0.5
P=$($C eval "(()=>{const r=document.querySelector('[data-eo-free-bucket-accept]').getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
$C tap $P >/dev/null; date -u "+fb accept %H:%M:%S.%3N UTC"
