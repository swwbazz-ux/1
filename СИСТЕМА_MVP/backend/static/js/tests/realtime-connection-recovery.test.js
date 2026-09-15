"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const test = require("node:test");
const sourcePath = process.env.REALTIME_TEST_SOURCE || path.resolve(__dirname, "../realtime-client.js");
const source = fs.readFileSync(sourcePath, "utf8");
function target() {
    const listeners = new Map();
    return {
        addEventListener(type, listener) {
            listeners.set(type, [...(listeners.get(type) || []), listener]);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).slice().forEach(fn => fn(event));
            return true;
        }
    };
}
function classes() {
    const values = new Set();
    return {
        add: (...names) => names.forEach(name => values.add(name)),
        remove: (...names) => names.forEach(name => values.delete(name)),
        contains: name => values.has(name),
        toggle(name, enabled) { if (enabled) values.add(name); else values.delete(name); }
    };
}
function storage() {
    const values = new Map();
    return {
        getItem: key => values.get(key) || null,
        setItem: (key, value) => values.set(key, String(value)),
        removeItem: key => values.delete(key)
    };
}
function runtime(options = {}) {
    let now = 1_000_000;
    let timerId = 0;
    let requestsAborted = 0;
    const timers = new Map();
    const intervals = new Map();
    const requests = [];
    const events = [];
    const status = {hidden: true, classList: classes()};
    const body = {dataset: {appRoleCode: options.indicator ? "driver" : "excavator_operator"}, classList: classes()};
    const document = Object.assign(target(), {
        body, hidden: false, activeElement: null,
        hasFocus: () => true,
        querySelector(selector) {
            if (selector === "[data-app-realtime-status]") return status;
            if (selector === "[data-connection-indicator]" && options.indicator) return {};
            return null;
        }
    });
    const navigator = {onLine: true};
    const window = Object.assign(target(), {
        document, navigator, AbortController,
        location: {pathname: "/fixture/", origin: "https://fixture.invalid", href: "https://fixture.invalid/fixture/", reload() {throw Error("Unexpected reload");}},
        localStorage: storage(), sessionStorage: storage(),
        AppRealtimeConfig: {
            stateUrl: "/realtime/state/", initialVersion: 7,
            workPollIntervalMs: 2000, pollTimeoutMs: 8000, maxSilentMs: 15000,
            mobileQueueKey: "fixture-outbox", idleDelayMs: 1,
            screens: [{name: "fixture", role: body.dataset.appRoleCode, path: "^/fixture/", mode: "custom", customRefresh: true, foregroundReconcile: true}]
        },
        requestAnimationFrame(callback) { callback(); return 0; },
        setTimeout(callback, delay) { timers.set(++timerId, {callback, delay}); return timerId; },
        clearTimeout(id) { timers.delete(id); },
        setInterval(callback, delay) { intervals.set(++timerId, {callback, delay}); return timerId; },
        clearInterval(id) { intervals.delete(id); },
        fetch(url, init) {
            // No network is used. Requests remain pending until the test settles them.
            return new Promise((resolve, reject) => {
                const request = {url, signal: init.signal, resolve, reject, done: false};
                init.signal.addEventListener("abort", () => {
                    requestsAborted += 1;
                    if (options.deferAbortReject) return;
                    request.done = true;
                    const error = new Error("Fixture planned or timeout abort");
                    error.name = "AbortError";
                    reject(error);
                });
                requests.push(request);
            });
        }
    });
    if (options.refresh) window.applyOperationalStateRefresh = options.refresh;
    window.addEventListener("operational-state-connection", event => events.push({...event.detail}));
    class FakeDate extends Date { static now() {return now;} }
    class CustomEvent { constructor(type, init) {this.type = type; this.detail = init.detail;} }
    vm.runInNewContext(source, {window, document, navigator, AbortController, CustomEvent, Date: FakeDate, URL, console}, {filename: sourcePath});
    document.dispatchEvent({type: "DOMContentLoaded"});
    return {
        window, document, requests, events, status,
        advance(ms) {now += ms;},
        watchdog() {intervals.forEach(timer => timer.callback());},
        runTimers(delay) {
            for (const [id, timer] of [...timers]) {
                if (timer.delay === delay) {timers.delete(id); timer.callback();}
            }
        },
        success(index, payload = {version: 7}) {
            requests[index].done = true;
            requests[index].resolve({status: 200, ok: true, headers: {get: () => "application/json"}, json: () => Promise.resolve(payload)});
        },
        failure(index, name = "TypeError") {
            requests[index].done = true;
            const error = new Error("Fixture failure"); error.name = name;
            requests[index].reject(error);
        },
        snapshot() {
            const state = window.AppRealtime.getDebugState();
            return {
                connectionState: body.dataset.connectionState || null,
                stale: body.classList.contains("is-realtime-stale"),
                bannerHidden: status.hidden,
                bannerVisibleClass: status.classList.contains("is-visible"),
                failures: state.consecutiveFailures,
                pollInFlight: state.pollInFlight,
                pendingVersion: state.pendingVersion,
                applyingUpdate: state.applyingUpdate,
                pageActive: state.pageActive,
                requestCount: requests.length,
                aborted: requestsAborted,
                events: events.map(event => ({connected: event.connected, failures: event.failures, reason: event.reason || null}))
            };
        }
    };
}
async function settle() { for (let i = 0; i < 16; i++) await Promise.resolve(); }
function record() {}


