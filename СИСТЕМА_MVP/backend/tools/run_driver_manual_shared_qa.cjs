const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const baseUrl = process.env.QA_BASE_URL || 'http://127.0.0.1:8769';
const outputDir = path.resolve(process.env.QA_OUTPUT_DIR || 'qa-artifacts/driver-manual-shared-excavator');
const executablePath = process.env.QA_CHROMIUM_PATH;
const driverPhone = process.env.DRIVER_QA_PHONE;
const driverPin = process.env.DRIVER_QA_PIN;
const driverSessionKey = process.env.DRIVER_QA_SESSION_KEY;
const excavatorPhone = process.env.EXCAVATOR_QA_PHONE;
const excavatorPin = process.env.EXCAVATOR_QA_PIN;
const excavatorSessionKey = process.env.EXCAVATOR_QA_SESSION_KEY;
const skipExcavator = process.env.SKIP_EXCAVATOR_QA === '1';

if (!driverSessionKey && ![driverPhone, driverPin].every(Boolean)) {
    throw new Error('Set DRIVER_QA_SESSION_KEY or DRIVER_QA_PHONE and DRIVER_QA_PIN.');
}
if (!skipExcavator && !excavatorSessionKey && ![excavatorPhone, excavatorPin].every(Boolean)) {
    throw new Error('Set EXCAVATOR_QA_SESSION_KEY or EXCAVATOR_QA_PHONE and EXCAVATOR_QA_PIN.');
}

fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

async function login(page, phone, pin, expectedPath) {
    await page.goto(`${baseUrl}/?form=1`, { waitUntil: 'networkidle' });
    await page.locator('#login-phone').fill(phone);
    await Promise.all([
        page.waitForLoadState('networkidle'),
        page.locator('button[type="submit"]').click(),
    ]);
    await page.locator('#login-pin').fill(pin);
    await Promise.all([
        page.waitForLoadState('networkidle'),
        page.locator('button[type="submit"]').click(),
    ]);
    assert(new URL(page.url()).pathname.startsWith(expectedPath), `Unexpected login target: ${page.url()}`);
    await page.waitForTimeout(1800);
}

async function openAuthenticated(context, page, sessionKey, phone, pin, expectedPath) {
    if (!sessionKey) return login(page, phone, pin, expectedPath);
    await context.addCookies([{name: 'sessionid', value: sessionKey, url: baseUrl}]);
    await page.goto(`${baseUrl}${expectedPath}`, {waitUntil: 'networkidle'});
    assert(new URL(page.url()).pathname.startsWith(expectedPath), `Unexpected session target: ${page.url()}`);
    await page.waitForTimeout(700);
}

async function dispatchTouch(cdp, type, point) {
    await cdp.send('Input.dispatchTouchEvent', {
        type,
        touchPoints: point ? [{ x: point.x, y: point.y, radiusX: 4, radiusY: 4, force: 1 }] : [],
    });
}

async function center(locator) {
    const rect = await locator.boundingBox();
    assert(rect, 'Interactive element has no layout box.');
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
}

