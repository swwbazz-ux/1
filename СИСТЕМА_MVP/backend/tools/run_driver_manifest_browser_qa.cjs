"use strict";
/* Local real-template browser integration; only synthetic worktree fixtures. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

const output = path.resolve(__dirname, '../qa_artifacts/driver-manifest-downtime');
const fixture = JSON.parse(fs.readFileSync(path.join(output, 'fixture.json'), 'utf8'));
const baseUrl = 'http://127.0.0.1:8024';
const result = {source: 'START_SERVER_MVP.bat; isolated SQLite; actual /driver/?tab=manifest template', checks: [], errors: [], screenshots: []};
const journal = {version:1,shifts:[{
    aliases:['s:'+fixture.shift_id], opened_at:new Date(Date.parse(fixture.first)-1200000).toISOString(),
    closed_at:new Date(Date.parse(fixture.first)+38553000).toISOString(), updated_at:new Date().toISOString(),
    log:fixture.log, server:null,
}]};

(async()=>{
    const browser=await chromium.launch({headless:true,executablePath:process.env.QA_CHROMIUM_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
    try {
        for(const width of [390,1280]) {
            const context=await browser.newContext({viewport:{width,height:width===390?844:900},deviceScaleFactor:1,serviceWorkers:'block'});
            await context.addCookies([{name:'sessionid',value:fixture.session,url:baseUrl}]);
            await context.addInitScript(({key,journal})=>{
                if(!localStorage.getItem('qa-manifest-injected')) {
                    localStorage.setItem(key,JSON.stringify(journal));
                    localStorage.setItem('qa-manifest-injected','1');
                }
            },{key:'driver-manifest-local-v1:'+fixture.access_id,journal});
            const page=await context.newPage();
            const pageErrors=[];
            page.on('pageerror',error=>pageErrors.push(error.message));
            page.on('console',message=>{if(message.type()==='error'&&!/service worker|serviceworker|vibrate|play\(\)|autoplay/i.test(message.text()))pageErrors.push(message.text());});
            const response=await page.goto(baseUrl+'/driver/?tab=manifest',{waitUntil:'domcontentloaded'});
            assert.equal(response.status(),200);
            await page.locator('[data-driver-tab-panel="manifest"].is-active').waitFor({state:'visible'});
            await page.waitForFunction(()=>window.DriverManifestLocal&&document.querySelector('[data-driver-report-downtime][data-reason="Ожидание погрузки"]')?.dataset.duration==='55 мин.');
            await page.waitForFunction(()=>typeof window.driverCloseDowntimeForTrip==='function');
            const metrics=await page.evaluate(()=>{
                const shell=document.querySelector('[data-driver-shell]');
                const model=window.DriverManifestLocal.model(shell);
                const report=window.DriverManifestLocal.render(shell);
                const panel=shell.querySelector('[data-driver-tab-panel="manifest"]');
                const rows=Array.from(panel.querySelectorAll('[data-driver-report-downtime]')).map(row=>({reason:row.dataset.reason,duration:row.dataset.duration}));
                const overlap=Array.from(panel.querySelectorAll('.driver-report-row')).filter(node=>node.getBoundingClientRect().width>0).some(node=>{
                    const label=node.querySelector('span')?.getBoundingClientRect(),value=node.querySelector('strong')?.getBoundingClientRect();
                    return label&&value&&label.right>value.left+1;
                });
                return {rows,overflow:document.documentElement.scrollWidth>innerWidth+1,overlap,tripTotal:panel.dataset.driverReportTripTotal,
                    waitingSeconds:model.rows.downtimes.filter(row=>row.reason==='Ожидание погрузки').reduce((sum,row)=>sum+(Date.parse(row.ended_at)-Date.parse(row.started_at))/1000,0),
                    moduleLoaded:!!report};
            });
            assert.equal(metrics.waitingSeconds,3321);
            assert.equal(metrics.tripTotal,'15');
            assert.equal(metrics.overflow,false);
            assert.equal(metrics.overlap,false);
            assert.deepEqual(metrics.rows.map(row=>row.duration),['55 мин.','13 мин.','17 мин.']);
            await page.locator('[data-driver-manifest-view-open="timeline"]').click();
            await page.locator('[data-driver-manifest-view="timeline"].is-active').waitFor({state:'visible'});
            assert(await page.locator('[data-driver-timeline] .driver-timeline-row').count()>15);
            await page.locator('[data-driver-manifest-view-open="report"]').click();
            const screenshot=path.join(output,'manifest-'+width+'.png');
            await page.screenshot({path:screenshot,fullPage:true});
            result.screenshots.push(screenshot);
            assert.deepEqual(pageErrors,[]);
            if(width===390) {
                await context.setOffline(true);
                const pending=await page.evaluate(({access,shift,first})=>{
                    const start=Date.parse(first)+35000000;
                    const key='pending-browser-refuel';
                    const panel=document.querySelector('[data-driver-tab-panel="manifest"]');
                    const payload=JSON.parse(panel.getAttribute('data-driver-manifest-data'));
                    const reason=Object.keys(payload.labels.reasons).find(id=>payload.labels.reasons[id]==='Заправка');
                    window.DriverManifestLocal.observe([
                        {event_id:key,event_type:'driver.downtime.started',sequence:35001,access_id:access,shift_id:shift,occurred_at:new Date(start).toISOString(),local_downtime_id:key,payload:{reason_id:Number(reason)},state:'pending'},
                        {event_id:key+'-end',event_type:'driver.downtime.ended',sequence:35121,access_id:access,shift_id:shift,occurred_at:new Date(start+120000).toISOString(),local_downtime_id:key,payload:{local_downtime_id:key},state:'pending'}
                    ]);
                    window.DriverManifestLocal.render(document.querySelector('[data-driver-shell]'));
                    return document.querySelector('[data-driver-report-downtime][data-reason="Заправка"]').dataset.duration;
                },{access:fixture.access_id,shift:fixture.shift_id,first:fixture.first});
                assert.equal(pending,'19 мин.');
                await context.setOffline(false);
                await page.reload({waitUntil:'domcontentloaded'});
                await page.waitForFunction(()=>document.querySelector('[data-driver-report-downtime][data-reason="Заправка"]')?.dataset.duration==='19 мин.');
                assert.equal(await page.locator('[data-driver-report-downtime][data-reason="Ожидание погрузки"]').getAttribute('data-duration'),'55 мин.');
                result.checks.push('pending local start/end rendered offline and survived browser reload alongside confirmed server data');
                assert(await page.locator('[data-driver-manifest-provisional]').isVisible());
            }
            // Сетевой отказ ожидаем только в явно созданной offline-фазе.
            assert.deepEqual(pageErrors.filter(message=>width!==390||message!=='Failed to load resource: net::ERR_INTERNET_DISCONNECTED'),[]);
            result.checks.push({width,httpStatus:response.status(),...metrics,consoleErrors:pageErrors});
            await context.close();
        }
        result.status='PASS';
    } catch(error) {
        result.status='FAIL'; result.errors.push(error.stack||String(error)); throw error;
    } finally {
        fs.writeFileSync(path.join(output,'browser-report.json'),JSON.stringify(result,null,2));
        await browser.close();
    }
    console.log(JSON.stringify(result,null,2));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