test("one error is weak with reconnecting copy and no stale/lost class", async () => {
 const r=runtime(); r.failure(0); await settle();
 assert.equal(r.snapshot().connectionState,"weak"); assert.equal(r.snapshot().failures,1);
 assert.equal(r.snapshot().stale,false); assert.match(r.status.textContent,/Переподключение/);
});
test("background watchdog never counts silence or corrupts settled state", async () => {
 const r=runtime(); r.success(0); await settle();
 r.window.AppRealtime.poll(); r.document.hidden=true; r.document.dispatchEvent({type:"visibilitychange"}); await settle();
 r.advance(60000); r.watchdog(); await settle();
 assert.equal(r.snapshot().failures,0); assert.equal(r.snapshot().connectionState,"ok");
});
test("watchdog timeout invalidates one owner and counts exactly once even with late abort catch", async () => {
 const r=runtime({deferAbortReject:true}); r.advance(10000); r.watchdog();
 assert.equal(r.snapshot().failures,1); r.failure(0,"AbortError"); await settle();
 assert.equal(r.snapshot().failures,1); assert.equal(r.snapshot().connectionState,"weak");
});
test("wake events in separate turns share healthy poll and one reconciliation", async () => {
 let refreshes=0; const r=runtime({refresh(){refreshes++;return Promise.resolve({applied:true});}});
 r.success(0); await settle();
 for (const type of ["focus","pageshow","resume","native-connectivity-resume"]) {
   r.window.dispatchEvent({type}); r.runTimers(0); await settle();
 }
 assert.equal(r.requests.length,2); assert.equal(r.snapshot().aborted,0);
 r.success(1); await settle(); r.runTimers(0); await settle(); assert.equal(refreshes,1);
});
test("three real failures reach lost; successful transport stays recovering until DOM applied", async () => {
 let apply; const r=runtime({refresh:()=>new Promise(resolve=>{apply=resolve;})});
 for(let i=0;i<3;i++){if(i)r.window.AppRealtime.poll();r.failure(i);await settle();}
 assert.equal(r.snapshot().connectionState,"lost");
 r.window.AppRealtime.poll();r.success(3);await settle();
 assert.equal(r.snapshot().failures,0);assert.equal(r.snapshot().connectionState,"recovering");
 apply({applied:true});await settle(); assert.equal(r.snapshot().connectionState,"ok");
});
test("invalid/missing version response counts failure instead of silently hanging",async()=>{
 const r=runtime();r.success(0,{});await settle();assert.equal(r.snapshot().failures,1);
 assert.equal(r.snapshot().connectionState,"weak");
});
test("missing role handler retains observed version and cannot mark it applied",async()=>{
 const r=runtime();r.advance(100);r.success(0,{version:8});await settle();
 const state=r.window.AppRealtime.getDebugState(); assert.equal(state.appliedVersion,7);
 assert.equal(state.observedVersion,8);assert.equal(state.pendingReconcileVersion,8);
 assert.equal(r.snapshot().connectionState,"recovering");
});
test("new irrelevant delta cannot clear an older failed DOM reconciliation",async()=>{
 const r=runtime({refresh:()=>Promise.reject(Error("fragment abort"))});
 r.advance(100);r.success(0,{version:8,relevant:true});await settle();
 r.window.AppRealtime.poll();r.success(1,{version:9,relevant:false});await settle();
 const state=r.window.AppRealtime.getDebugState();assert.equal(state.appliedVersion,7);
 assert.equal(state.pendingReconcileVersion,9);assert.equal(state.observedVersion,9);
});
test("fresh native success clears lost once but waits for WebView reconciliation",async()=>{
 const r=runtime();for(let i=0;i<3;i++){if(i)r.window.AppRealtime.poll();r.failure(i);await settle();}
 const detail={status:"success",lastSuccessAtMs:1000000,occurredAtMs:1000000,serverVersion:8};
 r.window.dispatchEvent({type:"native-connection-state",detail});await settle();
 assert.equal(r.snapshot().connectionState,"recovering");assert.equal(r.snapshot().failures,0);
 const revision=r.window.AppRealtime.getDebugState().foregroundReconcileRevision;
 r.window.dispatchEvent({type:"native-connection-state",detail});await settle();
 assert.equal(r.window.AppRealtime.getDebugState().foregroundReconcileRevision,revision);
});

