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
  const errors = [], requests = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'GET' && url.origin === 'https://dispatcher.driverform.ru') {
      if (url.pathname === '/dispatcher/control/') return route.fulfill({ contentType: 'text/html', body: fixture.replace('</head>', `<style>${css}</style></head>`) });
      if (url.pathname === '/tablet-adapter.js') return route.fulfill({ contentType: 'application/javascript', body: script });
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
    await page.evaluate(() => { window.panelFrames=[]; function frame(){ panelFrames.push([...document.querySelectorAll('[data-cr-panel]')].filter(n=>getComputedStyle(n).visibility==='visible').length); requestAnimationFrame(frame); }requestAnimationFrame(frame); });
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
    const geometry = () => page.evaluate(() => [...document.querySelectorAll('.dispatcher-complex-card,.complex-assigned-trucks,.complex-truck-tile')].map(n => { const r=n.getBoundingClientRect();return [r.x,r.y,r.width,r.height]; }));
    const fixedField = await geometry();
    for (const side of ['top', 'left', 'right', 'bottom']) { await handle(side).click(); await tick(250); assert.equal(await open(), side); assert.deepEqual(await geometry(), fixedField); }
    assert.equal(await page.evaluate(() => Math.max(...panelFrames)), 1); checks.push('strict exclusivity on every animation frame');
    const matchingTiles=await page.evaluate(()=>{
      const main=document.querySelector('.complex-truck-tile').getBoundingClientRect();
      return ['left','right'].map(side=>{const panel=document.querySelector('.dispatcher-'+side),r=panel.getBoundingClientRect(),tile=panel.querySelector('[data-garage-item]').getBoundingClientRect(),grid=panel.querySelector('.dispatcher-excavators,.dispatcher-trucks');return{side,height:r.height,y:r.y,width:r.width,columns:getComputedStyle(grid).gridTemplateColumns,tile:[tile.width,tile.height],main:[main.width,main.height]};});
    });
    for(const value of matchingTiles){assert.ok(Math.abs(value.height-800)<1&&Math.abs(value.y)<1&&value.width>value.tile[0]&&value.width-value.tile[0]<22);assert.equal(value.columns.trim().split(/\s+/).length,1);assert.ok(value.tile.every((n,i)=>Math.abs(n-value.main[i])<.01));}
    checks.push('full-height garages retain one vertical column at the actual main-rack tile size');
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
    assert.equal(await page.locator('.cr-mm-panel-backdrop').count(), 1); checks.push('focused search survives a background board fragment');
    const canvasBeforeKeyboard = await geometry();
    await page.setViewportSize({width:1333,height:480}); await tick(250); await page.clock.fastForward(15000);
    assert.equal(await open(), 'bottom'); assert.deepEqual(await geometry(), canvasBeforeKeyboard);
    await page.locator('[data-cr-mm-search]').press('Enter'); await tick(250); assert.equal(await open(), 'bottom');
    await page.setViewportSize({width:1333,height:800}); await tick(400);
    checks.push('keyboard resize defers layout and panel switching until input finishes');
    assert.equal(await open(), 'right'); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before); checks.push('search focus pause and Enter reveal without clearing');
    await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-search]').focus();
    await handle('left').click(); await tick(250); assert.equal(await open(), 'left'); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before);
    await handle('bottom').click(); await tick(250); await page.locator('[data-cr-mm-search]').focus();
    await page.locator('.cr-mm-panel-backdrop').click({position:{x:400,y:200}}); await tick(250);
    assert.equal(await page.locator('[data-cr-panel-state="open"]').count(), 0); assert.equal(await page.locator('[data-cr-mm-search]').inputValue(), before); checks.push('manual switch and outside close finish input without losing query');
    await handle('right').click(); await tick(250);
    await page.locator('.dispatcher-right [data-garage-item]').click(); assert.equal(await page.evaluate(() => fixtureDetails), 1);
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
    await page.evaluate(()=>{document.body.dataset.appShellVersion='unknown';});await tick(200);
    assert.equal(await page.locator('.dispatcher-canvas').getAttribute('data-dispatcher-canvas'),'off');
    assert.equal(await page.locator('html.cr-mm-panels').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.style.getPropertyValue('--cr-panel-canvas-w')),'');
    checks.push('unknown shell restores the real narrow-screen canvas mode after adapter teardown');
    assert.deepEqual(errors, []); assert.deepEqual(requests, []);
  } catch (error) { process.exitCode = 1; checks.push({ failure: error.message }); }
  finally { fs.writeFileSync(path.join(output, 'panels-results.json'), JSON.stringify({ checks, errors, requests }, null, 2)); console.log(JSON.stringify({ checks, errors, requests }, null, 2)); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
