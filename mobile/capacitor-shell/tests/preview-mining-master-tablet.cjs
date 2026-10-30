/* Offline browser fixture: genuine dispatcher TestClient HTML, local assets, no server access. */
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const fixturePath = process.argv[2];
  const outputPath = process.argv[3];
  if (!fixturePath || !outputPath) throw new Error('Usage: preview-mining-master-tablet.cjs sanitized.html output-directory');
  const repo = path.resolve(__dirname, '../../..');
  const staticRoot = path.join(repo, 'СИСТЕМА_MVP/backend/static');
  const assetRoot = path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet');
  const html = fs.readFileSync(fixturePath, 'utf8');
  const css = fs.readFileSync(path.join(assetRoot, 'mining-master-tablet.css'), 'utf8');
  const js = fs.readFileSync(path.join(assetRoot, 'mining-master-tablet.js'), 'utf8');
  fs.mkdirSync(outputPath, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1333, height: 800 }, deviceScaleFactor: 1, hasTouch: true });
  let preventedRequests = 0;
  const attemptedOperations = [];
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === 'POST' && /\/dispatcher\/control\/(truck\/assign|excavator\/move)\//.test(url.pathname)) attemptedOperations.push(url.pathname);
    if (req.method() !== 'GET') { preventedRequests++; return route.abort(); }
    if (req.isNavigationRequest() && url.origin === 'https://dispatcher.driverform.ru' && url.pathname === '/dispatcher/control/') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
    if (url.pathname.startsWith('/static/')) {
      const local = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice(8)));
      if (local.startsWith(staticRoot + path.sep) && fs.existsSync(local) && fs.statSync(local).isFile()) return route.fulfill({ path: local });
    }
    preventedRequests++;
    return route.abort();
  });
  await page.goto('https://dispatcher.driverform.ru/dispatcher/control/', { waitUntil: 'load' });
  await page.waitForTimeout(200);
  await page.screenshot({ path: path.join(outputPath, 'dispatcher-baseline-1333x800.png') });
  const baseline = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, cards: document.querySelectorAll('.dispatcher-complex-card:not(.status-empty)').length, nav: Array.from(document.querySelectorAll('.dispatcher-nav a')).map(a => ({ text: a.textContent.trim(), href: a.getAttribute('href'), height: a.getBoundingClientRect().height })) }));
  await page.addStyleTag({ content: css });
  await page.addScriptTag({ content: js });
  const diagnostic = await page.evaluate(() => Array.from(document.querySelectorAll('.dispatcher-complex-card:not(.status-empty)')).slice(0, 2).map(card => Array.from([card, ...card.children]).map(node => {
    const style = getComputedStyle(node); const r = node.getBoundingClientRect();
    return { cls: node.className, x: r.x, y: r.y, w: r.width, h: r.height, css: Object.fromEntries(['position', 'top', 'bottom', 'left', 'right', 'flex', 'flexShrink', 'flexBasis', 'order', 'margin', 'transform', 'height', 'minHeight', 'maxHeight', 'contain', 'contentVisibility', 'gridTemplateRows', 'gridAutoRows'].map(k => [k, style[k]])) };
  })));
  const results = [];
  for (const [width, height] of [[1333, 800], [1280, 800], [1333, 480], [1280, 480], [1024, 600]]) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(100);
    await page.screenshot({ path: path.join(outputPath, `tablet-${width}x${height}.png`) });
    results.push(await page.evaluate(() => {
      const rect = selector => {
        const node = document.querySelector(selector);
        if (!node) return null;
        const r = node.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height, scrollHeight: node.scrollHeight, clientHeight: node.clientHeight, transform: getComputedStyle(node).transform };
      };
      return { width: innerWidth, height: innerHeight, root: document.documentElement.className, shell: document.body.dataset.appShellVersion, overflowX: document.documentElement.scrollWidth > innerWidth, overflowY: document.documentElement.scrollHeight > innerHeight, board: rect('.dispatcher-board'), left: rect('.dispatcher-left'), center: rect('.dispatcher-complexes'), right: rect('.dispatcher-right'), toolbar: rect('#cr-mm-toolbar'), selectButtons: document.querySelectorAll('[data-cr-mm-action="select"]').length, enabledSelectButtons: document.querySelectorAll('[data-cr-mm-action="select"]:not(:disabled)').length, warning: !!document.getElementById('cr-mm-compatibility-warning') };
    }));
  }
  const keyboard = [];
  for (const width of [1333, 1280]) {
    await page.setViewportSize({ width, height: 480 });
    await page.waitForTimeout(100);
    const target = await page.locator('.complex-assigned-trucks [data-dispatcher-drag="truck"]').last().getAttribute('data-equipment-name');
    await page.locator('[data-cr-mm-search]').fill(target);
    await page.waitForTimeout(80);
    const state = await page.evaluate(() => {
      const box = node => { const r = node.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; };
      const inside = (r, outer) => r.left >= outer.left - 1 && r.right <= outer.right + 1 && r.top >= outer.top - 1 && r.bottom <= outer.bottom + 1;
      const viewport = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
      const controls = [...document.querySelectorAll('.dispatcher-nav a,.dispatcher-command-utility > *,.dispatcher-shift-button')].map(node => ({ text: node.getAttribute('aria-label') || node.textContent.trim(), ...box(node) }));
      const matches = [...document.querySelectorAll('.cr-mm-search-hit')];
      return { width: innerWidth, scale: Number(document.documentElement.style.getPropertyValue('--cr-mm-scale')), controls, controlTargetsReadable: controls.every(r => r.height >= 43 && r.width >= 43 && inside(r, viewport)), controlsDoNotOverlap: controls.every((r, i) => controls.slice(i + 1).every(s => r.right <= s.left + 1 || s.right <= r.left + 1 || r.bottom <= s.top + 1 || s.bottom <= r.top + 1)), searchVisible: inside(box(document.querySelector('[data-cr-mm-search]')), viewport), matchVisible: matches.some(node => { const grid = node.closest('.dispatcher-zone-grid'); return grid && inside(box(node), box(grid)); }), garages: ['.dispatcher-excavators', '.dispatcher-trucks'].map(selector => { const node = document.querySelector(selector), tile = node.querySelector('[data-dispatcher-drag]'); node.scrollTop = node.scrollHeight; return { tileHeight: box(tile).height, bottomReached: Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) <= 1 }; }) };
    });
    await page.screenshot({ path: path.join(outputPath, `keyboard-search-${width}x480.png`) });
    await page.locator('[data-cr-mm-search]').fill('');
    state.clearSearch = await page.locator('.cr-mm-search-hit').count() === 0;
    await page.locator('.dispatcher-trucks [data-dispatcher-drag="truck"]').first().click();
    state.detail = await page.locator('[data-gd-equipment-detail]').evaluate(node => {
      const panel = node.querySelector('.mm-equipment-detail-panel'), close = node.querySelector('button[data-gd-detail-close]'), r = panel.getBoundingClientRect(), c = close.getBoundingClientRect();
      return { visible: !node.hidden, panelWithinViewport: r.top >= -1 && r.bottom <= innerHeight + 1, closeVisible: c.top >= 0 && c.bottom <= innerHeight && c.height >= 43 };
    });
    await page.screenshot({ path: path.join(outputPath, `keyboard-detail-${width}x480.png`) });
    await page.locator('button[data-gd-detail-close]').first().click();
    keyboard.push(state);
  }
  await page.setViewportSize({ width: 1333, height: 800 });
  await page.waitForTimeout(100);
  const controls = await page.evaluate(() => Array.from(document.querySelectorAll('.dispatcher-nav a, .dispatcher-command-utility > *, .dispatcher-shift-button')).map(node => { const r = node.getBoundingClientRect(); return { text: node.getAttribute('aria-label') || node.textContent.trim(), x: r.x, y: r.y, width: r.width, height: r.height, visible: r.width > 0 && r.height > 0 && r.x >= 0 && r.right <= innerWidth && r.y >= 0 && r.bottom <= innerHeight - 64 }; }));
  const modeInitiallyDisabled = await page.locator('[data-cr-mm-action="mode"]').isDisabled();
  await page.locator('.dispatcher-trucks [data-dispatcher-drag="truck"]').first().click();
  await page.waitForTimeout(100);
  const ordinaryTapOpensDetails = await page.locator('[data-gd-equipment-detail]').evaluate(node => !node.hidden && node.getBoundingClientRect().width > 0);
  await page.screenshot({ path: path.join(outputPath, 'dispatcher-detail-1333x800.png') });
  if (ordinaryTapOpensDetails) await page.locator('button[data-gd-detail-close]').first().click();
  const overflow = {};
  const hiddenTruck = page.locator('.complex-assigned-trucks [data-dispatcher-drag="truck"][hidden]').first();
  let truck = null, more = null;
  if (await hiddenTruck.count()) {
  const hiddenTruckId = await hiddenTruck.getAttribute('data-equipment-id');
  const hiddenTruckName = await hiddenTruck.getAttribute('data-equipment-name');
  truck = page.locator(`.complex-assigned-trucks [data-dispatcher-drag="truck"][data-equipment-id="${hiddenTruckId}"]`);
  more = truck.locator('..').locator('.complex-truck-more');
  await more.click();
  await page.waitForTimeout(200);
  overflow.readonlyExpanded = await truck.isVisible();
  overflow.originalHiddenTokenPreserved = await truck.evaluate(node => node.hidden && node.dataset.dragBound === 'true');
  await page.screenshot({ path: path.join(outputPath, 'dispatcher-expanded-trucks-1333x800.png') });
  await page.mouse.move(0, 0);
  await truck.click();
  overflow.readonlyHiddenTruckDetail = await page.locator('[data-gd-equipment-detail]').evaluate(node => !node.hidden);
  await page.locator('button[data-gd-detail-close]').first().click();
  await more.click();
  overflow.collapsed = !(await truck.isVisible());
  await more.focus();
  await page.keyboard.press('Space');
  overflow.keyboardExpand = await truck.isVisible();
  await page.keyboard.press('Enter');
  overflow.keyboardCollapse = !(await truck.isVisible());
  await page.locator('[data-cr-mm-search]').fill(hiddenTruckName);
  overflow.searchRevealsHiddenTruck = await truck.isVisible();
  await page.locator('[data-cr-mm-search]').fill('');
  await more.click();
  }
  await page.evaluate(() => { const board = document.querySelector('.dispatcher-board'); board.dataset.dispatcherShiftOpen = 'true'; board.dataset.dispatcherOwnShiftOpen = 'false'; board.classList.remove('is-readonly'); });
  await page.waitForTimeout(100);
  const activeOtherShiftEnabled = !(await page.locator('[data-cr-mm-action="mode"]').isDisabled());
  await page.locator('[data-cr-mm-action="mode"]').click();
  if (more) {
  await more.click();
  overflow.moreDoesNotSelectComplex = await page.locator('.cr-mm-selected').count() === 0;
  await page.mouse.move(0, 0);
  await truck.click();
  overflow.hiddenTruckSelected = await truck.evaluate(node => node.classList.contains('cr-mm-selected'));
  await page.locator('[data-cr-mm-action="cancel"]').click();
  await page.locator('[data-cr-mm-action="mode"]').click();
  }
  await page.locator('.dispatcher-trucks [data-dispatcher-drag="truck"]').first().click();
  const selectionCount = await page.locator('.cr-mm-selected').count();
  /* Repeated injection is idempotent; fragment replacement cancels operation mode. */
  const lifecycle = await page.evaluate(() => {
    const oldBoard = document.querySelector('.dispatcher-board');
    const before = document.querySelectorAll('#cr-mm-toolbar').length;
    window.CopperMiningMasterTablet.init();
    window.CopperMiningMasterTablet.init();
    const after = document.querySelectorAll('#cr-mm-toolbar').length;
    const replacement = oldBoard.cloneNode(true);
    replacement.querySelectorAll('[data-cr-mm-action]').forEach(node => node.remove());
    oldBoard.replaceWith(replacement);
    return { before, after };
  });
  await page.waitForTimeout(100);
  lifecycle.afterFragment = await page.locator('#cr-mm-toolbar').count();
  lifecycle.selectionAfterFragment = await page.locator('.cr-mm-selected').count();
  lifecycle.expandedRacksAfterFragment = await page.locator('.cr-mm-expanded-rack').count();
  lifecycle.modeAfterFragment = await page.locator('[data-cr-mm-action="mode"]').getAttribute('aria-pressed');
  lifecycle.toolbarCount = await page.locator('#cr-mm-toolbar').count();
  await page.evaluate(() => { document.body.dataset.appShellVersion = 'dispatcher-desktop-shell-v999'; });
  await page.waitForTimeout(100);
  lifecycle.unknownVersion = await page.evaluate(() => ({ warning: !!document.getElementById('cr-mm-compatibility-warning'), injectedControls: document.querySelectorAll('[data-cr-mm-action]').length, tabletLayout: document.documentElement.classList.contains('cr-mm-tablet') }));
  const report = { baseline, controls, results, keyboard, diagnostic, modeInitiallyDisabled, ordinaryTapOpensDetails, overflow, activeOtherShiftEnabled, selectionCount, lifecycle, attemptedOperations, errors, preventedRequests, network: 'All requests intercepted; genuine TestClient HTML, in-memory test data and local static files only.' };
  fs.writeFileSync(path.join(outputPath, 'fixture-results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
  if (errors.length || attemptedOperations.length || !ordinaryTapOpensDetails || !modeInitiallyDisabled || keyboard.some(k => k.scale < .7 || !k.controlTargetsReadable || !k.controlsDoNotOverlap || !k.searchVisible || !k.matchVisible || !k.clearSearch || !k.detail.visible || !k.detail.panelWithinViewport || !k.detail.closeVisible || k.garages.some(g => g.tileHeight < 43 || !g.bottomReached)) || Object.values(overflow).some(value => value !== true) || !activeOtherShiftEnabled || selectionCount !== 1 || results.some(r => r.warning || r.overflowX || r.overflowY || !r.toolbar) || lifecycle.before !== lifecycle.after || lifecycle.afterFragment !== lifecycle.before || lifecycle.selectionAfterFragment !== 0 || lifecycle.expandedRacksAfterFragment !== 0 || lifecycle.modeAfterFragment !== 'false' || lifecycle.toolbarCount !== 1 || lifecycle.unknownVersion.injectedControls !== 0) process.exitCode = 1;
})().catch(error => { console.error(error); process.exit(1); });