test("ordinary first failure then success resets errors without a lost event", async () => {
 const r=runtime({refresh:()=>Promise.resolve({applied:true})});r.success(0);await settle();
 r.window.AppRealtime.poll();r.failure(1);await settle();assert.equal(r.snapshot().connectionState,"weak");
 r.window.AppRealtime.poll();r.success(2);await settle();
 assert.equal(r.snapshot().connectionState,"ok");assert.equal(r.snapshot().failures,0);
 assert.equal(r.events.some(event=>event.state==="lost"),false);
});
test("current timeout and late finally cannot touch a newer request owner",async()=>{
 const r=runtime({deferAbortReject:true});r.advance(8000);r.runTimers(8000);
 assert.equal(r.snapshot().failures,1);r.window.AppRealtime.poll();
 r.failure(0,"AbortError");await settle();
 assert.equal(r.snapshot().pollInFlight,true);assert.equal(r.snapshot().failures,1);
 r.success(1);await settle();assert.equal(r.snapshot().pollInFlight,false);assert.equal(r.snapshot().failures,0);
});
test("fifteen active seconds without either successful channel is lost, not invented failures",async()=>{
 const r=runtime();r.success(0);await settle();r.advance(14999);r.watchdog();
 assert.equal(r.snapshot().connectionState,"ok");r.advance(1);r.watchdog();
 assert.equal(r.snapshot().connectionState,"lost");assert.equal(r.snapshot().failures,0);
});
test("fresh successful native channel prevents false lost from a failing WebView channel",async()=>{
 const r=runtime();r.success(0);await settle();
 r.window.dispatchEvent({type:"native-connection-state",detail:{status:"success",lastSuccessAtMs:1000000,occurredAtMs:1000000}});
 for(let i=1;i<=3;i++){r.window.AppRealtime.poll();r.failure(i);await settle();}
 assert.notEqual(r.snapshot().connectionState,"lost");assert.equal(r.snapshot().connectionState,"recovering");
 r.advance(15001);r.watchdog();assert.equal(r.snapshot().connectionState,"lost");
});
test("stale future duplicate and native failure evidence cannot reset a failed WebView",async()=>{
 const r=runtime();r.failure(0);await settle();
 for(const detail of [
  {status:"success",lastSuccessAtMs:980000,occurredAtMs:980000},
  {status:"success",lastSuccessAtMs:1002000,occurredAtMs:1002000},
  {status:"failure",lastSuccessAtMs:1000000,occurredAtMs:1000000}
 ])r.window.dispatchEvent({type:"native-connection-state",detail});
 assert.equal(r.snapshot().failures,1);assert.equal(r.snapshot().connectionState,"weak");
});
test("offline is weak until silence threshold and online alone is not transport proof",async()=>{
 const r=runtime();r.success(0);await settle();r.window.navigator.onLine=false;
 r.window.dispatchEvent({type:"offline"});r.watchdog();
 assert.equal(r.snapshot().failures,0);assert.equal(r.snapshot().connectionState,"weak");
 r.advance(15000);r.watchdog();assert.equal(r.snapshot().connectionState,"lost");
 r.window.navigator.onLine=true;r.window.dispatchEvent({type:"online"});
 assert.equal(r.snapshot().connectionState,"lost");
});
test("pending outbox keeps recovering after fragment; drain requests final truth",async()=>{
 let refreshes=0;const r=runtime({refresh:()=>{refreshes++;return Promise.resolve({applied:true});}});
 r.window.dispatchEvent({type:"operational-outbox-state",detail:{pendingCount:2}});
 r.success(0,{version:8});await settle();assert.equal(r.snapshot().connectionState,"recovering");
 assert.equal(r.window.AppRealtime.getDebugState().pendingOutboxCount,2);
 r.window.dispatchEvent({type:"operational-outbox-state",detail:{pendingCount:0}});r.runTimers(0);
 r.success(1,{version:8});await settle();assert.equal(refreshes,2);assert.equal(r.snapshot().connectionState,"ok");
});
test("twenty-second server truth refresh repeats even while global version remains unchanged",async()=>{
 let refreshes=0;const r=runtime({refresh:()=>{refreshes++;return Promise.resolve({applied:true});}});
 r.success(0);await settle();
 for(let i=1;i<=20;i++){
  r.advance(2000);r.window.AppRealtime.poll();r.success(i,{version:7,relevant:false});await settle();
  r.watchdog();await settle();
  assert.equal(refreshes,Math.floor(i/10));
 }
 assert.equal(r.snapshot().connectionState,"ok");assert.equal(refreshes,2);
});
test("synchronous DOM throw retains pending version and retries with bounded backoff",async()=>{
 let attempts=0;const r=runtime({refresh:()=>{attempts++;throw Error("DOM init failed");}});
 r.success(0,{version:8});await settle();assert.equal(attempts,1);
 for(const delay of [2000,4000,8000,15000,15000]){
  r.advance(delay);r.window.AppRealtime.poll();
  r.requests.forEach((request,index)=>{if(!request.done)r.success(index,{version:8});});await settle();
  r.runTimers(delay);await settle();
 }
 assert.equal(attempts,6);assert.equal(r.snapshot().failures,0);
 assert.equal(r.window.AppRealtime.getDebugState().appliedVersion,7);
 assert.equal(r.window.AppRealtime.getDebugState().pendingReconcileVersion,8);
 assert.equal(r.snapshot().connectionState,"recovering");
});
test("an older successful fragment cannot erase a newer requested server version",async()=>{
 const resolvers=[];const r=runtime({refresh:()=>new Promise(resolve=>resolvers.push(resolve))});
 r.success(0,{version:8});await settle();
 r.window.AppRealtime.requestReconcile("post_ack",9);
 resolvers[0]({applied:true,version:8});await settle();
 assert.equal(r.window.AppRealtime.getDebugState().appliedVersion,8);
 assert.equal(r.window.AppRealtime.getDebugState().pendingReconcileVersion,9);
 r.runTimers(0);await settle();resolvers[1]({applied:true,version:9});await settle();
 assert.equal(r.window.AppRealtime.getDebugState().appliedVersion,9);
 assert.equal(r.window.AppRealtime.getDebugState().pendingReconcileVersion,null);
});
test("failed role refresh gets exponential backoff while a busy input is only deferred",async()=>{
 let calls=0;const r=runtime({refresh:()=>{calls++;return Promise.resolve({deferred:true,reason:"driver_refresh_failed"});}});
 r.success(0,{version:8});await settle();assert.equal(r.window.AppRealtime.getDebugState().refreshFailures,1);
 r.advance(2000);r.runTimers(2000);await settle();assert.equal(calls,2);
 assert.equal(r.window.AppRealtime.getDebugState().refreshFailures,2);
 r.advance(4000);r.document.activeElement={tagName:"INPUT"};r.runTimers(4000);await settle();
 assert.equal(calls,2);assert.equal(r.window.AppRealtime.getDebugState().refreshFailures,2);
 r.document.activeElement=null;r.advance(1000);r.runTimers(1000);await settle();assert.equal(calls,3);
});
test("planned pause abort produces diagnostic without an unplanned error or lost state",async()=>{
 const r=runtime();const diagnostics=[];r.window.addEventListener("app:connectiondiagnostic",event=>diagnostics.push(event.detail));
 r.document.hidden=true;r.document.dispatchEvent({type:"visibilitychange"});await settle();
 assert.equal(r.snapshot().failures,0);assert.equal(r.events.some(event=>event.state==="lost"),false);
 assert.ok(diagnostics.some(event=>event.cause==="planned_abort"));
 assert.ok(diagnostics.some(event=>event.cause==="background_pause"));
});
test("HTTP 200 login HTML is invalid transport data and never acknowledges state",async()=>{
 const r=runtime();r.requests[0].resolve({status:200,ok:true,headers:{get:()=>"text/html"},json:()=>Promise.resolve({version:8})});await settle();
 assert.equal(r.snapshot().failures,1);assert.equal(r.window.AppRealtime.getDebugState().appliedVersion,7);
});

