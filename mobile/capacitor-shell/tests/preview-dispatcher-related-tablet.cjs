/* Read-only browser QA. All document/static bytes come from local files. */
const fs = require('node:fs');
const path = require('node:path');
// Set COPPER_PLAYWRIGHT_MODULE to the local Playwright package directory when
// using a bundled runtime; otherwise install Playwright in the Node search path.
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const fixtureDir = process.argv[2], outputDir = process.argv[3];
  if (!fixtureDir || !outputDir) throw new Error('Usage: preview-dispatcher-related-tablet.cjs fixture-directory output-directory');
  const repo = path.resolve(__dirname, '../../..');
  const staticRoot = path.join(repo, 'СИСТЕМА_MVP/backend/static');
  const assets = path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet');
  const css = fs.readFileSync(path.join(assets, 'mining-master-tablet.css'), 'utf8');
  const js = fs.readFileSync(path.join(assets, 'mining-master-tablet.js'), 'utf8');
  const pages = [['reports', '/mining-master/tablet/dispatcher/reports/'], ['journal', '/mining-master/tablet/dispatcher/shift-log/'], ['analytics', '/mining-master/tablet/dispatcher/management/'], ['truck-report', '/mining-master/tablet/dispatcher/shift-reports/trucks/'], ['volume', '/mining-master/tablet/reports/volume/'], ['customer-daily', '/mining-master/tablet/reports/customer-daily/'], ['templates', '/mining-master/tablet/reports/templates/'], ['common-downtimes', '/mining-master/tablet/reports/downtimes/']];
  const documents = new Map(pages.map(([name, route]) => [route, fs.readFileSync(path.join(fixtureDir, `${name}-sanitized.html`), 'utf8')]));
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1333, height: 800 }, deviceScaleFactor: 1, hasTouch: true });
  const blocked = [], errors = [], reports = [];
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url());
    if (req.method() === 'GET' && req.isNavigationRequest() && url.origin === 'https://mining-master.driverform.ru' && documents.has(url.pathname)) return route.fulfill({ contentType: 'text/html; charset=utf-8', body: documents.get(url.pathname) });
    if (req.method() === 'GET' && url.pathname.startsWith('/static/')) {
      const local = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice(8)));
      if (local.startsWith(staticRoot + path.sep) && fs.existsSync(local) && fs.statSync(local).isFile()) return route.fulfill({ path: local });
    }
    blocked.push({ method: req.method(), path: url.pathname });
    return route.abort();
  });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  for (const [name, route] of pages) {
    await page.setViewportSize({ width: 1333, height: 800 });
    await page.goto('https://mining-master.driverform.ru' + route);
    // The sanitization removes input nodes. Restore only the known date filter
    // geometry from the server template, with a synthetic date and no submission.
    const syntheticDateFilters = await page.evaluate(() => {
      let count = 0;
      document.querySelectorAll('.dispatcher-report-filters label,.dispatcher-report-controlbar-filters label,.dispatcher-shift-report-filters label').forEach(label => {
        if (label.querySelector('input,select') || label.textContent.trim().toLocaleLowerCase('ru') !== 'дата') return;
        const input = document.createElement('input'); input.type = 'date'; input.name = 'date'; input.value = '2026-10-30'; label.appendChild(input); count++;
      });
      return count;
    });
    // Sanitized live DOM has no scripts. Re-enable only the local canvas sizing
    // script so the original resize behavior is represented in this fixture.
    if (await page.locator('[data-dispatcher-canvas]').count()) await page.addScriptTag({ content: fs.readFileSync(path.join(staticRoot, 'js/dispatcher-canvas-v1.js'), 'utf8') });
    await page.screenshot({ path: path.join(outputDir, `${name}-before.png`) });
    const originalLinks = await page.locator('a[href]').evaluateAll(nodes => nodes.map(node => [node.textContent.trim(), node.getAttribute('href')]));
    const originalForms = await page.locator('form').evaluateAll(nodes => nodes.map(node => [node.method, node.getAttribute('action'), [...node.querySelectorAll('input,select,button')].map(item => [item.tagName, item.getAttribute('name'), item.getAttribute('type')])]));
    await page.addStyleTag({ content: css });
    await page.addScriptTag({ content: js });
    await page.waitForTimeout(60);
    await page.screenshot({ path: path.join(outputDir, `${name}-after.png`) });
    const metrics = await page.evaluate(() => {
      const box = node => { const r = node.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom }; };
      const shell = document.querySelector('.dispatcher-analytics-shell'), canvas = document.querySelector('[data-dispatcher-canvas]') || shell;
      return { route: location.pathname, active: document.documentElement.classList.contains('cr-mm-tablet-page'), canvas: box(canvas), canvasTransform: getComputedStyle(canvas).transform, shell: box(shell), scrollHeight: shell.scrollHeight, clientHeight: shell.clientHeight, boardControls: document.querySelectorAll('#cr-mm-toolbar, [data-cr-mm-action]').length, documentOverflow: document.documentElement.scrollWidth > innerWidth + 1, controls: [...document.querySelectorAll('.dispatcher-nav a,.dispatcher-command-utility > *, .dispatcher-shift-button')].map(node => ({ text: node.getAttribute('aria-label') || node.textContent.trim(), ...box(node) })), tableScrollContainers: [...document.querySelectorAll('.dispatcher-report-table-wrap,.dispatcher-shift-report-table-wrap')].map(node => ({ ...box(node), clientWidth: node.clientWidth, scrollWidth: node.scrollWidth, overflow: getComputedStyle(node).overflowX })) };
    });
    metrics.syntheticDateFilters = syntheticDateFilters;
    metrics.horizontalScroll = await page.locator('.dispatcher-report-table-wrap,.dispatcher-shift-report-table-wrap,.management-linked-report-content .table-wrap').evaluateAll(nodes => nodes.map(node => {
      const before = node.scrollLeft;
      node.scrollLeft = node.scrollWidth;
      const last = node.querySelector('thead tr:last-child > :last-child'), r = node.getBoundingClientRect();
      const result = { clientWidth: node.clientWidth, scrollWidth: node.scrollWidth, overflow: getComputedStyle(node).overflowX, maxReached: Math.abs(node.scrollWidth - node.clientWidth - node.scrollLeft) <= 1, lastHeaderReachable: !last || last.getBoundingClientRect().right <= r.right + 1 };
      node.scrollLeft = before;
      return result;
    }));
    metrics.syncBadgeClear = await page.evaluate(() => {
      const badge = document.querySelector('.dispatcher-shift-report-sync');
      if (!badge) return true;
      const r = badge.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && [...document.querySelectorAll('.dispatcher-shift-report-controlbar > .dispatcher-report-actions > *')].every(node => {
        const c = node.getBoundingClientRect(); return r.right <= c.left || r.left >= c.right || r.bottom <= c.top || r.top >= c.bottom;
      });
    });
    metrics.linksPreserved = JSON.stringify(originalLinks) === JSON.stringify(await page.locator('a[href]').evaluateAll(nodes => nodes.map(node => [node.textContent.trim(), node.getAttribute('href')])));
    metrics.formsPreserved = JSON.stringify(originalForms) === JSON.stringify(await page.locator('form').evaluateAll(nodes => nodes.map(node => [node.method, node.getAttribute('action'), [...node.querySelectorAll('input,select,button')].map(item => [item.tagName, item.getAttribute('name'), item.getAttribute('type')])])));
    metrics.scroll = await page.locator('.dispatcher-analytics-shell').evaluate(node => { node.scrollTop = node.scrollHeight; return { top: node.scrollTop, max: node.scrollHeight - node.clientHeight, bottomReached: Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) <= 1 }; });
    await page.screenshot({ path: path.join(outputDir, `${name}-bottom.png`) });
    await page.setViewportSize({ width: 1280, height: 800 });
    metrics.resize = await page.locator('[data-dispatcher-canvas], body > .dispatcher-analytics-shell').evaluate(node => ({ width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height, transform: getComputedStyle(node).transform }));
    if (process.env.COPPER_PRINT_QA === '1' && (process.env.COPPER_PRINT_PAGES || 'reports,truck-report').split(',').includes(name)) {
      await page.emulateMedia({ media: 'print' });
      metrics.print = await page.evaluate(() => {
        const canvas = document.querySelector('[data-dispatcher-canvas]'), shell = document.querySelector('.dispatcher-analytics-shell');
        return { position: getComputedStyle(canvas).position, transform: getComputedStyle(canvas).transform, overflow: getComputedStyle(shell).overflow, background: getComputedStyle(document.body).backgroundColor, horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, tileCount: document.querySelectorAll('.dispatcher-report-tile').length, rowCount: document.querySelectorAll('.dispatcher-shift-report-table tbody tr').length, selectedDate: document.querySelector('input[name="date"]')?.value || null };
      });
      await page.pdf({ path: path.join(outputDir, `${name}-print.pdf`), format: 'A4', landscape: true, printBackground: true, preferCSSPageSize: true });
      await page.emulateMedia({ media: 'screen' });
    }
    await page.evaluate(() => { document.body.dataset.appShellVersion = 'dispatcher-desktop-shell-v999'; window.CopperMiningMasterTablet.init(); });
    metrics.unknownVersionDisabled = !(await page.locator('html').evaluate(node => node.classList.contains('cr-mm-tablet-page')));
    reports.push(metrics);
  }
  await browser.close();
  const result = { reports, blocked, errors, scope: 'Sanitized live HTML + local CSS/canvas JS; no server requests or production operations.' };
  fs.writeFileSync(path.join(outputDir, 'related-results.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  if (errors.length || reports.some(r => !r.active || r.canvasTransform !== 'none' || r.canvas.width < 1332 || r.canvas.height < 799 || r.boardControls || r.documentOverflow || !r.linksPreserved || !r.formsPreserved || !r.syncBadgeClear || !r.scroll.bottomReached || r.horizontalScroll.some(t => !t.maxReached || !t.lastHeaderReachable) || !r.unknownVersionDisabled || r.resize.transform !== 'none' || r.resize.width < 1279 || (r.print && (r.print.position !== 'static' || r.print.transform !== 'none' || r.print.overflow !== 'visible' || r.print.background !== 'rgb(255, 255, 255)' || r.print.horizontalOverflow)) || r.controls.some(c => c.x < -1 || c.right > 1334 || c.height < 43))) process.exitCode = 1;
})().catch(error => { console.error(error); process.exit(1); });
