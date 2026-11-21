/* Secure-context browser test. Native bridge replies are simulated only for transport;
   the visibility/export actor checks, IndexedDB, hash and byte chunks are real. */
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||'playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const base='https://mining-master.driverform.ru',home='/mining-master/tablet/',period=home+'reports/volume/?date_from=2026-11-01&date_to=2026-11-02',exp=period.replace('/volume/','/volume/export/'),unknown=period.replaceAll('11-0','10-0');
const out=path.resolve(process.argv[2]||path.join(__dirname,'../../../qa_artifacts/offline-reports'));fs.mkdirSync(out,{recursive:true});
const checks={},ok=(k,v)=>{assert(v,k);checks[k]=true;};
(async()=>{
 const b=await chromium.launch({headless:true}),ctx=await b.newContext(),p=await ctx.newPage();
 let offline=false,fileRequests=0;
 const documentBody='<html><body data-app-role-code="mining_master"><main>Строка проверенного отчёта 14,25 т</main></body></html>';
 const bytes=Buffer.alloc(110000,7);bytes.set([80,75,3,4]);
 await ctx.route('**/*',async r=>{const u=new URL(r.request().url());if(u.origin!==base)throw Error('External request');
    if(offline)return r.abort('internetdisconnected');
    if(u.pathname.endsWith('/export/')){fileRequests++;return r.fulfill({status:200,contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers:{'Content-Disposition':'attachment; filename=shift.xlsx'},body:bytes});}
    return r.fulfill({contentType:'text/html',body:documentBody});
 });
 try{
 await p.goto(base+home);
 await p.evaluate(()=>{
     const a={id:1,access_id:1,key:'A',generation:'A1',role:'mining_master',verified:true};
     window.localState={actor:a,shift:{is_open:true,local_shift_id:'S1'},locked:false};
     window.FieldLedger={read:async()=>structuredClone(localState)};
     window.notices=[];window.FieldIdentity={notice:m=>notices.push(m),confirmScope:async actor=>!window.testOffline&&actor.key===localState.actor.key};
     window.rendered=[];window.CopperFieldPage={postMessage:m=>rendered.push(JSON.parse(m))};
     window.chunks=[];window.CopperOfflineReport={postMessage(m){const o=JSON.parse(m);chunks.push(o);queueMicrotask(()=>this.onmessage({data:JSON.stringify({id:o.id,serial:o.serial,ok:true})}));}};
 });
 for(const file of ['field-archive','field-reports'])await p.addScriptTag({path:path.join(__dirname,'../profiles/mining_master_tablet/assets/tablet/'+file+'.js')});
 await p.evaluate(u=>FieldReports.open(u),period);
 ok('exact filter and save time rendered',await p.evaluate(u=>rendered.at(-1).route===u&&rendered.at(-1).html.includes('данные получены'),period));
 await p.evaluate(u=>FieldReports.open(u),exp);
 let chunks=await p.evaluate(()=>chunks);
 const copy=Buffer.concat(chunks.filter(c=>c.op==='chunk').map(c=>Buffer.from(c.bytes,'base64')));
 ok('real binary retained and written in bounded sequential chunks',copy.equals(bytes)&&chunks.filter(c=>c.op==='chunk').length===3&&chunks[0].sha256===crypto.createHash('sha256').update(bytes).digest('hex'));
 offline=true;await ctx.setOffline(true);await p.evaluate(()=>{window.testOffline=true;chunks.length=0;rendered.length=0;});
 await p.evaluate(u=>FieldReports.open(u),period);ok('exact cached period opens while offline',await p.evaluate(u=>rendered.at(-1).route===u,period));
 const before=fileRequests;await p.evaluate(u=>FieldReports.open(u),exp);
 chunks=await p.evaluate(()=>chunks);ok('offline export uses saved bytes and makes no new GET',chunks.at(-1).op==='commit'&&fileRequests===before);
 const missed=await p.evaluate(u=>FieldReports.open(u),unknown);
 ok('unavailable period remains pending; current report never used as substitute',!missed&&await p.evaluate(()=>rendered.length===1));
 await p.evaluate(()=>{localState.shift.local_shift_id='S2';});
 const deepLink=await p.evaluate(u=>FieldArchive.render(u),period);
 ok('cold report link cannot bypass generation and shift scope via generic archive',!deepLink&&await p.evaluate(()=>rendered.length===1));
 await p.evaluate(()=>{localState.shift.local_shift_id='S1';});
 await p.evaluate(()=>{localState.actor={...localState.actor,key:'B',generation:'B1',access_id:2};});
 const denied=await p.evaluate(u=>FieldReports.open(u),period);
 ok('new employee cannot read former employee report/export',!denied&&await p.evaluate(()=>rendered.length===1));
 await p.evaluate(()=>{localState.actor={...localState.actor,key:'A',generation:'A1',access_id:1};window.testOffline=false;});
 offline=false;await ctx.setOffline(false);await p.evaluate(()=>FieldReports.wake());await p.waitForTimeout(300);
 await ctx.setOffline(true);await p.evaluate(()=>window.testOffline=true);
 ok('deferred genuine period was fetched and is usable offline',await p.evaluate(u=>FieldReports.open(u),unknown));
 fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({checks,productionRequests:0},null,2));console.log(Object.keys(checks).length+' report/period/export browser checks passed');
 }finally{await b.close();}
})().catch(e=>{fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({checks,error:e.message},null,2));console.error(e);process.exitCode=1;});
