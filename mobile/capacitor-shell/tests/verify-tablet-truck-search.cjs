/* Runs the APK adapter and unchanged Dispatcher drag handlers in Chromium.
   The board and its POST recorder are local fixture data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '../../..');
const assets = path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet');
const statics = path.join(repo, 'СИСТЕМА_MVP/backend/static');
const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/mining-master-tablet-panels.html'), 'utf8')
    .replace('<html lang="ru"', '<html data-cr-tablet-edge-guard="active" lang="ru"')
    .replace('</head>', '<style>' + fs.readFileSync(path.join(assets, 'mining-master-tablet.css'), 'utf8') + '</style></head>');
const adapter = fs.readFileSync(path.join(assets, 'mining-master-tablet.js'), 'utf8');
const truckInput = '[data-cr-mm-assign-truck]';
const destination = '[data-cr-mm-assign-excavator]';
const submit = '[data-cr-mm-assign-submit]';
const handle = '.cr-mm-panel-handle[data-cr-panel-side=bottom]';
const assigned = '[data-dispatcher-drag=truck][data-equipment-id="9"]';
const garageTruck = '[data-dispatcher-drag=truck][data-equipment-id="1"]';
const start = new URL(fs.readFileSync(path.join(assets, '../../app.properties'), 'utf8').match(/^startUrl=(.+)$/m)[1].trim());

async function verify(browser, width, height) {
    const context = await browser.newContext({ viewport: { width, height }, hasTouch: true, serviceWorkers: 'block' });
    const page = await context.newPage();
    const errors = [];
    page.setDefaultTimeout(5000);
    page.on('pageerror', e => errors.push(e.message));
    await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin === start.origin && route.request().method() === 'GET') {
            if (url.pathname === start.pathname) return route.fulfill({ contentType: 'text/html', body: fixture });
            if (url.pathname === '/tablet-adapter.js') return route.fulfill({ contentType: 'application/javascript', body: adapter });
            if (url.pathname.startsWith('/static/')) {
                const file = path.resolve(statics, url.pathname.slice(8));
                if (file.startsWith(statics + path.sep) && fs.existsSync(file)) return route.fulfill({ path: file });
            }
        }
        errors.push('Unexpected request: ' + url.pathname);
        return route.abort();
    });
    const panel = side => page.waitForFunction(side => {
        const root = document.documentElement;
        return side ? root.dataset.crPanelOpen === side && !!document.querySelector('[data-cr-panel="' + side + '"][data-cr-panel-state="open"]')
            : !document.querySelector('[data-cr-panel-state="open"],[data-cr-panel-state="closing"]') && !root.dataset.crPanelOpen;
    }, side);
    const locate = async (value, explicit = false) => {
        await page.locator(handle).tap();
        await panel('bottom');
        await page.locator(truckInput).fill(value);
        assert.equal(await page.locator('.cr-mm-truck-hit').count(), 1);
        if (explicit) await page.locator(submit).tap();
    };
    const cleared = async () => {
        assert.equal(await page.locator(truckInput).inputValue(), '');
        assert.equal(await page.locator('.cr-mm-truck-hit').count(), 0);
    };
    const fresh = async () => {
        const response = await page.goto(start.href);
        assert.equal(response.status(), 200);
        await page.locator(handle).waitFor();
    };
    const cases = [];
    try {
        await fresh();
        await locate('9');
        await panel(null);
        assert.equal(await page.locator(assigned + '.cr-mm-truck-hit').count(), 1);
        await page.evaluate(() => { window.staleBoard = document.querySelector('.dispatcher-board').cloneNode(true); });
        await page.locator(assigned).tap();
        await cleared();
        assert.equal(await page.evaluate(() => fixtureDetails), 1);
        await page.evaluate(() => {
            const old = document.querySelector('.dispatcher-board');
            old.replaceWith(staleBoard);
            staleBoard.querySelectorAll('[data-drag-bound],[data-dispatcher-drop-bound]').forEach(node => {
                node.removeAttribute('data-drag-bound'); node.removeAttribute('data-dispatcher-drop-bound');
            });
            fixtureBind();
        });
        await page.waitForFunction(() => !document.querySelector('.cr-mm-truck-hit'));
        await cleared();
        cases.push('exact field number hides drawer, next tap still opens card, stale refresh cannot revive result');

        await fresh();
        await locate('58', true);
        await panel('right');
        assert.equal(await page.locator(garageTruck + '.cr-mm-truck-hit').count(), 1);
        assert.equal(await page.locator(truckInput).inputValue(), '58');
        await page.locator(handle).tap();
        await panel('bottom'); await cleared();
        cases.push('Find exposes garage; handle tap clears query and opens bottom drawer');

        await fresh();
        await page.evaluate(() => {
            const grid = document.querySelector('.dispatcher-zone-grid');
            for (let i = 0; i < 60; i++) {
                const card = document.createElement('article');
                card.className = 'dispatcher-complex-card'; card.textContent = 'Комплекс ' + (i + 10);
                grid.appendChild(card);
            }
        });
        await locate('9'); await panel(null);
        const grid = page.locator('.dispatcher-zone-grid');
        const before = await grid.evaluate(n => n.scrollTop);
        const area = await grid.boundingBox();
        const touch = await context.newCDPSession(page);
        const point = { x: Math.round(area.x + area.width * .65), y: Math.round(area.y + area.height * .75) };
        await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
        for (let dy = 12; dy <= 144; dy += 12) {
            await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: point.x, y: point.y - dy }] });
        }
        await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
        await touch.detach();
        await page.waitForFunction(previous => document.querySelector('.dispatcher-zone-grid').scrollTop > previous, before);
        assert.equal(await page.locator(assigned + '.cr-mm-truck-hit').count(), 1);
        assert.equal(await page.locator(truckInput).inputValue(), '9');
        cases.push('scrolling the field by touch keeps the located number and highlight');

        await fresh();
        await locate('9');
        await panel(null);
        // Browser-generated trusted drag, targeting the real board DnD handlers.
        await page.locator(assigned).dragTo(page.locator('[data-dispatcher-drop=complex][data-equipment-id="2"]'));
        await page.waitForFunction(() => fixturePosts.length === 1);
        await cleared();
        assert.equal(await page.evaluate(() => fixturePosts[0].payload.truck_id), '9');
        cases.push('drag clears the located truck and issues exactly one original fixture command');

        await fresh();
        await locate('58');
        await panel('right');
        const box = await page.locator(handle).boundingBox();
        const x = box.x + box.width / 2, y = box.y + box.height / 2;
        await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x, y - 75, { steps: 6 }); await page.mouse.up();
        await panel('bottom');
        assert.equal(await page.locator(truckInput).inputValue(), '58');
        await page.locator(destination).selectOption('2');
        await page.locator(submit).tap(); await panel(null); await cleared();
        assert.equal(await page.evaluate(() => fixturePosts.length), 1);
        assert.equal(await page.evaluate(() => fixturePosts[0].payload.excavator_id), '2');
        cases.push('swiping back resumes assignment without racing auto-hide');

        await fresh();
        await page.evaluate(() => {
            const node = document.querySelector('[data-equipment-id="1"]').cloneNode(true);
            node.dataset.equipmentId = '95'; node.dataset.equipmentName = '95';
            node.querySelector('strong').textContent = '95';
            document.querySelector('.dispatcher-trucks').appendChild(node);
        });
        await locate('9');
        await page.waitForTimeout(1150); await panel('bottom');
        await page.locator(truckInput).press('5'); await panel('right');
        assert.equal(await page.locator('[data-equipment-id="95"].cr-mm-truck-hit').count(), 1);
        await fresh();
        await page.locator(handle).tap(); await panel('bottom');
        await page.locator(destination).selectOption('2'); await page.locator(truckInput).fill('58');
        await page.waitForTimeout(1150); await panel('bottom');
        assert.equal(await page.locator(submit).textContent(), 'Назначить');
        await page.locator(submit).tap(); await panel(null); await cleared();
        assert.equal(await page.evaluate(() => fixturePosts.length), 1);
        cases.push('slow entry of 95 survives exact prefix 9; preselected assignment waits for explicit command');
        await fresh();
        await page.locator(handle).tap(); await panel('bottom');
        await page.locator(truckInput).fill('99999'); await page.locator(submit).tap();
        await panel('bottom');
        assert.equal(await page.locator(truckInput).evaluate(n => n.validity.customError), true);
        assert.equal(await page.locator('.cr-mm-truck-hit').count(), 0);
        assert.equal(await page.evaluate(() => fixturePosts.length), 0);
        cases.push('unknown number keeps the drawer open for correction');
        assert.deepEqual(errors, []);
        return { width, height, cases };
    } finally { await context.close(); }
}

(async () => {
    const browser = await chromium.launch({ channel: 'chrome', headless: true });
    try {
        for (const [width, height] of [[1333, 800], [1024, 600], [390, 844]]) {
            console.log(JSON.stringify(await verify(browser, width, height)));
        }
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
