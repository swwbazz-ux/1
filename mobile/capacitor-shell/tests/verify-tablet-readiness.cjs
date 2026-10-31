/* Browser integration regression, all requests served from local fixture bytes.
   COPPER_PLAYWRIGHT_MODULE may point at a bundled Playwright package directory. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const repo = path.resolve(__dirname, '../../..');
  const output = path.resolve(process.argv[2] || path.join(repo, 'qa_artifacts/tablet-readiness'));
  fs.mkdirSync(output, { recursive: true });
  const adapter = fs.readFileSync(process.env.COPPER_TABLET_SCRIPT || path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.js'), 'utf8');
  const java = fs.readFileSync(path.join(__dirname, '../android/app/src/main/java/ru/copperresources/mobile/StartupLoadingOverlay.java'), 'utf8');
  const expression = java.match(/private static final String READINESS_PROBE\s*=([\s\S]*?);\r?\n\r?\n/)[1];
  const probe = vm.runInNewContext(expression, { STANDALONE_READINESS_EXTENSION: '' });
  const requiredFrames = Number(java.match(/REQUIRED_STABLE_FRAMES\s*=\s*(\d+)/)[1]);
  const requiredQuiet = Number(java.match(/REQUIRED_QUIET_WINDOW_MS\s*=\s*(\d+)/)[1]);
  const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/mining-master-tablet-readiness.html'), 'utf8');
  const readonly = fs.readFileSync(path.join(repo, 'СИСТЕМА_MVP/backend/static/js/role-readonly.js'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.css'), 'utf8');
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1333, height: 800 }, hasTouch: true });
  const errors = [], requests = [], stages = [];
  const result = { requiredFrames, requiredQuiet, stages, errors, requests };
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const req = route.request(), url = new URL(req.url());
    if (req.method() === 'GET' && url.origin === 'https://mining-master.driverform.ru') {
      if (url.pathname === '/mining-master/tablet/') return route.fulfill({ contentType: 'text/html', body: fixture.replace('</head>', `<style>${css}</style></head>`) });
      if (url.pathname === '/static/js/role-readonly.js') return route.fulfill({ contentType: 'application/javascript', body: readonly });
      if (url.pathname.startsWith('/static/img/equipment/')) return route.fulfill({contentType:'image/png',body:fs.readFileSync(path.join(repo,'СИСТЕМА_MVP/backend',url.pathname.slice(1)))});
      if (url.pathname === '/tablet-adapter.js') return route.fulfill({ contentType: 'application/javascript', body: adapter });
    }
    requests.push({ method: req.method(), path: url.pathname });
    return route.abort();
  });
  async function sample(name, duration = 700, action = '') {
    const metrics = await page.evaluate(async ({ probe, duration, action }) => {
      const records = [], samples = [];
      const observer = new MutationObserver(items => items.forEach(record => {
        const node = record.target;
        records.push({ type: record.type, target: node.tagName, attribute: record.attributeName, old: record.oldValue, current: record.type === 'attributes' ? node.getAttribute(record.attributeName) : null });
      }));
      observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeOldValue: true, attributeFilter: ['class', 'style', 'hidden', 'disabled', 'data-role-access-active'] });
      if (action) window.eval(action);
      const start = performance.now();
      while (performance.now() - start < duration) {
        const parts = window.eval(probe).split('|');
        samples.push({ ready: parts[0], frames: Number(parts[2]), quiet: Number(parts[3]), fonts: parts[4], images: parts[5], role: parts[6], visible: parts[7] });
        await new Promise(requestAnimationFrame);
      }
      observer.disconnect();
      return { samples, mutationCount: records.length, noOpMutations: records.filter(r => r.type === 'attributes' && r.old === r.current).length, records: records.slice(0, 32) };
    }, { probe, duration, action });
    metrics.name = name;
    metrics.ready = metrics.samples.some(s => s.ready === 'complete' && s.frames >= requiredFrames && s.quiet >= requiredQuiet && s.fonts === '1' && s.images === '1' && s.role === '1' && s.visible === '1');
    stages.push(metrics);
    return metrics;
  }
  try {
    await page.goto('https://mining-master.driverform.ru/mining-master/tablet/');
    assert.equal((await sample('closed-shift startup', 1000)).ready, true, 'closed shift must reach the unchanged native readiness gate');
    const repeated = await sample('repeated init', 700, 'for (var i=0;i<12;i++) window.CopperMiningMasterTablet.init();');
    assert.equal(repeated.ready, true);
    assert.equal(repeated.noOpMutations, 0, 'stable adapter reinitialization must not emit attribute no-ops');
    await page.evaluate(() => {
      window.__fixtureNoopTimer = setInterval(() => {
        const board = document.querySelector('.dispatcher-board');
        board.setAttribute('data-dispatcher-shift-open', board.getAttribute('data-dispatcher-shift-open'));
        document.body.setAttribute('data-role-access-active', document.body.getAttribute('data-role-access-active'));
      }, 30);
    });
    const unchanged = await sample('unchanged server permission attributes');
    assert.equal(unchanged.ready, true, 'unchanged server attributes must not keep the tablet/native layout busy');
    assert.equal(unchanged.records.filter(r => ['class', 'style', 'hidden', 'disabled'].includes(r.attribute)).length, 0);
    await page.evaluate(() => {
      clearInterval(window.__fixtureNoopTimer);
      const board = document.querySelector('.dispatcher-board');
      board.classList.remove('is-readonly'); board.dataset.dispatcherShiftOpen = 'true'; board.dataset.dispatcherOwnShiftOpen='true';
    });
    await page.waitForSelector('[data-cr-mm-assign-truck]');
    assert.equal((await sample('real shift opened')).ready, true);
    await page.locator('[data-cr-mm-assign-truck]').fill('1');
    assert.equal(await page.locator('.cr-mm-truck-hit').count(), 1);
    await page.evaluate(() => { document.querySelector('[data-dispatcher-drag="truck"]').dataset.haulAssignmentStateId = '11'; });
    assert.equal((await sample('real assignment changed')).ready, true);
    await page.evaluate(() => { const board = document.querySelector('.dispatcher-board'); board.dataset.dispatcherShiftOpen = 'false'; board.dataset.dispatcherOwnShiftOpen='false'; board.classList.add('is-readonly'); });
    assert.equal((await sample('real shift closed')).ready, true);
    await page.evaluate(() => {
      const board = document.querySelector('.dispatcher-board');
      board.replaceWith(board.cloneNode(true));
    });
    assert.equal((await sample('fragment replaced')).ready, true);
    assert.equal(await page.locator('#cr-mm-toolbar').count(), 1);
    assert.deepEqual(errors, []);
    assert.deepEqual(requests, [], 'fixture must perform no external or business request');
  } catch (error) {
    result.failure = error.message; process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(output, 'readiness-results.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ stages: stages.map(s => ({ name: s.name, ready: s.ready, mutations: s.mutationCount, noOps: s.noOpMutations, maxQuiet: Math.max(...s.samples.map(p => p.quiet)), maxFrames: Math.max(...s.samples.map(p => p.frames)) })), failure: result.failure, errors, requests }, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
