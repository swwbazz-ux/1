import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = readFileSync(resolve(root, "android/app/src/mining_master_tablet/java/ru/copperresources/mobile/TabletStandaloneReadiness.java"), "utf8");
// Execute the expression shipped in the APK, not a second implementation of its DOM checks.
const initializer = source.slice(source.indexOf("private static final String RATING_EXPRESSION ="), source.indexOf("    static String expressionFor("));
const expression = Array.from(initializer.matchAll(/"(?:[^"\\]|\\.)*"/g), match => JSON.parse(match[0])).join("");

function fixture() {
  const rect = { width: 1333, height: 800 };
  const style = { display: "block", visibility: "visible" };
  const shell = {
    hidden: false,
    dataset: { ratingLayout: "four" },
    getAttribute: () => null,
    getBoundingClientRect: () => rect,
    getClientRects: () => [rect],
  };
  const runtime = { layoutGrid() {}, loadRating() {}, state: {} };
  const window = { RatingTvScreen: runtime };
  window.top = window;
  const context = {
    window,
    location: { origin: "https://mining-master.driverform.ru", pathname: "/reports/rating/tv/" },
    document: {
      body: { classList: { contains: name => name === "rating-tv-body" } },
      querySelector: selector => selector === "main[data-rating-tv]" ? shell : null,
    },
    getComputedStyle: () => style,
  };
  return { context, shell, runtime, style, rect };
}

test("standalone rating recognizes initialized empty/loading/error UI without requiring payload", () => {
  for (const layout of ["four", "three-reserve"]) {
    for (const state of [{}, { requestInFlight: true }, { payload: null, error: "unavailable" }]) {
      const item = fixture();
      item.shell.dataset.ratingLayout = layout;
      item.runtime.state = state;
      assert.equal(runInNewContext(expression, item.context), true);
    }
  }
});

test("standalone rating rejects missing shell, runtime, body class or initialized layout", () => {
  const changes = [
    item => { item.context.document.body = null; },
    item => { item.context.document.body.classList.contains = () => false; },
    item => { item.context.document.querySelector = () => null; },
    item => { item.context.window.RatingTvScreen = null; },
    item => { item.runtime.layoutGrid = undefined; },
    item => { item.runtime.loadRating = undefined; },
    item => { item.runtime.state = null; },
    item => { item.shell.dataset.ratingLayout = ""; },
  ];
  for (const change of changes) {
    const item = fixture();
    change(item);
    assert.equal(runInNewContext(expression, item.context), false);
  }
});

test("standalone rating cannot reveal a hidden or zero-size shell", () => {
  const changes = [
    item => { item.shell.hidden = true; },
    item => { item.shell.getAttribute = () => "true"; },
    item => { item.style.display = "none"; },
    item => { item.style.visibility = "hidden"; },
    item => { item.shell.getClientRects = () => []; },
    item => { item.rect.width = 0; },
    item => { item.rect.height = Number.NaN; },
  ];
  for (const change of changes) {
    const item = fixture();
    change(item);
    assert.equal(runInNewContext(expression, item.context), false);
  }
});

test("standalone rating JavaScript rechecks exact origin, path and main frame", () => {
  const changes = [
    item => { item.context.window.top = {}; },
    item => { item.context.location.origin = "http://mining-master.driverform.ru"; },
    item => { item.context.location.origin = "https://mining-master.driverform.ru.attacker.example"; },
    item => { item.context.location.pathname = "/reports/rating/%74v/"; },
    item => { item.context.location.pathname = "/reports/rating/tv/error/"; },
    item => { item.context.location.pathname = "/login/"; },
  ];
  for (const change of changes) {
    const item = fixture();
    change(item);
    assert.equal(runInNewContext(expression, item.context), false);
  }
});
