import http from "node:http";
import { createHash } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";

function arg(name, fallback = "") {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? String(process.argv[index + 1] || "") : fallback;
}

const listenHost = arg("listen-host", "127.0.0.1");
const listenPort = Number(arg("listen-port", "18460"));
const upstreamHost = arg("upstream-host", "127.0.0.1");
const upstreamPort = Number(arg("upstream-port", "18461"));
const requestLogPath = arg("request-log");
const dropLogPath = arg("drop-log");
const armPath = arg("arm");
const blockedPath = arg("blocked");
const releasePath = arg("release");

function appendJson(path, value) {
  if (!path) return;
  appendFileSync(path, `${JSON.stringify(value)}\n`, "utf8");
}

function readJson(path) {
  if (!path || !existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function removeIfPresent(path) {
  if (path && existsSync(path)) unlinkSync(path);
}

function parseSyncEvents(requestPath, body) {
  if (!requestPath.startsWith("/offline-events/sync/")) return [];
  try {
    const parsed = JSON.parse(body.toString("utf8"));
    return Array.isArray(parsed.events) ? parsed.events : [];
  } catch {
    return [];
  }
}

function eventSummary(event) {
  return {
    event_id: String(event?.event_id || ""),
    event_type: String(event?.event_type || ""),
    occurred_at: String(event?.occurred_at || ""),
    sequence: Number(event?.sequence || 0),
    truck_id: Number(event?.payload?.truck_id || 0),
    local_trip_id: String(event?.payload?.local_trip_id || ""),
    depends_on: Array.isArray(event?.depends_on) ? event.depends_on : [],
  };
}

function matchesControl(event, control) {
  if (!control || event?.event_type !== "excavator.trip.loaded") return false;
  if (control.event_id && String(event.event_id) !== String(control.event_id)) return false;
  if (control.truck_id && Number(event?.payload?.truck_id || 0) !== Number(control.truck_id)) return false;
  return true;
}

const server = http.createServer((clientReq, clientRes) => {
  const chunks = [];
  clientReq.on("data", (chunk) => chunks.push(chunk));
  clientReq.on("end", () => {
    const body = Buffer.concat(chunks);
    const events = parseSyncEvents(clientReq.url || "", body);
    const summaries = events.map(eventSummary);
    const blocked = readJson(blockedPath);
    const blockedEvent = events.find((event) => matchesControl(event, blocked));

    if (blockedEvent && !existsSync(releasePath)) {
      appendJson(requestLogPath, {
        at: new Date().toISOString(),
        method: clientReq.method,
        path: clientReq.url,
        events: summaries,
        disposition: "qa_block_after_committed_drop",
        status: 503,
      });
      const payload = Buffer.from(JSON.stringify({
        ok: false,
        error: "E2-QA1 intentional hold after committed lost response",
      }));
      clientRes.writeHead(503, {
        "content-type": "application/json; charset=utf-8",
        "content-length": payload.length,
        "cache-control": "no-store",
      });
      clientRes.end(payload);
      return;
    }

    if (blockedEvent && existsSync(releasePath)) {
      removeIfPresent(releasePath);
      removeIfPresent(blockedPath);
    }

    const headers = { ...clientReq.headers };
    delete headers["connection"];
    delete headers["proxy-connection"];
    headers["connection"] = "close";

    const upstreamReq = http.request({
      host: upstreamHost,
      port: upstreamPort,
      method: clientReq.method,
      path: clientReq.url,
      headers,
      agent: false,
    });

    upstreamReq.on("response", (upstreamRes) => {
      const arm = readJson(armPath);
      const armedEvent = events.find((event) => matchesControl(event, arm));

      if (armedEvent) {
        const responseChunks = [];
        upstreamRes.on("data", (chunk) => responseChunks.push(chunk));
        upstreamRes.on("end", () => {
          const responseBody = Buffer.concat(responseChunks);
          let responseJson = null;
          try {
            responseJson = JSON.parse(responseBody.toString("utf8"));
          } catch {
            responseJson = null;
          }
          const result = Array.isArray(responseJson?.results)
            ? responseJson.results.find((item) => String(item?.event_id || "") === String(armedEvent.event_id || ""))
            : null;
          const evidence = {
            at: new Date().toISOString(),
            method: clientReq.method,
            path: clientReq.url,
            event: eventSummary(armedEvent),
            upstream_status: upstreamRes.statusCode,
            upstream_response_sha256: createHash("sha256").update(responseBody).digest("hex"),
            result: result ? {
              event_id: result.event_id,
              status: result.status,
              trip_id: result.trip_id,
              event_receipt_id: result.event_receipt_id,
              no_effect: Boolean(result.no_effect),
            } : null,
            downstream: "connection_destroyed_before_headers",
          };
          appendJson(dropLogPath, evidence);
          appendJson(requestLogPath, {
            at: evidence.at,
            method: clientReq.method,
            path: clientReq.url,
            events: summaries,
            disposition: "upstream_complete_downstream_dropped",
            upstream_status: upstreamRes.statusCode,
          });
          removeIfPresent(armPath);
          writeFileSync(blockedPath, JSON.stringify({
            event_id: String(armedEvent.event_id || ""),
            truck_id: Number(armedEvent?.payload?.truck_id || 0),
            committed_at: evidence.at,
          }), "utf8");
          clientRes.destroy();
          clientReq.socket.destroy();
        });
        upstreamRes.on("error", (error) => clientRes.destroy(error));
        return;
      }

      const responseHeaders = { ...upstreamRes.headers };
      delete responseHeaders["connection"];
      delete responseHeaders["keep-alive"];
      responseHeaders["connection"] = "close";
      clientRes.writeHead(upstreamRes.statusCode || 502, responseHeaders);
      upstreamRes.pipe(clientRes);
      appendJson(requestLogPath, {
        at: new Date().toISOString(),
        method: clientReq.method,
        path: clientReq.url,
        events: summaries,
        disposition: "forwarded",
        upstream_status: upstreamRes.statusCode,
      });
    });

    upstreamReq.on("error", (error) => {
      appendJson(requestLogPath, {
        at: new Date().toISOString(),
        method: clientReq.method,
        path: clientReq.url,
        events: summaries,
        disposition: "upstream_error",
        error: String(error?.message || error),
      });
      if (!clientRes.headersSent) {
        const payload = Buffer.from(JSON.stringify({ok: false, error: "isolated backend unavailable"}));
        clientRes.writeHead(502, {
          "content-type": "application/json; charset=utf-8",
          "content-length": payload.length,
          "cache-control": "no-store",
        });
        clientRes.end(payload);
      } else {
        clientRes.destroy(error);
      }
    });

    if (body.length) upstreamReq.write(body);
    upstreamReq.end();
  });
});

server.listen(listenPort, listenHost, () => {
  process.stdout.write(JSON.stringify({
    status: "listening",
    listen: `${listenHost}:${listenPort}`,
    upstream: `${upstreamHost}:${upstreamPort}`,
    pid: process.pid,
  }) + "\n");
});

function shutdown() {
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 3000).unref();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