test("separated lifecycle signals after a fast HTTP and DOM response still coalesce",async()=>{
 let refreshes=0;const r=runtime({refresh:()=>{refreshes++;return Promise.resolve({applied:true});}});
 r.success(0);await settle();r.advance(100);
 r.window.dispatchEvent({type:"resume"});r.runTimers(0);r.success(1);await settle();
 for(const type of ["pageshow","native-connectivity-resume","focus"]){
  r.advance(100);r.window.dispatchEvent({type});r.runTimers(0);await settle();
 }
 assert.equal(r.requests.length,2);assert.equal(refreshes,1);assert.equal(r.snapshot().aborted,0);
});
test("a later reconcile signal reuses a burst's plain focus response",async()=>{
 let refreshes=0;const r=runtime({refresh:()=>{refreshes++;return Promise.resolve({applied:true});}});
 r.success(0);await settle();r.advance(100);
 r.window.dispatchEvent({type:"focus"});r.runTimers(0);r.success(1);await settle();
 assert.equal(refreshes,0);r.advance(100);r.window.dispatchEvent({type:"pageshow"});r.runTimers(0);await settle();
 assert.equal(r.requests.length,2);assert.equal(refreshes,1);assert.equal(r.snapshot().connectionState,"ok");
});

test("visible-only wake gets a fresh active silence budget after a minute in background",async()=>{
 const r=runtime();r.success(0);await settle();r.document.hidden=true;
 r.document.dispatchEvent({type:"visibilitychange"});r.advance(60000);r.watchdog();
 r.document.hidden=false;r.document.dispatchEvent({type:"visibilitychange"});r.runTimers(0);r.watchdog();
 assert.notEqual(r.snapshot().connectionState,"lost");assert.equal(r.snapshot().failures,0);
 r.advance(15000);r.watchdog();assert.equal(r.snapshot().connectionState,"lost");
});

