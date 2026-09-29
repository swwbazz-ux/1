#!/bin/bash
# Navigate and measure time until the excavator work screen is usable.
# usage: ready.sh <cdpPort> <url> [maxSeconds]
D="$(cd "$(dirname "$0")" && pwd)"
T0=$(date +%s%N); node "$D/cdpx.js" "$1" any nav "$2" >/dev/null
last=""; end=$(( $(date +%s) + ${3:-30} ))
while [ $(date +%s) -lt $end ]; do
  R=$(timeout 4 node "$D/cdpx.js" "$1" any eval "(()=>{const b=document.body;const s=document.querySelector('[data-eo-shift-state]');if(!b||!s)return 'nobody';const btn=document.querySelector('[data-eo-shift-button]');return document.readyState+' lock='+b.dataset.appContractLocked+'/'+b.dataset.appContractLockReason+' shift='+s.dataset.eoShiftState+' btn='+(btn&&!btn.disabled?'on':'off')+' trucks='+document.querySelectorAll('[data-eo-truck-card]').length})()" 2>&1)
  if [ "$R" != "$last" ]; then echo "t+$(( ($(date +%s%N)-T0)/1000000 ))ms $R"; last="$R"; fi
  case "$R" in *lock=false*btn=on*) break;; esac
  sleep 0.25
done
