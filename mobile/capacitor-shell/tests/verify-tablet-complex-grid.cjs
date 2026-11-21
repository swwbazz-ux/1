/* Browser regression for the APK field, using a saved sanitized board with at least
   ten complexes. All static assets and runtime handlers come from local files. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

async function verify() {
    const [fixture, output, profileArg] = process.argv.slice(2);
    if (!fixture || !output) throw new Error('Usage: node verify-tablet-complex-grid.cjs sanitized.html output-directory [profile-directory]');
    const profile = path.resolve(profileArg || path.join(__dirname, '../profiles/mining_master_tablet'));
    const url = fs.readFileSync(path.join(profile, 'app.properties'), 'utf8').match(/^startUrl=(.+)$/m)[1].trim();
    const assets = path.join(profile, 'assets/tablet');
    const staticRoot = path.resolve(__dirname, '../../../СИСТЕМА_MVP/backend/static');
    const html = fs.readFileSync(fixture, 'utf8');
    const browser = await chromium.launch({ channel: 'chrome', headless: true });
    fs.mkdirSync(output, { recursive: true });
    const results = [], errors = [];
    try {
        for (const [width, height] of [[1333, 800], [1280, 720], [1024, 600]]) {
            const page = await browser.newPage({ viewport: { width, height }, hasTouch: true, serviceWorkers: 'block' });
            page.on('pageerror', error => errors.push(error.message));
            await page.routeWebSocket('**/*', socket => socket.close());
            await page.route('**/*', route => {
                const request = route.request(), target = new URL(request.url());
                if (request.method() === 'GET' && request.url() === url) return route.fulfill({ contentType: 'text/html', body: html });
                if (request.method() === 'GET' && target.origin === new URL(url).origin && target.pathname.startsWith('/static/')) {
                    const file = path.resolve(staticRoot, decodeURIComponent(target.pathname.slice(8)));
                    if (file.startsWith(staticRoot + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) return route.fulfill({ path: file });
                }
                return route.abort();
            });
            const response = await page.goto(url);
            assert.equal(response.status(), 200);
            await page.addStyleTag({ path: path.join(assets, 'mining-master-tablet.css') });
            await page.addScriptTag({ path: path.join(assets, 'mining-master-tablet.js') });
            // Sanitized snapshots may omit scripts. Run the same reconciler and sorter used by the field.
            for (const [factory, name] of [['createDispatcherFragmentReconciler', 'dispatcher-fragment-reconciler-v1.js'],
                ['createDispatcherBoardLayout', 'dispatcher-board-layout-v1.js']]) {
                if (!await page.evaluate(key => typeof window[key] === 'function', factory)) {
                    await page.addScriptTag({ path: path.join(staticRoot, 'js', name) });
                }
            }
            await page.waitForSelector('html.cr-mm-panels');
            const grid = page.locator('.dispatcher-zone-grid');
            const measure = () => grid.evaluate(node => {
                const bounds = node.getBoundingClientRect(), css = getComputedStyle(node);
                const cards = [...node.querySelectorAll(':scope > .dispatcher-complex-card')].map(card => {
                    // offset geometry excludes the hover/pulse transform of a status indicator.
                    const box = card.getBoundingClientRect();
                    return { label: card.dataset.zoneLabel, key: card.dataset.equipmentId,
                        row: card.offsetTop, col: card.offsetLeft, height: card.offsetHeight,
                        inside: box.top >= bounds.top - 2 && box.bottom <= bounds.bottom + 2,
                        visible: Math.min(box.bottom, bounds.bottom) - Math.max(box.top, bounds.top) > 2 };
                }).sort((a, b) => a.row - b.row || a.col - b.col);
                return { cards, scroll: node.scrollTop, track: css.gridAutoRows, columns: css.gridTemplateColumns,
                    gap: parseFloat(css.rowGap), height: node.clientHeight, width: node.clientWidth,
                    scrollHeight: node.scrollHeight, scrollbar: css.scrollbarWidth,
                    windowScroll: [scrollX, scrollY], ranges: [node.scrollWidth, node.clientWidth] };
            });
            const verifyGrid = async side => {
                await grid.evaluate(node => { node.scrollTop = 0; });
                await page.waitForTimeout(250);
                const state = await measure(), cards = state.cards;
                assert.ok(cards.length > 9, 'fixture must have more than nine complexes');
                assert.equal(new Set(cards.slice(0, 9).map(card => card.row)).size, 3);
                assert.equal(new Set(cards.slice(0, 9).map(card => card.col)).size, 3);
                assert.equal(cards.filter(card => card.inside).length, 9, JSON.stringify({ width, side, state }));
                assert.equal(cards.filter(card => card.visible).length, 9);
                assert.equal(state.scrollbar, 'none'); assert.deepEqual(state.ranges, [state.width, state.width]);
                assert.ok(Math.abs(3 * cards[0].height + 2 * state.gap - state.height) <= 2);
                results.push({ width, height, side, visible: 9, row: cards[0].height, viewport: state.height });
                return cards;
            };
            const first = await verifyGrid('closed');
            const names = first.map(card => card.label);
            const naturalNumbers = names.map(name => Number(name.match(/\d+/)[0]));
            assert.deepEqual(naturalNumbers, naturalNumbers.slice().sort((a, b) => a - b));
            for (const side of ['top', 'left', 'right', 'bottom']) {
                const handle = page.locator('.cr-mm-panel-handle[data-cr-panel-side="' + side + '"]');
                await handle.click();
                await page.waitForSelector('[data-cr-panel="' + side + '"][data-cr-panel-state="open"]');
                await verifyGrid(side);
                await handle.click(); await page.waitForTimeout(260);
            }
            const region = await grid.boundingBox(), touch = await page.context().newCDPSession(page);
            const x = region.x + region.width * 0.42, y = region.y + region.height * 0.87;
            await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
            for (let part = 1; part <= 10; part++) {
                await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - region.height * 0.65 * part / 10 }] });
            }
            await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
            await page.waitForTimeout(800);
            const scrolled = await measure();
            assert.ok(scrolled.scroll > 0, 'finger swipe scrolls the field');
            assert.ok(scrolled.cards.slice(9).some(card => card.visible));
            assert.deepEqual(scrolled.windowScroll, [0, 0]);
            const changedId = await page.evaluate(() => {
                const board = document.querySelector('.dispatcher-board');
                const fragment = createDispatcherFragmentReconciler({ window, document });
                const saved = fragment.captureState(board);
                const refreshed = board.cloneNode(true);
                const newGrid = refreshed.querySelector('.dispatcher-zone-grid');
                [...newGrid.children].reverse().forEach(card => {
                    card.style.removeProperty('--cr-complex-order');
                    newGrid.appendChild(card);
                });
                const changed = newGrid.querySelector('.dispatcher-complex-card');
                changed.classList.add('status-yellow'); changed.dataset.equipmentState = 'waiting';
                fragment.reconcileBoard(board, refreshed);
                fragment.restoreState(board, saved);
                window.createDispatcherBoardLayout({ document }).normalizeComplexGrid();
                return changed.dataset.equipmentId;
            });
            await page.waitForTimeout(300);
            const after = await measure();
            assert.deepEqual(after.cards.map(card => card.label), names, 'status refresh leaves every complex in its original slot');
            assert.ok(Math.abs(after.scroll - scrolled.scroll) <= 1, JSON.stringify({ after: after.scroll, before: scrolled.scroll }));
            assert.equal(await grid.locator('[data-equipment-id="' + changedId + '"][data-equipment-state="waiting"].status-yellow').count(), 1);
            await grid.evaluate(node => { node.scrollTop = 0; }); await page.waitForTimeout(250);
            await page.screenshot({ path: path.join(output, 'grid-' + width + '.png') });
            await page.close();
        }
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output, 'grid-results.json'), JSON.stringify({ viewports: results, errors }, null, 2));
        console.log('PASS: 3 viewports x 5 panel states, real touch scroll, status reorder, keyed fragment replacement and scroll restoration.');
    } finally { await browser.close(); }
}
verify().catch(error => { console.error(error); process.exitCode = 1; });