async function dragVisualSnapshot(page, sourceSelector, targetSelector) {
    return page.evaluate(({sourceSelector, targetSelector}) => {
        const source = document.querySelector(sourceSelector);
        const preview = document.querySelector('.truck-drag-preview');
        const comet = document.querySelector('.eo-truck-comet');
        const target = document.querySelector(targetSelector);
        const shell = target?.closest('.eo-shell');
        const sibling = Array.from(shell?.querySelectorAll('[data-eo-dump-target]') || [])
            .find((node) => node !== target && !node.classList.contains('is-drop-ready'));
        const sourceRect = source?.getBoundingClientRect();
        const previewRect = preview?.getBoundingClientRect();
        const cometRect = comet?.getBoundingClientRect();
        const targetRect = target?.getBoundingClientRect();
        const siblingRect = sibling?.getBoundingClientRect();
        const previewStyle = preview ? getComputedStyle(preview) : null;
        const cometStyle = comet ? getComputedStyle(comet) : null;
        const targetStyle = target ? getComputedStyle(target) : null;
        const sourceImageRect = source?.querySelector('i img')?.getBoundingClientRect();
        const allTargetTextFits = Array.from(shell?.querySelectorAll('[data-eo-dump-target]') || []).every((node) => {
            const cardRect = node.getBoundingClientRect();
            const textRect = node.querySelector('strong')?.getBoundingClientRect();
            return !!textRect && textRect.left >= cardRect.left && textRect.right <= cardRect.right && textRect.top >= cardRect.top && textRect.bottom <= cardRect.bottom;
        });
        return {
            sourceAspect: sourceRect ? sourceRect.width / sourceRect.height : 0,
            previewAspect: previewRect ? previewRect.width / previewRect.height : 0,
            sourceImageAspect: sourceImageRect ? sourceImageRect.width / sourceImageRect.height : 0,
            previewBorderWidth: previewStyle?.borderTopWidth || '',
            previewBorderColor: previewStyle?.borderTopColor || '',
            previewBoxShadow: previewStyle?.boxShadow || '',
            previewHasGlint: !!preview?.querySelector('.eo-truck-pickup-glint'),
            cometPosition: cometStyle?.position || '',
            cometDisplay: cometStyle?.display || '',
            cometOpacity: Number.parseFloat(cometStyle?.opacity || '0'),
            cometWidth: cometRect?.width || 0,
            cometHeight: cometRect?.height || 0,
            cometPath: comet?.querySelector('path[d]')?.getAttribute('d') || '',
            innerShellActive: !!shell?.classList.contains('is-truck-drag-active'),
            targetReady: !!target?.classList.contains('is-drop-ready'),
            targetHeight: targetRect?.height || 0,
            siblingHeight: siblingRect?.height || 0,
            targetToSiblingHeight: targetRect && siblingRect ? targetRect.height / siblingRect.height : 0,
            targetBorderColor: targetStyle?.borderTopColor || '',
            targetBoxShadow: targetStyle?.boxShadow || '',
            allTargetTextFits,
        };
    }, {sourceSelector, targetSelector});
}

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath });
    const report = {
        viewport: { width: 412, height: 915, deviceScaleFactor: 1 },
        source: 'standard Django local server',
        screenshots: [],
        checks: [],
        consoleErrors: [],
        browserWarnings: [],
        pageErrors: [],
        failedRequests: [],
        gestureRequests: [],
        dragVisuals: {},
    };

    function observe(page) {
        page.on('console', (message) => {
            if (message.type() !== 'error') return;
            if (/Blocked call to navigator\.vibrate/.test(message.text())) {
                report.browserWarnings.push(message.text());
                return;
            }
            report.consoleErrors.push(message.text());
        });
        page.on('pageerror', (error) => report.pageErrors.push(error.message));
        page.on('requestfailed', (request) => report.failedRequests.push(`${request.method()} ${request.url()}`));
    }

    try {
        let excavatorDragVisual = null;
        if (!skipExcavator) {
            const excavatorContext = await browser.newContext({
                viewport: report.viewport,
                deviceScaleFactor: 1,
                isMobile: true,
                hasTouch: true,
            });
            const excavatorPage = await excavatorContext.newPage();
            observe(excavatorPage);
            await openAuthenticated(excavatorContext, excavatorPage, excavatorSessionKey, excavatorPhone, excavatorPin, '/excavator/work/');
            await excavatorPage.locator('[data-eo-truck-card]').first().waitFor({ state: 'visible' });
            await excavatorPage.locator('[data-eo-dump-target]').first().waitFor({ state: 'visible' });
            assert(await excavatorPage.locator('[data-driver-manual-action-row], [data-driver-manual-close], [data-driver-manual-point-open], [data-driver-manual-source-row]').count() === 0, 'Driver-only manual controls leaked into the Excavator workplace.');
            await excavatorPage.waitForTimeout(900);
            const excavatorShot = path.join(outputDir, 'A-current-excavator-412x915.png');
            await excavatorPage.screenshot({ path: excavatorShot });
            report.screenshots.push({ id: 'A', file: path.basename(excavatorShot), url: excavatorPage.url() });
            const excavatorSource = excavatorPage.locator('[data-eo-truck-card][data-eo-can-load="1"]').first();
            const excavatorTargets = excavatorPage.locator('[data-eo-dump-target]');
            assert(await excavatorSource.count() === 1, 'Excavator fixture has no active draggable truck card.');
            const excavatorStart = await center(excavatorSource);
            const excavatorFinish = await center(excavatorTargets.first());
            const excavatorTargetId = await excavatorTargets.first().getAttribute('data-eo-dump-target');
            const excavatorCdp = await excavatorContext.newCDPSession(excavatorPage);
            await dispatchTouch(excavatorCdp, 'touchStart', excavatorStart);
            await dispatchTouch(excavatorCdp, 'touchMove', {
                x: excavatorStart.x + (excavatorFinish.x - excavatorStart.x) * 0.45,
                y: excavatorStart.y + (excavatorFinish.y - excavatorStart.y) * 0.45,
            });
            await excavatorPage.waitForTimeout(70);
            await dispatchTouch(excavatorCdp, 'touchMove', excavatorFinish);
            await excavatorPage.waitForTimeout(90);
            excavatorDragVisual = await dragVisualSnapshot(
                excavatorPage,
                '[data-eo-truck-card][data-eo-can-load="1"]',
                `[data-eo-dump-target="${excavatorTargetId}"]`,
            );
            report.dragVisuals.excavator = excavatorDragVisual;
            assert(excavatorDragVisual.innerShellActive && excavatorDragVisual.targetReady, 'Excavator reference drag did not enter the canonical active target state.');
            assert(excavatorDragVisual.cometPosition === 'fixed' && excavatorDragVisual.cometWidth >= 411 && excavatorDragVisual.cometHeight >= 914 && excavatorDragVisual.cometPath.length > 8, 'Excavator reference comet is not visibly full-screen.');
            const excavatorDragShot = path.join(outputDir, 'A2-current-excavator-drag-412x915.png');
            await excavatorPage.screenshot({ path: excavatorDragShot });
            report.screenshots.push({ id: 'A2', file: path.basename(excavatorDragShot), url: excavatorPage.url() });
            await dispatchTouch(excavatorCdp, 'touchCancel');
            await excavatorPage.waitForTimeout(80);
            assert(await excavatorPage.locator('.truck-drag-preview, .eo-truck-comet').count() === 0, 'Excavator reference drag did not clean up after cancellation.');
            report.checks.push('A/A2: current Excavator workplace and its real cancelled drag provide the visual/mechanical parity reference without a mutation request.');
        }

        const driverContext = await browser.newContext({
            viewport: report.viewport,
            deviceScaleFactor: 1,
            isMobile: true,
            hasTouch: true,
        });
        const driverPage = await driverContext.newPage();
        observe(driverPage);
        let manualTripNumber = 9000;
        const syncedManualEvents = [];
        await driverPage.route('**/offline-events/sync/', async (route) => {
            const request = route.request();
            const batch = JSON.parse(request.postData() || '{}');
            const results = (batch.events || []).map((event) => {
                if (event.event_type === 'driver.trip.loaded') {
                    manualTripNumber += 1;
                    syncedManualEvents.push(event);
                    return {
                        event_id: event.event_id,
                        status: 'accepted',
                        server_received_at: new Date().toISOString(),
                        server_ids: {trip_id: manualTripNumber, shift_id: event.shift_id},
                        trip_origin: 'driver_manual',
                    };
                }
                return {event_id: event.event_id, status: 'accepted', server_ids: {}};
            });
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({protocol_version: 1, results}),
            });
        });
        await openAuthenticated(driverContext, driverPage, driverSessionKey, driverPhone, driverPin, '/driver/');
        await driverPage.locator('[data-driver-manual-open]').waitFor({ state: 'visible' });
        await driverPage.locator('[data-driver-manual-open]').click();
        await driverPage.locator('[data-driver-manual-workspace]').waitFor({ state: 'visible' });
        const source = driverPage.locator('[data-driver-manual-source]').first();
        const targets = driverPage.locator('[data-driver-manual-dump-target]');
        await source.waitFor({ state: 'visible' });
        assert(await targets.count() === 3, 'Driver manual workplace did not render all three configured targets.');
        await driverPage.waitForTimeout(500);

        const layout = await driverPage.evaluate(() => {
            const nav = document.querySelector('[data-driver-bottom-nav]').getBoundingClientRect();
            const topbar = document.querySelector('[data-driver-manual-workspace] .eo-topbar').getBoundingClientRect();
            const heading = document.querySelector('[data-driver-manual-workspace] .eo-dashboard-head').getBoundingClientRect();
            const back = document.querySelector('.driver-manual-workspace__action--return').getBoundingClientRect();
            const source = document.querySelector('[data-driver-manual-source]').getBoundingClientRect();
            const point = document.querySelector('[data-driver-manual-point-open]').getBoundingClientRect();
            const timerNode = document.querySelector('[data-driver-manual-trip-timer]');
            const timer = timerNode.getBoundingClientRect();
            const firstTarget = document.querySelector('[data-driver-manual-dump-target]').getBoundingClientRect();
            const workGrid = document.querySelector('.driver-manual-workspace__source-layout');
            const workGridRect = workGrid.getBoundingClientRect();
            const workGridStyle = getComputedStyle(workGrid);
            const sourceHit = document.elementFromPoint(source.left + source.width / 2, source.top + 6);
            const actions = [
                document.querySelector('.driver-manual-workspace__action--return'),
                document.querySelector('[data-driver-manual-point-open]'),
            ];
            return {
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                nav: { top: nav.top, bottom: nav.bottom, height: nav.height },
                topbar: {top: topbar.top, bottom: topbar.bottom},
                heading: {top: heading.top, bottom: heading.bottom},
                actionRow: [back, point].map((rect) => ({left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height})),
                actionGap: point.left - back.right,
                source: {left: source.left, right: source.right, top: source.top, bottom: source.bottom, width: source.width, height: source.height},
                workGrid: {
                    top: workGridRect.top,
                    bottom: workGridRect.bottom,
                    left: workGridRect.left,
                    right: workGridRect.right,
                    height: workGridRect.height,
                    rows: workGridStyle.gridTemplateRows.split(' ').map(Number.parseFloat),
                },
                timer: {
                    left: timer.left,
                    right: timer.right,
                    top: timer.top,
                    bottom: timer.bottom,
                    height: timer.height,
                    active: timerNode.dataset.driverManualTimerActive,
                    value: timerNode.querySelector('[data-driver-manual-trip-timer-value]')?.textContent.trim(),
                },
                firstTargetTop: firstTarget.top,
                sourceTopHit: !!(sourceHit && sourceHit.closest('[data-driver-manual-source]')),
                actionTexts: actions.map((button) => ({
                    label: button.querySelector('strong')?.textContent.trim(),
                    hint: button.querySelector('em')?.textContent.trim(),
                    fits: button.scrollWidth <= button.clientWidth && button.scrollHeight <= button.clientHeight,
                })),
                leanDumpCards: Array.from(document.querySelectorAll('[data-driver-manual-dump-target]')).every((card) =>
                    card.children.length === 1 &&
                    card.firstElementChild?.classList.contains('eo-dashboard-unload-top') &&
                    card.querySelectorAll('strong').length === 1 &&
                    card.querySelectorAll('small').length === 1 &&
                    !/РАЗГРУЖЕНО/i.test(card.textContent)
                ),
                open: document.querySelector('[data-driver-shell]').classList.contains('is-driver-manual-workspace-open'),
            };
        });
        assert(layout.open, 'Manual workplace did not enter open state.');
        assert(layout.scrollWidth <= layout.clientWidth, 'Manual workplace has horizontal overflow at 412px.');
        assert(layout.nav.top < 915 && layout.nav.bottom <= 915.5, 'Driver bottom navigation is not visible.');
        assert(layout.topbar.top >= 0 && layout.topbar.bottom <= 90, 'Manual top bar opened outside the viewport.');
        assert(layout.heading.top >= layout.topbar.bottom, 'Manual heading is hidden behind or above the top bar.');
        assert(Math.abs(layout.actionRow[0].top - layout.actionRow[1].top) < 2 && Math.abs(layout.actionRow[0].bottom - layout.actionRow[1].bottom) < 2, 'Manual action buttons are not aligned in one top row.');
        assert(Math.abs(layout.actionRow[0].width - layout.actionRow[1].width) < 2, 'Manual action buttons do not have equal widths.');
        assert(layout.actionRow.every((rect) => rect.height >= 64), 'Manual action touch target is smaller than 64px.');
        assert(Math.abs(layout.actionRow[0].height - layout.source.height) < 2, 'Top actions do not keep the source-grid row height.');
        assert(layout.workGrid.rows.length === 3 && Math.abs(layout.workGrid.rows[0] - layout.workGrid.rows[2]) < 2, 'Action and Excavator rows do not keep the same grid-cell height.');
        assert(Math.abs(layout.actionRow[0].top - layout.workGrid.top) < 2, 'Manual actions do not start at the top of the work grid.');
        assert(Math.abs(layout.timer.top - layout.actionRow[0].bottom - layout.actionGap) < 2, 'Trip timer is not directly below the action buttons with the shared gap.');
        assert(Math.abs(layout.source.top - layout.timer.bottom - layout.actionGap) < 2, `Excavator card is not directly below the timer with the shared gap: ${JSON.stringify(layout)}`);
        assert(layout.timer.height >= 48 && Math.abs(layout.timer.left - layout.workGrid.left) < 2 && Math.abs(layout.timer.right - layout.workGrid.right) < 2, 'Trip timer does not span the usable second row.');
        assert(layout.timer.active === 'false' && layout.timer.value === '00:00:00', 'Trip timer must be idle before the first completed dispatch gesture.');
        assert(layout.actionGap >= 8, 'Manual action buttons do not have a safe gap.');
        assert(Math.abs((layout.source.left + layout.source.right) / 2 - 206) < 3, 'Excavator card is not centered below the buttons.');
        assert(layout.source.bottom <= layout.firstTargetTop, 'Excavator card overlaps the unload targets.');
        assert(layout.sourceTopHit, 'The top edge of the Excavator card is intercepted by another control.');
        assert(layout.actionTexts[0].label === 'ОБЫЧНЫЙ РЕЖИМ', 'The left action has the wrong visible label.');
        assert(layout.actionTexts[1].label === 'ТОЧКА РАЗГРУЗКИ', 'The right action has the wrong visible label.');
        assert(layout.actionTexts.every((item) => item.fits), 'Manual action text is clipped at 412px.');
        assert(layout.leanDumpCards, 'Driver dump cards contain text other than the point name and trip count.');

        const driverShot = path.join(outputDir, 'B-driver-shared-workplace-412x915.png');
        await driverPage.screenshot({ path: driverShot });
        report.screenshots.push({ id: 'B', file: path.basename(driverShot), url: driverPage.url() });

        const cdp = await driverContext.newCDPSession(driverPage);
        const sourceRect = await source.boundingBox();
        await dispatchTouch(cdp, 'touchStart', {x: sourceRect.x + sourceRect.width / 2, y: sourceRect.y + 6});
        await dispatchTouch(cdp, 'touchMove', {x: sourceRect.x + sourceRect.width / 2 + 3, y: sourceRect.y + 8});
        await dispatchTouch(cdp, 'touchCancel');
        await driverPage.waitForTimeout(50);
        assert(await driverPage.locator('[data-driver-manual-workspace]').isVisible(), 'Imprecise source touch triggered the return action.');
        assert(await driverPage.locator('[data-driver-point-sheet]').isHidden(), 'Imprecise source touch opened the point action.');
        assert(await driverPage.locator('.truck-drag-preview').count() === 0, 'Cancelled source-edge touch retained a drag preview.');
        report.checks.push('The separated lower source card can be touched near its upper edge without activating either top action.');

        const pointAction = driverPage.locator('[data-driver-manual-point-open]');
        await pointAction.click();
        const pointSheet = driverPage.locator('[data-driver-point-sheet]');
        await pointSheet.waitFor({ state: 'visible' });
        assert((await pointSheet.locator('h2').textContent()).trim() === 'Другая точка разгрузки', 'Manual point action opened the wrong selector mode.');
        assert(await driverPage.locator('[data-driver-manual-workspace]').isVisible(), 'Opening the point selector closed manual mode.');
        const pointSheetShot = path.join(outputDir, 'E-driver-manual-point-selector-412x915.png');
        await driverPage.screenshot({ path: pointSheetShot });
        report.screenshots.push({ id: 'E', file: path.basename(pointSheetShot), url: driverPage.url() });
        const targetIdsBefore = await targets.evaluateAll((nodes) => nodes.map((node) => node.dataset.eoDumpTarget));
        const pointTiles = pointSheet.locator('.driver-unload-tile');
        assert(await pointTiles.count() > 0, 'Canonical Driver point directory is empty in the local fixture.');
        let pointTileIndex = 0;
        for (let index = 0; index < await pointTiles.count(); index += 1) {
            const candidateId = await pointTiles.nth(index).locator('xpath=..').locator('[name="dump_point"]').inputValue();
            if (!targetIdsBefore.includes(candidateId)) {
                pointTileIndex = index;
                break;
            }
        }
        const selectedPointName = await pointTiles.nth(pointTileIndex).getAttribute('data-driver-point-name');
        const selectedPointId = await pointTiles.nth(pointTileIndex).locator('xpath=..').locator('[name="dump_point"]').inputValue();
        await pointTiles.nth(pointTileIndex).click();
        await pointSheet.waitFor({ state: 'hidden' });
        assert(await driverPage.locator(`[data-driver-manual-dump-target][data-eo-dump-target="${selectedPointId}"]`).count() === 1, 'Selected manual point was not available as a drag target.');
        assert((await pointAction.locator('[data-driver-manual-point-hint]').textContent()).trim() === selectedPointName, 'Point action did not retain the selected point name.');
        const fourPointLayout = await targets.evaluateAll((nodes) => nodes.map((node) => {
            const rect = node.getBoundingClientRect();
            const title = node.querySelector('strong');
            const titleRect = title?.getBoundingClientRect();
            return {
                left: rect.left,
                top: rect.top,
                width: rect.width,
                titleFits: !!titleRect && titleRect.left >= rect.left && titleRect.right <= rect.right && titleRect.top >= rect.top && titleRect.bottom <= rect.bottom,
                childCount: node.children.length,
            };
        }));
        assert(fourPointLayout.length === 4, 'The selected one-off point did not produce the four-card layout.');
        assert(Math.max(...fourPointLayout.slice(0, 3).map((item) => item.top)) - Math.min(...fourPointLayout.slice(0, 3).map((item) => item.top)) < 2, 'Four dump points do not keep three columns in the first row at 412px.');
        assert(fourPointLayout[3].top > fourPointLayout[0].top + 4, 'The fourth dump point did not move to the second row.');
        assert(fourPointLayout.every((item) => item.childCount === 1 && item.titleFits), `A four-card dump point contains extra content or clipped title: ${JSON.stringify(fourPointLayout)}`);
        const responsiveType = await driverPage.evaluate(() => {
            const grid = document.querySelector('.driver-manual-workspace .eo-dashboard-unload-grid');
            const title = grid?.querySelector('[data-driver-manual-dump-target] strong');
            const dense = title ? parseFloat(getComputedStyle(title).fontSize) : 0;
            grid?.classList.remove('is-count-4');
            grid?.classList.add('is-count-1');
            const single = title ? parseFloat(getComputedStyle(title).fontSize) : 0;
            grid?.classList.remove('is-count-1');
            grid?.classList.add('is-count-4');
            return {dense, single};
        });
        assert(responsiveType.single >= responsiveType.dense + 8, `Single dump-point text is not materially larger than the four-card text: ${JSON.stringify(responsiveType)}`);
        const selectedPointShot = path.join(outputDir, 'F-driver-manual-selected-point-412x915.png');
        await driverPage.screenshot({ path: selectedPointShot });
        report.screenshots.push({ id: 'F', file: path.basename(selectedPointShot), url: driverPage.url() });
        report.checks.push('The right action opens the canonical Driver point tiles, keeps manual mode open, and exposes the selected point as a drag target without a trip request.');

        const manualFreeBucket = driverPage.locator('[data-driver-manual-free-bucket-open]');
        await manualFreeBucket.waitFor({ state: 'visible' });
        assert(!(await manualFreeBucket.isDisabled()), 'Manual free-bucket button is unexpectedly disabled.');
        await manualFreeBucket.click();
        const freeBucketSheet = driverPage.locator('[data-driver-free-bucket-sheet]');
        await freeBucketSheet.waitFor({ state: 'visible' });
        assert(await driverPage.locator('[data-driver-manual-workspace]').isVisible(), 'Opening free bucket closed manual mode.');
        assert((await freeBucketSheet.locator('h2').textContent()).trim() === 'Под свободный ковш', 'Manual button did not open the canonical Driver free-bucket sheet.');
        const freeBucketShot = path.join(outputDir, 'D-driver-manual-free-bucket-412x915.png');
        await driverPage.screenshot({ path: freeBucketShot });
        report.screenshots.push({ id: 'D', file: path.basename(freeBucketShot), url: driverPage.url() });
        await freeBucketSheet.locator('[data-driver-free-bucket-close]').first().click();
        await freeBucketSheet.waitFor({ state: 'hidden' });
        assert(await driverPage.locator('[data-driver-manual-workspace]').isVisible(), 'Closing free bucket did not return to manual mode.');
        report.checks.push('The exact Excavator free-bucket button opens the canonical Driver selector over manual mode and returns to the same workspace.');

        for (let cycle = 0; cycle < 3; cycle += 1) {
            await driverPage.locator('.driver-manual-workspace__action--return').click();
            assert(await driverPage.locator('[data-driver-manual-workspace]').isHidden(), 'Ordinary-mode action did not close manual mode.');
            await driverPage.locator('[data-driver-manual-open]').click();
            await driverPage.locator('[data-driver-manual-workspace]').waitFor({ state: 'visible' });
            assert(await driverPage.locator('.truck-drag-preview').count() === 0, 'Re-entry retained a stale drag preview.');
        }
        report.checks.push('The Ordinary mode action and three repeated entries work without a stale controller or preview.');

        const start = await center(driverPage.locator('[data-driver-manual-source]').first());
        const finishTarget = targets.nth(1);
        const finish = await center(finishTarget);
        const timerTargetName = await finishTarget.getAttribute('data-eo-dump-name');
        const finishTargetId = await finishTarget.getAttribute('data-eo-dump-target');
        const gestureRequests = [];
        const recordRequest = (request) => gestureRequests.push(`${request.method()} ${request.url()}`);
        driverPage.on('request', recordRequest);
        await dispatchTouch(cdp, 'touchStart', start);
        await dispatchTouch(cdp, 'touchMove', {
            x: start.x + (finish.x - start.x) * 0.45,
            y: start.y + (finish.y - start.y) * 0.45,
        });
        await driverPage.waitForTimeout(70);
        await dispatchTouch(cdp, 'touchMove', finish);
        await driverPage.waitForTimeout(90);
        assert(await driverPage.locator('.truck-drag-preview').count() === 1, 'Shared drag preview was not created.');
        assert(await driverPage.locator('.eo-truck-comet').count() === 1, 'Shared comet trail was not created.');
        assert(await finishTarget.evaluate((node) => node.classList.contains('is-drop-ready')), 'Target highlight did not follow the gesture.');
        const driverDragVisual = await dragVisualSnapshot(
            driverPage,
            '[data-driver-manual-source]',
            `[data-driver-manual-dump-target][data-eo-dump-target="${finishTargetId}"]`,
        );
        report.dragVisuals.driver = driverDragVisual;
        assert(driverDragVisual.innerShellActive, 'Driver gesture did not activate the inner shared Excavator shell.');
        assert(driverDragVisual.previewBorderWidth === '2px' && driverDragVisual.previewHasGlint && driverDragVisual.previewBoxShadow !== 'none', `Driver preview is missing the canonical bright held-card effect: ${JSON.stringify(driverDragVisual)}`);
        assert(driverDragVisual.cometPosition === 'fixed' && driverDragVisual.cometDisplay !== 'none' && driverDragVisual.cometOpacity > 0 && driverDragVisual.cometWidth >= 411 && driverDragVisual.cometHeight >= 914 && driverDragVisual.cometPath.length > 8, `Driver comet is not visibly full-screen: ${JSON.stringify(driverDragVisual)}`);
        assert(driverDragVisual.targetToSiblingHeight >= 1.35, `Driver unload targets do not perform the canonical counter-motion: ${JSON.stringify(driverDragVisual)}`);
        assert(driverDragVisual.allTargetTextFits, `Driver dump-point labels are clipped during counter-motion: ${JSON.stringify(driverDragVisual)}`);
        assert(driverDragVisual.sourceImageAspect >= 1.3, `Driver Excavator image was distorted into a square: ${JSON.stringify(driverDragVisual)}`);
        assert(/rgba?\(255,\s*21[0-9],\s*[0-7]?\d/.test(driverDragVisual.targetBorderColor), `Ready target did not inherit the canonical gold highlight: ${driverDragVisual.targetBorderColor}`);
        if (excavatorDragVisual) {
            assert(Math.abs(driverDragVisual.sourceAspect - excavatorDragVisual.sourceAspect) <= 0.06, `Driver source-card proportions differ from Excavator: driver=${driverDragVisual.sourceAspect}, excavator=${excavatorDragVisual.sourceAspect}`);
            assert(driverDragVisual.previewBorderWidth === excavatorDragVisual.previewBorderWidth, 'Driver held-card border thickness differs from Excavator.');
            assert(Math.abs(driverDragVisual.targetToSiblingHeight - excavatorDragVisual.targetToSiblingHeight) <= 0.12, `Driver target counter-motion differs from Excavator: driver=${driverDragVisual.targetToSiblingHeight}, excavator=${excavatorDragVisual.targetToSiblingHeight}`);
        }
        const dragShot = path.join(outputDir, 'C-driver-shared-drag-412x915.png');
        await driverPage.screenshot({ path: dragShot });
        report.screenshots.push({ id: 'C', file: path.basename(dragShot), url: driverPage.url() });
        await dispatchTouch(cdp, 'touchEnd');
        await driverPage.waitForTimeout(1600);
        driverPage.off('request', recordRequest);
        report.gestureRequests = gestureRequests;
        assert(await driverPage.locator('.truck-drag-preview').count() === 0, 'Drag preview remained after drop.');
        assert(await driverPage.locator('.eo-truck-comet').count() === 0, 'Comet remained after drop.');
        const resultText = await driverPage.locator('[data-driver-manual-result]').textContent();
        const manualError = await driverPage.locator('[data-driver-manual-workspace]').getAttribute('data-driver-manual-last-error');
        assert(
            /Рейс №9001/.test(resultText) && /в пути/i.test(resultText),
            `Durably queued gesture did not receive the isolated manual-trip acknowledgement: ${JSON.stringify(resultText)}; synced=${syncedManualEvents.length}; error=${manualError}.`
        );
        const mutationRequests = gestureRequests.filter((entry) => !entry.startsWith('GET '));
        assert(mutationRequests.filter((entry) => /\/offline-events\/sync\//.test(entry)).length === 1, `Gesture did not use exactly one common offline-sync request: ${mutationRequests.join(', ')}`);
        assert(!mutationRequests.some((entry) => /\/excavator\/.*(?:load|trip)/.test(entry)), `Driver gesture called an Excavator mutation endpoint: ${mutationRequests.join(', ')}`);
        assert(syncedManualEvents.length === 1 && syncedManualEvents[0].event_type === 'driver.trip.loaded', 'Gesture did not send one Driver-only manual-load event.');
        assert(syncedManualEvents[0].local_trip_id === syncedManualEvents[0].event_id, 'Manual trip lost its stable local identity.');
        await driverPage.waitForTimeout(1100);
        const timerAfterDrop = await driverPage.locator('[data-driver-manual-trip-timer]').evaluate((node) => ({
            active: node.dataset.driverManualTimerActive,
            pointName: node.dataset.driverManualTimerPointName,
            label: node.querySelector('[data-driver-manual-trip-timer-label]')?.textContent.trim(),
            value: node.querySelector('[data-driver-manual-trip-timer-value]')?.textContent.trim(),
        }));
        assert(timerAfterDrop.active === 'true', 'Trip timer did not start after the completed dispatch gesture.');
        assert(timerAfterDrop.pointName === timerTargetName && timerAfterDrop.label.includes(timerTargetName), 'Trip timer is not tied to the selected destination.');
        assert(timerAfterDrop.value !== '00:00:00', 'Trip timer did not advance after one second.');
        await driverPage.waitForTimeout(1600);
        const timerShot = path.join(outputDir, 'H-driver-trip-timer-active-412x915.png');
        await driverPage.screenshot({ path: timerShot });
        report.screenshots.push({ id: 'H', file: path.basename(timerShot), url: driverPage.url() });
        assert(!(await driverPage.locator('[data-driver-manual-source]').isDisabled()), 'Confirmed manual mark incorrectly blocked the next manual swipe.');
        report.checks.push('Completed Driver gesture used the shared preview, comet and highlight, persisted one Driver-only event through the common outbox, kept the source reusable, and continued the destination timer.');

        const secondStart = await center(driverPage.locator('[data-driver-manual-source]').first());
        const secondFinish = await center(targets.nth(0));
        const secondPointName = await targets.nth(0).getAttribute('data-eo-dump-name');
        await dispatchTouch(cdp, 'touchStart', secondStart);
        await dispatchTouch(cdp, 'touchMove', {
            x: secondStart.x + (secondFinish.x - secondStart.x) * 0.5,
            y: secondStart.y + (secondFinish.y - secondStart.y) * 0.5,
        });
        await dispatchTouch(cdp, 'touchMove', secondFinish);
        await dispatchTouch(cdp, 'touchEnd');
        await driverPage.waitForTimeout(1600);
        assert(syncedManualEvents.length === 2, 'A second manual swipe was blocked or duplicated.');
        assert(syncedManualEvents[1].depends_on.includes(syncedManualEvents[0].event_id), 'The next manual cycle is not ordered after the previous durable mark.');
        const secondTimer = await driverPage.locator('[data-driver-manual-trip-timer]').evaluate((node) => ({
            pointName: node.dataset.driverManualTimerPointName,
            active: node.dataset.driverManualTimerActive,
        }));
        assert(secondTimer.active === 'true' && secondTimer.pointName === secondPointName, 'The second manual swipe did not start the next destination timer.');
        assert(!(await driverPage.locator('[data-driver-manual-source]').isDisabled()), 'The second manual cycle again blocked the source card.');
        report.checks.push('A second swipe works without a separate unload confirmation, depends on the previous durable event, and restarts the timer for its own destination.');

        await driverPage.waitForTimeout(900);

        await driverPage.waitForTimeout(100);
        const cancelStart = await center(driverPage.locator('[data-driver-manual-source]').first());
        await dispatchTouch(cdp, 'touchStart', cancelStart);
        await dispatchTouch(cdp, 'touchMove', { x: cancelStart.x + 36, y: cancelStart.y + 18 });
        await driverPage.waitForTimeout(50);
        await dispatchTouch(cdp, 'touchCancel');
        await driverPage.waitForTimeout(50);
        assert(await driverPage.locator('.truck-drag-preview').count() === 0, 'Cancelled gesture retained a preview.');
        assert(await driverPage.locator('.eo-truck-comet').count() === 0, 'Cancelled gesture retained a comet.');
        report.checks.push('Pointer cancellation clears the shared drag state.');

        await driverPage.locator('[data-driver-tab-open="shift"]').click();
        assert(await driverPage.locator('[data-driver-manual-workspace]').isHidden(), 'Bottom navigation did not close manual mode.');
        assert(await driverPage.locator('[data-driver-tab-panel="shift"]').evaluate((node) => node.classList.contains('is-active')), 'Bottom navigation did not switch tabs.');
        report.checks.push('Existing Driver bottom navigation remains operational from manual mode.');

        await driverPage.setViewportSize({ width: 360, height: 640 });
        await driverPage.locator('[data-driver-tab-open="work"]').click();
        await driverPage.evaluate(() => {
            const control = document.querySelector('[data-driver-manual-open]');
            window.DriverManualExcavatorWorkspace.open(control);
        });
        await driverPage.locator('[data-driver-manual-workspace]').waitFor({ state: 'visible' });
        const smallLayout = await driverPage.evaluate(() => {
            const sourceRect = document.querySelector('[data-driver-manual-source]')?.getBoundingClientRect();
            const timerRect = document.querySelector('[data-driver-manual-trip-timer]')?.getBoundingClientRect();
            const actionNodes = Array.from(document.querySelectorAll('.driver-manual-workspace__action'));
            const actionRects = actionNodes.map((node) => node.getBoundingClientRect());
            const targetRects = Array.from(document.querySelectorAll('[data-driver-manual-dump-target]')).map((node) => node.getBoundingClientRect());
            const nav = document.querySelector('[data-driver-bottom-nav]').getBoundingClientRect();
            const workGrid = document.querySelector('.driver-manual-workspace__source-layout');
            const workGridRect = workGrid.getBoundingClientRect();
            const workGridRows = getComputedStyle(workGrid).gridTemplateRows.split(' ').map(Number.parseFloat);
            return {
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                sourceTop: sourceRect ? sourceRect.top : 0,
                sourceCenter: sourceRect ? (sourceRect.left + sourceRect.right) / 2 : 0,
                sourceBottom: sourceRect ? sourceRect.bottom : 0,
                sourceWidth: sourceRect ? sourceRect.width : 0,
                sourceHeight: sourceRect ? sourceRect.height : 0,
                actionBottom: Math.max(...actionRects.map((rect) => rect.bottom)),
                actionTop: Math.min(...actionRects.map((rect) => rect.top)),
                actionWidthDelta: Math.abs(actionRects[0].width - actionRects[1].width),
                actionHeightMin: Math.min(...actionRects.map((rect) => rect.height)),
                actionGap: actionRects[1].left - actionRects[0].right,
                workGridTop: workGridRect.top,
                workGridBottom: workGridRect.bottom,
                workGridRows,
                timerTop: timerRect ? timerRect.top : 0,
                timerBottom: timerRect ? timerRect.bottom : 0,
                timerHeight: timerRect ? timerRect.height : 0,
                actionTextFits: actionNodes.every((node) => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight),
                firstTargetTop: targetRects[0]?.top || 0,
                targetBottom: Math.max(...targetRects.map((rect) => rect.bottom)),
                targetColumnCount: new Set(targetRects.map((rect) => Math.round(rect.left))).size,
                targetTextFits: Array.from(document.querySelectorAll('[data-driver-manual-dump-target]')).every((card) => {
                    const cardRect = card.getBoundingClientRect();
                    const titleRect = card.querySelector('strong')?.getBoundingClientRect();
                    return !!titleRect && titleRect.left >= cardRect.left && titleRect.right <= cardRect.right && titleRect.top >= cardRect.top && titleRect.bottom <= cardRect.bottom;
                }),
                navTop: nav.top,
            };
        });
        assert(smallLayout.scrollWidth <= smallLayout.clientWidth, 'Manual workplace has horizontal overflow at 360px.');
        assert(smallLayout.actionWidthDelta < 2 && smallLayout.actionHeightMin >= 64, 'Top action buttons are not equal usable touch targets at 360px.');
        assert(Math.abs(smallLayout.actionHeightMin - smallLayout.sourceHeight) < 2, 'Top actions are vertically compressed at 360px.');
        assert(smallLayout.workGridRows.length === 3 && Math.abs(smallLayout.workGridRows[0] - smallLayout.workGridRows[2]) < 2, 'The 360px action and source rows do not keep the same height.');
        assert(Math.abs(smallLayout.actionTop - smallLayout.workGridTop) < 2, 'The 360px actions do not start at the top of the work grid.');
        assert(smallLayout.timerHeight >= 48, 'Trip timer is too short at 360px.');
        assert(smallLayout.actionGap >= 8, 'Top action buttons lose their safe gap at 360px.');
        assert(Math.abs(smallLayout.timerTop - smallLayout.actionBottom - smallLayout.actionGap) < 2, 'The 360px timer does not use the shared gap below the actions.');
        assert(Math.abs(smallLayout.sourceTop - smallLayout.timerBottom - smallLayout.actionGap) < 2, 'The 360px Excavator card does not use the shared gap below the timer.');
        assert(Math.abs(smallLayout.sourceCenter - 180) < 3, 'Excavator card is not centered at 360px.');
        assert(smallLayout.actionTextFits, 'Manual action text is clipped at 360px.');
        assert(smallLayout.sourceBottom <= smallLayout.firstTargetTop, 'Source and dump targets overlap at 360x640.');
        assert(smallLayout.targetBottom <= smallLayout.navTop, 'Dump targets overlap bottom navigation at 360x640.');
        assert(smallLayout.targetColumnCount === 3, 'Dump points do not preserve the three-column matrix at 360px.');
        assert(smallLayout.targetTextFits, 'Adaptive dump-point names are clipped at 360px.');
        const smallShot = path.join(outputDir, 'G-driver-manual-layout-360x640.png');
        await driverPage.screenshot({path: smallShot});
        report.screenshots.push({id: 'G', file: path.basename(smallShot), url: driverPage.url()});
        report.checks.push('360x640 layout keeps actions, timer and centered source stacked with one shared gap, plus a three-column dump matrix and separated bottom navigation.');

        await fs.promises.writeFile(path.join(outputDir, 'browser-qa-report.json'), JSON.stringify(report, null, 2), 'utf8');
        assert(report.consoleErrors.length === 0, `Console errors: ${report.consoleErrors.join(' | ')}`);
        assert(report.pageErrors.length === 0, `Page errors: ${report.pageErrors.join(' | ')}`);
        const relevantFailedRequests = report.failedRequests.filter((entry) => !entry.includes('/session-heartbeat/'));
        assert(relevantFailedRequests.length === 0, `Failed requests: ${relevantFailedRequests.join(' | ')}`);
        console.log(JSON.stringify(report, null, 2));
    } finally {
        await browser.close();
    }
})().catch((error) => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
