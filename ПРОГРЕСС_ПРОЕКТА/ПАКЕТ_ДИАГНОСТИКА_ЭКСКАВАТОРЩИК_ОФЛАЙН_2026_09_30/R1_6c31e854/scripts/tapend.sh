#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"
P=$(node "$D/cdpx.js" "$1" "$2" eval "(()=>{const r=document.querySelector('[data-eo-close-event]').getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
node "$D/cdpx.js" "$1" "$2" tap $P >/dev/null; date -u "+tap end %H:%M:%S.%3N UTC"