test("native global version prompts a relevance poll instead of an unrelated fragment",async()=>{
 let refreshes=0;const r=runtime({refresh:()=>{refreshes++;return Promise.resolve({applied:true});}});
 r.success(0);await settle();
 r.window.dispatchEvent({type:"native-connection-state",detail:{status:"success",lastSuccessAtMs:1000000,occurredAtMs:1000000,serverVersion:8}});
 r.runTimers(0);r.success(1,{version:8,relevant:false});await settle();
 assert.equal(refreshes,0);assert.equal(r.window.AppRealtime.getDebugState().appliedVersion,8);
 assert.equal(r.snapshot().connectionState,"ok");
});

test("fresh network fragment clears lost even when the realtime channel failed",async()=>{
 let apply;const r=runtime({refresh:()=>new Promise(resolve=>{apply=resolve;})});
 r.success(0,{version:8});await settle();
 for(let i=1;i<=3;i++){r.window.AppRealtime.poll();r.failure(i);await settle();}
 assert.equal(r.snapshot().connectionState,"lost");apply({applied:true,version:8});await settle();
 assert.equal(r.snapshot().connectionState,"recovering");assert.equal(r.snapshot().failures,0);
 assert.equal(r.window.AppRealtime.getDebugState().pendingReconcileVersion,8);
});
test("authenticated WebView heartbeat recovers a PWA without any native bridge",async()=>{
 const r=runtime();for(let i=0;i<3;i++){if(i)r.window.AppRealtime.poll();r.failure(i);await settle();}
 assert.equal(r.snapshot().connectionState,"lost");
 r.window.AppRealtime.reportTransportSuccess({channel:"web_heartbeat",occurredAtMs:1000000,serverVersion:8});await settle();
 assert.equal(r.snapshot().connectionState,"recovering");assert.equal(r.snapshot().failures,0);
 assert.equal(r.window.AppRealtime.getDebugState().nativeLastSuccessAt,0);
 for(let i=3;i<=5;i++){r.window.AppRealtime.poll();r.failure(i);await settle();}
 assert.notEqual(r.snapshot().connectionState,"lost");
});

test("heartbeat event accepts only the authenticated endpoint's fresh 204 contract",async()=>{
 const r=runtime();r.failure(0);await settle();
 r.window.dispatchEvent({type:"web-heartbeat-success",detail:{source:"application-session-heartbeat",status:200,occurredAtMs:1000000}});
 assert.equal(r.snapshot().failures,1);
 r.window.dispatchEvent({type:"web-heartbeat-success",detail:{source:"application-session-heartbeat",status:204,occurredAtMs:1000000}});
 assert.equal(r.snapshot().failures,0);assert.equal(r.snapshot().connectionState,"recovering");
});
