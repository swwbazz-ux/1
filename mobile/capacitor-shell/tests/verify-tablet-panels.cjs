/* Real adapter + role-readonly + Dispatcher DnD/canvas/rack layout. All requests use local bytes.
   COPPER_PLAYWRIGHT_MODULE may point to a bundled Playwright package directory. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { chromium } = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const repo = path.resolve(__dirname, '../../..');
  const output = path.resolve(process.argv[2] || path.join(repo, 'qa_artifacts/tablet-panels'));
  fs.mkdirSync(output, { recursive: true });
  const script = fs.readFileSync(path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.js'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.css'), 'utf8');
  const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/mining-master-tablet-panels.html'), 'utf8');
  const java = fs.readFileSync(path.join(__dirname, '../android/app/src/main/java/ru/copperresources/mobile/StartupLoadingOverlay.java'), 'utf8');
  const probe = vm.runInNewContext(java.match(/private static final String READINESS_PROBE\s*=([\s\S]*?);\r?\n\r?\n/)[1], { STANDALONE_READINESS_EXTENSION: '' });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1333, height: 800 }, hasTouch: true });
  page.setDefaultTimeout(5000);
  const errors = [], requests = [], checks = [], keyboardCases = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'GET' && url.origin === 'https://dispatcher.driverform.ru') {
      if (url.pathname === '/dispatcher/control/') return route.fulfill({ contentType: 'text/html', body: fixture.replace('</head>', `<style>${css}</style></head>`) });
      if (url.pathname === '/tablet-adapter.js') return route.fulfill({ contentType: 'application/javascript', body: script });
      if (['/static/img/equipment/excavator-gray.png', '/static/img/equipment/truck-gray.png'].includes(url.pathname)) return route.fulfill({ path: path.join(repo, 'СИСТЕМА_MVP/backend', url.pathname.slice(1)) });
      if (['/static/js/role-readonly.js', '/static/js/dispatcher-board-dnd-v1.js', '/static/js/dispatcher-canvas-v1.js', '/static/js/dispatcher-board-layout-v1.js', '/static/js/dispatcher-complex-truck-racks-v1.js'].includes(url.pathname)) return route.fulfill({ contentType: 'application/javascript', body: fs.readFileSync(path.join(repo, 'СИСТЕМА_MVP/backend', url.pathname.slice(1)), 'utf8') });
    }
    requests.push({ method: route.request().method(), url: url.pathname }); return route.abort();
  });
  const handle = side => page.locator(`.cr-mm-panel-handle[data-cr-panel-side="${side}"]`);
  const open = () => page.locator('[data-cr-panel-state="open"]').getAttribute('data-cr-panel');
  async function tick(ms) { await page.clock.runFor(ms); }
  try {
    await page.goto('https://dispatcher.driverform.ru/dispatcher/control/');
    await page.waitForSelector('html.cr-mm-panels');
    await page.waitForFunction(probe => { const p=eval(probe).split('|');return p[0]==='complete'&&Number(p[2])>=8&&Number(p[3])>=240; },probe);
    const quiet = await page.evaluate(async probe => {
      const records = [], samples = [];
      const observer = new MutationObserver(items => records.push(...items.map(r => ({ attr: r.attributeName, same: r.oldValue === r.target.getAttribute(r.attributeName) }))));
      observer.observe(document.documentElement, { subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ['class', 'style', 'hidden', 'disabled'] });
      for (let i = 0; i < 8; i++) CopperMiningMasterTablet.init();
      const start = performance.now();
      while (performance.now() - start < 800) { samples.push(eval(probe).split('|')); await new Promise(requestAnimationFrame); }
      observer.disconnect(); return { records, ready: samples.some(p => Number(p[2]) >= 8 && Number(p[3]) >= 240 && p[0] === 'complete') };
    }, probe);
    assert.equal(quiet.ready, true); assert.equal(quiet.records.length, 0); checks.push('native readiness and mutation-free repeated init');
    const hitZones = await page.evaluate(() => [...document.querySelectorAll('.cr-mm-panel-handle')].map(n => {
      const r=n.getBoundingClientRect(), side=n.dataset.crPanelSide, horizontal=side==='top'||side==='bottom';
      const base=horizontal?(side==='top'?r.top:r.bottom):(side==='left'?r.left:r.right), sign=side==='top'||side==='left'?1:-1;
      const hit=depth=>document.elementFromPoint(horizontal?r.x+r.width/2:base+sign*depth,horizontal?base+sign*depth:r.y+r.height/2)?.closest('.cr-mm-panel-handle');
      return {side,inside:hit(32)===n,outside:hit(40)===null};
    }));
    assert.equal(hitZones.every(z=>z.inside&&z.outside),true); checks.push('36px local handle hit zones leave the surrounding field available');
    await page.clock.install();
    await page.evaluate(() => { window.panelFrames=[];window.panelOverlap=[];function frame(){const field=document.querySelector('.dispatcher-complexes')?.getBoundingClientRect(),visible=[...document.querySelectorAll('[data-cr-panel]')].filter(n=>getComputedStyle(n).visibility==='visible');panelFrames.push(visible.length);if(field)visible.forEach(n=>{const r=n.getBoundingClientRect();panelOverlap.push(Math.max(0,Math.min(r.right,field.right)-Math.max(r.left,field.left))*Math.max(0,Math.min(r.bottom,field.bottom)-Math.max(r.top,field.top)));});requestAnimationFrame(frame);}requestAnimationFrame(frame); });
    await page.evaluate(()=>{const source=document.querySelector('[data-dispatcher-drop="complex"]');source.dataset.dispatcherDrag='complex';source.dataset.equipmentName='К-2';source.dataset.placementZone='active';source.draggable=true;fixtureBind();});
    for(const side of ['left','right']){
      const source=await page.locator('[data-dispatcher-drag="complex"] h2').boundingBox();
      await page.mouse.move(source.x+source.width/2,source.y+source.height/2);await page.mouse.down();
      await page.mouse.move(side==='left'?5:1328,400,{steps:15});await tick(500);
      assert.equal(await open(),side);assert.equal(await page.locator('html').getAttribute('data-cr-panel-native-drag'),'true');
      await page.clock.fastForward(15000);assert.equal(await open(),side);assert.equal(await page.evaluate(()=>fixturePosts.length),0);
      await page.keyboard.press('Escape');await page.mouse.up();await tick(500);
      assert.equal(await page.locator('html').getAttribute('data-cr-panel-native-drag'),null);assert.equal(await page.evaluate(()=>fixturePosts.length),0);
    }
    checks.push('real native drag reveals either edge, pauses idle, and cancellation submits nothing');
    /* A short, populated rack reproduces the real four-column cut-off. Merely
       shrinking the card does not resize the rack's intrinsic width. */
    await page.evaluate(()=>{
      const card=document.querySelector('[data-equipment-id="2"]'),sample=document.querySelector('.complex-truck-tile'),rack=document.createElement('div');
      rack.className='complex-assigned-trucks';rack.dataset.truckNeed='7';
      for(let i=0;i<7;i++){const tile=sample.cloneNode(true);tile.hidden=false;tile.removeAttribute('data-drag-bound');tile.dataset.equipmentId=String(91+i);tile.dataset.equipmentName=String(91+i);tile.dataset.haulAssignmentStateId=String(101+i);tile.querySelector('strong').textContent=String(91+i);rack.appendChild(tile);}
      card.appendChild(rack);const grid=document.querySelector('.dispatcher-zone-grid');grid.style.gridAutoRows='220px';grid.style.alignContent='start';fixtureBind();
    });await tick(450);
    const geometry = () => page.evaluate(() => [...document.querySelectorAll('.dispatcher-complex-card,.complex-assigned-trucks,.complex-truck-tile')].map(n => { const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height]; }));
    async function verifyKeyboard(width, fullHeight, imeHeight) {
      /* The fake clock advances idle/state timers, not compositor animation.
         Measure the actual final toolbar transform, not its entry frame. */
      const settleToolbar = () => page.locator('#cr-mm-toolbar').evaluate(node => Promise.all(node.getAnimations().map(animation => animation.finished.catch(() => {}))));
      const capture = () => page.evaluate(() => {
        const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
        const clipped = [...document.querySelectorAll('.complex-truck-tile:not([hidden])')].filter(tile => {
          const r = tile.getBoundingClientRect();
          return ['.dispatcher-complex-card', '.complex-assigned-trucks'].some(selector => {
            const box = tile.closest(selector).getBoundingClientRect();
            return r.left < box.left - .1 || r.right > box.right + .1 || r.top < box.top - .1 || r.bottom > box.bottom + .1;
          });
        }).map(tile => tile.dataset.equipmentId);
        return { field: rect('.dispatcher-complexes'), toolbar: rect('#cr-mm-toolbar'), canvas: rect('.dispatcher-canvas'),
          handle: rect('.cr-mm-panel-handle[data-cr-panel-side="bottom"]'),
          scale: document.documentElement.style.getPropertyValue('--cr-mm-scale'),
          focused: document.activeElement === window.imeSearchInput, query: window.imeSearchInput.value, clipped };
      });
      const showIme = async mechanism => {
        if (mechanism === 'innerHeight+visualViewport') await page.setViewportSize({ width, height: imeHeight });
        else await page.evaluate(height => {
          Object.defineProperty(visualViewport, 'height', { configurable: true, get: () => height });
          visualViewport.dispatchEvent(new Event('resize'));
        }, imeHeight);
        await tick(350); await settleToolbar();
      };
      const hideIme = async mechanism => {
        if (mechanism === 'innerHeight+visualViewport') await page.setViewportSize({ width, height: fullHeight });
        else await page.evaluate(() => { delete visualViewport.height; visualViewport.dispatchEvent(new Event('resize')); });
        await tick(350); await settleToolbar();
      };
      for (const mechanism of ['innerHeight+visualViewport', 'visualViewport-only']) {
        await page.locator('[data-cr-mm-search]').focus();
        await page.evaluate(() => { window.imeSearchInput = document.querySelector('[data-cr-mm-search]'); });
        await settleToolbar();
        const beforeIme = await capture(), beforeGeometry = await geometry();
        await showIme(mechanism);
        const shown = await capture();
        assert.ok(shown.field.bottom <= shown.toolbar.top + .1, JSON.stringify({ width, mechanism, shown }));
        assert.ok(Math.abs(shown.field.bottom - shown.toolbar.top) < .1, JSON.stringify({ width, mechanism, shown }));
        assert.ok(Math.abs(shown.toolbar.bottom - imeHeight) < .1);
        assert.ok(Math.abs(shown.handle.bottom - shown.toolbar.top) < .1);
        assert.equal(shown.toolbar.height, 64); assert.equal(shown.scale, beforeIme.scale); assert.deepEqual(shown.canvas, beforeIme.canvas);
        assert.equal(shown.focused, true); assert.equal(shown.query, beforeIme.query); assert.deepEqual(shown.clipped, []);
        await page.clock.fastForward(12000); assert.equal(await open(), 'bottom'); assert.equal((await capture()).focused, true);
        await page.evaluate(() => {
          window.imeMutations = [];
          window.imeObserver = new MutationObserver(records => imeMutations.push(...records.map(record => record.attributeName || record.type)));
          imeObserver.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'disabled'] });
          for (let i = 0; i < 8; i++) CopperMiningMasterTablet.init();
        }); await tick(300);
        assert.deepEqual(await page.evaluate(() => { imeObserver.disconnect(); return imeMutations; }), []);
        await page.screenshot({ path: path.join(output, 'keyboard-' + mechanism + '-' + width + '.png') });
        await hideIme(mechanism);
        const restored = await capture();
        assert.deepEqual(restored.field, beforeIme.field); assert.deepEqual(restored.toolbar, beforeIme.toolbar);
        assert.deepEqual(await geometry(), beforeGeometry); assert.equal(restored.focused, true); assert.equal(restored.query, beforeIme.query);
        checks.push(width + ': ' + mechanism + ' reserves the visible field, keeps tiles/focus/scale, pauses 12s, restores and stays mutation-quiet');

        await showIme(mechanism);
        await handle('bottom').click(); await tick(250);
        assert.equal((await capture()).focused, false); assert.equal(await open(), 'bottom');
        assert.equal((await capture()).query, beforeIme.query);
        await hideIme(mechanism);
        assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0);
        assert.equal((await capture()).query, beforeIme.query);
        assert.ok(Math.abs((await capture()).field.bottom - fullHeight) < .1);
        checks.push(width + ': explicit close finishes ' + mechanism + ' input, waits for IME exit and keeps the query');
        keyboardCases.push({ width, mechanism, before: beforeIme, shown, restored });
        await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-search]').focus();
      }
    }
    const closedField = await geometry();
    for (const side of ['top', 'left', 'right', 'bottom']) {
      await handle(side).click(); await tick(450); assert.equal(await open(), side);
      const value=await page.evaluate(side=>{
        const panel=document.querySelector('[data-cr-panel="'+side+'"]'),r=panel.getBoundingClientRect(),f=document.querySelector('.dispatcher-complexes').getBoundingClientRect();
        const main=document.querySelector('.complex-truck-tile').getBoundingClientRect(),tile=panel.querySelector('[data-garage-item]')?.getBoundingClientRect(),grid=panel.querySelector('.dispatcher-excavators,.dispatcher-trucks');
        return {side,p:[r.x,r.y,r.width,r.height],f:[f.x,f.y,f.width,f.height],reserved:side==='top'?f.top>=r.bottom-.1:side==='bottom'?f.bottom<=r.top+.1:side==='left'?f.left>=r.right-.1:f.right<=r.left+.1,columns:grid&&getComputedStyle(grid).gridTemplateColumns,tile:tile&&[tile.width,tile.height],main:[main.width,main.height]};
      },side);
      assert.equal(value.reserved,true,JSON.stringify(value));
      if(value.tile){assert.ok(Math.abs(value.p[1])<1&&Math.abs(value.p[3]-800)<1);assert.equal(value.columns.trim().split(/\s+/).length,1);assert.ok(value.tile.every((n,i)=>Math.abs(n-value.main[i])<.01));}
      const clipped=await page.evaluate(()=>[...document.querySelectorAll('.complex-truck-tile:not([hidden])')].filter(tile=>{const r=tile.getBoundingClientRect(),c=tile.closest('.dispatcher-complex-card').getBoundingClientRect();return r.left<c.left-.1||r.right>c.right+.1||r.top<c.top-.1||r.bottom>c.bottom+.1;}).map(n=>({id:n.dataset.equipmentId,box:n.getBoundingClientRect().toJSON(),card:n.closest('.dispatcher-complex-card').getBoundingClientRect().toJSON(),rack:n.parentElement.style.cssText})));
      assert.deepEqual(clipped,[],side+' must reflow every visible truck inside its own card');
      if(side==='top'){
        const beforeHeader=await geometry();
        await page.evaluate(()=>{const header=document.querySelector('.dispatcher-topbar');header.replaceWith(header.cloneNode(true));fixtureBind();});await tick(350);
        assert.equal(await open(),'top');assert.deepEqual(await geometry(),beforeHeader);
        checks.push('an open header survives its server fragment replacement with the same reserved track');
      }
      await page.screenshot({path:path.join(output,'reserved-'+side+'-1333.png')});
      const openField=await geometry();await handle(side).click();await tick(100);assert.deepEqual(await geometry(),openField);
      await tick(300);assert.deepEqual(await geometry(),closedField);
    }
    assert.equal(await page.evaluate(() => Math.max(...panelFrames)), 1);assert.ok(await page.evaluate(()=>Math.max(...panelOverlap))<2);
    checks.push('all four panels reserve space before entry and throughout closing; no frame covers the field');
    checks.push('side garages remain full-height, one vertical column, with exact live field tile dimensions');
    checks.push('dense racks recalculate columns through the real control resize hook without clipping visible trucks');
    assert.equal(await page.locator('.cr-mm-panel-backdrop').count(),0);checks.push('no dimming or pointer-intercepting backdrop remains');
    const fixedField = await geometry();
    await page.evaluate(()=>{window.dispatchEvent(new Event('resize'));visualViewport.dispatchEvent(new Event('resize'));});await tick(350);
    assert.deepEqual(await geometry(),fixedField);
    await page.setViewportSize({width:1333,height:776});await tick(350);assert.deepEqual(await geometry(),fixedField);
    await page.setViewportSize({width:1333,height:800});await tick(350);assert.deepEqual(await geometry(),fixedField);
    checks.push('real canvas/rack runtimes cannot reflow the field on resize or transient system bars');
    await page.evaluate(()=>document.querySelector('.dispatcher-board').classList.add('is-excavator-garage-empty','is-truck-garage-empty'));
    for(const side of ['left','right']){await handle(side).click();await tick(400);await page.locator('.dispatcher-'+side).waitFor({state:'visible'});}
    await page.evaluate(()=>document.querySelector('.dispatcher-board').classList.remove('is-excavator-garage-empty','is-truck-garage-empty'));
    checks.push('empty garage classes retain visible original drop targets');
    await handle('left').click(); await tick(250); await page.clock.fastForward(9000);
    await page.locator('.dispatcher-excavators').dispatchEvent('scroll'); await page.clock.fastForward(9000); assert.equal(await open(), 'left');
    const tile = await page.locator('.dispatcher-left [data-garage-item]').boundingBox();
    await page.mouse.move(tile.x + tile.width / 2, tile.y + tile.height / 2); await page.mouse.down();
    await page.clock.fastForward(30000); assert.equal(await open(), 'left'); await page.mouse.up();
    await page.clock.fastForward(9500); assert.equal(await open(), 'left'); checks.push('scroll resets idle and held pointer pauses with a fresh interval');
    await handle('bottom').click(); await tick(250);
    await page.locator('#cr-mm-toolbar').dispatchEvent('wheel');
    await page.clock.fastForward(9500); assert.equal(await open(), 'bottom'); await tick(1000);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0); checks.push('ten-second idle close');
    await handle('bottom').click(); await tick(220); await page.locator('[data-cr-mm-search]').fill('58');
    await page.clock.fastForward(30000); assert.equal(await open(), 'bottom');
    const before = await page.locator('[data-cr-mm-search]').inputValue();
    await page.evaluate(() => { window.beforeSearch=document.querySelector('[data-cr-mm-search]');const b=document.querySelector('.dispatcher-board');const copy=b.cloneNode(true);copy.querySelectorAll('[data-drag-bound],[data-dispatcher-drop-bound]').forEach(n=>{n.removeAttribute('data-drag-bound');n.removeAttribute('data-dispatcher-drop-bound')});b.replaceWith(copy);fixtureBind(); });
    await tick(300);
    assert.equal(await page.evaluate(() => document.activeElement===beforeSearch && document.querySelector('[data-cr-mm-search]')===beforeSearch), true);
    assert.equal(await open(), 'bottom'); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before);
    assert.equal(await page.locator('.cr-mm-panel-backdrop').count(), 0); checks.push('focused search survives a background board fragment');
    await verifyKeyboard(1333, 800, 480);
    await page.setViewportSize({width:1333,height:480}); await tick(250); await page.clock.fastForward(15000);
    assert.equal(await open(), 'bottom');
    await page.locator('[data-cr-mm-search]').press('Enter'); await tick(250); assert.equal(await open(), 'bottom');
    await page.setViewportSize({width:1333,height:800}); await tick(400);
    checks.push('keyboard resize preserves search and defers panel switching until input finishes');
    assert.equal(await open(), 'right'); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before); checks.push('search focus pause and Enter reveal without clearing');
    await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-search]').focus();
    await handle('left').click(); await tick(250); assert.equal(await open(), 'left'); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before);
    await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-search]').focus();
    await handle('bottom').click(); await tick(250);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before); checks.push('manual switch and explicit close finish input without losing query');
    await handle('left').click();await tick(450);
    const countBefore=await page.evaluate(()=>fixtureDetails);await page.locator('.complex-truck-tile[data-equipment-id="91"]').click();
    assert.equal(await page.evaluate(()=>fixtureDetails),countBefore+1);assert.equal(await open(),'left');
    checks.push('ordinary field clicks reach the existing detail handler while a garage stays open');
    await handle('right').click(); await tick(250);
    const beforeGarageDetails=await page.evaluate(()=>fixtureDetails);
    await page.locator('.dispatcher-right [data-garage-item]').click(); assert.equal(await page.evaluate(() => fixtureDetails), beforeGarageDetails+1);
    await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-action="mode"]').click();
    await tick(250);assert.equal(await page.locator('[data-cr-panel-state="open"]').count(),0);checks.push('mode action closes the bottom panel so the next field tap selects a source');
    await handle('right').click(); await tick(250); await page.locator('.dispatcher-right [data-garage-item]').click(); await tick(250);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0);
    await page.locator('[data-dispatcher-drop="complex"][data-equipment-id="2"]').click();
    const posts = await page.evaluate(() => fixturePosts); assert.equal(posts.length, 1); assert.equal(posts[0].url, '/dispatcher/control/truck/assign/'); assert.equal(posts[0].payload.expected_assignment_state_id, 10);
    checks.push('normal detail click and real existing DnD assignment after garage closes');
    await handle('top').click(); await tick(250); await page.locator('#fixture-dialog').click();
    await page.clock.fastForward(30000); assert.equal(await open(), 'top');
    await page.locator('#fixture-dialog-close').click(); await page.clock.fastForward(9500); assert.equal(await open(), 'top'); await tick(750);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0); checks.push('modal pause resumes with full interval');
    await handle('left').click(); await tick(250);
    await page.evaluate(() => { const b=document.querySelector('.dispatcher-board');const copy=b.cloneNode(true);copy.querySelectorAll('[data-drag-bound],[data-dispatcher-drop-bound]').forEach(n=>{n.removeAttribute('data-drag-bound');n.removeAttribute('data-dispatcher-drop-bound')});b.replaceWith(copy);fixtureBind(); });
    await tick(300); assert.equal(await page.locator('.cr-mm-panel-handle').count(), 4); assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0);
    await page.evaluate(() => { document.body.dataset.appShellVersion='unknown'; }); await tick(100);
    assert.equal(await page.locator('.cr-mm-panel-handle').count(), 0); assert.equal(await page.locator('[data-cr-panel]').count(), 0);
    await page.evaluate(() => { document.body.dataset.appShellVersion='dispatcher-desktop-shell-v166'; }); await tick(300);
    assert.equal(await page.locator('.cr-mm-panel-handle').count(), 4); await page.clock.fastForward(30000);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0); checks.push('fragment replacement and unknown-shell cleanup with no late timers');
    await page.setViewportSize({width:1024,height:600});await tick(350);
    assert.equal(await page.locator('.dispatcher-canvas').getAttribute('data-dispatcher-canvas'),'on');
    const narrowColumns=await page.evaluate(()=>['left','right'].map(side=>{const panel=document.querySelector('.dispatcher-'+side),grid=panel.querySelector('.dispatcher-excavators,.dispatcher-trucks'),first=grid.querySelector('[data-garage-item]'),copy=first.cloneNode(true);copy.removeAttribute('id');grid.appendChild(copy);const a=first.getBoundingClientRect(),b=copy.getBoundingClientRect();copy.remove();return Math.abs(a.x-b.x)<.01&&b.y>a.y&&grid.scrollWidth<=grid.clientWidth+1&&getComputedStyle(grid).overflowY==='auto';}));
    assert.deepEqual(narrowColumns,[true,true]);checks.push('phone CSS cannot turn either garage into a horizontal row');
    await handle('bottom').click(); await tick(300); await page.locator('[data-cr-mm-search]').fill('58');
    await verifyKeyboard(1024, 600, 360);
    await handle('bottom').click(); await tick(300);
    await page.evaluate(()=>{document.body.dataset.appShellVersion='unknown';});await tick(200);
    assert.equal(await page.locator('.dispatcher-canvas').getAttribute('data-dispatcher-canvas'),'off');
    assert.equal(await page.locator('html.cr-mm-panels').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.style.getPropertyValue('--cr-panel-canvas-w')),'');
    assert.equal(await page.evaluate(()=>document.documentElement.style.getPropertyValue('--cr-panel-keyboard-inset')),'');
    assert.equal(await page.evaluate(()=>document.documentElement.style.getPropertyValue('--cr-panel-toolbar-inset')),'');
    checks.push('unknown shell restores the real narrow-screen canvas mode after adapter teardown');
    assert.deepEqual(errors, []); assert.deepEqual(requests, []);
  } catch (error) { process.exitCode = 1; checks.push({ failure: error.message }); }
  finally { fs.writeFileSync(path.join(output, 'panels-results.json'), JSON.stringify({ checks, keyboardCases, errors, requests }, null, 2)); console.log(JSON.stringify({ checks, errors, requests }, null, 2)); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
