/* Real Chromium and the policy used by the native WebView. No external requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const {chromium} = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
    const repo = path.resolve(__dirname, '../../..');
    const origin = 'https://mining-master.driverform.ru', route = '/mining-master/tablet/';
    const backend = path.join(repo, 'СИСТЕМА_MVP/backend/static');
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'copper-icon-test-'));
    let browser;
    try {
        const policy = path.resolve(__dirname, '../android/app/src/mining_master_tablet/java/ru/copperresources/mobile/TabletUrlPolicy.java');
        const cli = path.join(temp, 'IconPolicy.java');
        fs.writeFileSync(cli, [
            'package ru.copperresources.mobile;',
            'import java.nio.charset.StandardCharsets;',
            'class IconPolicy { public static void main(String[] args) throws Exception {',
            'if (args.length>0) { for (String url:args) System.out.println(TabletUrlPolicy.packagedIcon(url)); }',
            'else System.out.write(TabletUrlPolicy.allowPackagedIcons(new String(System.in.readAllBytes(),StandardCharsets.UTF_8)).getBytes(StandardCharsets.UTF_8));',
            '}}'
        ].join('\n'));
        execFileSync('javac', ['-encoding','UTF-8','-d',temp,policy,cli]);
        const java = (...args) => ['-cp',temp,'ru.copperresources.mobile.IconPolicy',...args];
        const nativeHTML = html => execFileSync('java', java(), {input:html,encoding:'utf8',maxBuffer:4e6});
        const icons = ['truck','excavator'].flatMap(kind => ['gray','blue','green','red','yellow'].map(color => kind+'-'+color+'.png'));
        const address = file => origin+'/static/img/equipment/'+file;
        const urls = icons.map(address);
        const allow = execFileSync('java',java(...urls),{encoding:'utf8'}).trim().split(/\r?\n/);
        assert.deepEqual(allow,icons);
        const modules = ['dispatcher-detail-charts-v1.js','dispatcher-detail-settings-v1.js','dispatcher-detail-v1.js'].map(file =>
            fs.readFileSync(path.join(backend,'js',file),'utf8')).join('\n');
        const raw = '<!doctype html><html><head></head><body data-app-role-code="mining_master"><main class="dispatcher-board">' +
            '<button id="truck" class="complex-truck-tile"><img src="/static/img/equipment/truck-gray.png" alt=""></button>' +
            '<button id="excavator"><img src="/static/img/equipment/excavator-gray.png" alt=""></button></main>' +
            '<script>'+modules+'<\/script></body></html>';
        browser = await chromium.launch({channel:'chrome',headless:true});
        const ctx = await browser.newContext({viewport:{width:1333,height:800},serviceWorkers:'block'});
        const archive = fs.readFileSync(path.resolve(__dirname,'../profiles/mining_master_tablet/assets/tablet/field-archive.js'),'utf8');
        let html = '<!doctype html><html><head><title>Local capture</title></head><body></body></html>';
        const outside = [],iconResponses = [],violations = [];
        const p = await ctx.newPage();
        p.on('pageerror',error => violations.push(error.message));
        p.on('console',msg => { if(msg.type()==='error')violations.push(msg.text()); });
        await ctx.route('**/*',request => {
            const url = new URL(request.request().url());
            if(url.href===origin+route)return request.fulfill({contentType:'text/html',body:html});
            let index = urls.indexOf(url.origin+url.pathname);
            if(index!==-1){
                iconResponses.push(allow[index]);
                return request.fulfill({contentType:'image/png',path:path.join(backend,'img/equipment',allow[index])});
            }
            outside.push(url.href);
            return request.abort('internetdisconnected');
        });
        await ctx.addInitScript({content:archive});
        await p.goto(origin+route);
        const captured = await p.evaluate(async raw => {
            window.FieldIdentity={confirmScope:async()=>true};
            const actor={verified:true,role:'mining_master',key:'test',id:1,access_id:1,generation:'test'};
            return (await FieldArchive.capture(FieldArchive.root,actor,{raw})).html;
        },raw);
        assert(captured.includes('src="'+urls[0]+'"'));
        // Original 0.1.42 captured images as data:. The live board subsequently overwrites their src.
        const giveLegacyBytes = (s,file) => s.split('src="'+address(file)+'"').join('src="data:image/png;base64,'+
            fs.readFileSync(path.join(backend,'img/equipment',file)).toString('base64')+'"');
        const legacy = giveLegacyBytes(giveLegacyBytes(captured,'truck-gray.png'),'excavator-gray.png');
        const checks = {};
        async function redraw() {
            return p.evaluate(async () => {
                const dt=window.createDispatcherDetail({});
                dt.setNodeEquipmentState(document.getElementById('truck'),'assigned','truck');
                dt.setNodeEquipmentState(document.getElementById('excavator'),'working','excavator');
                const imgs=[...document.querySelectorAll('.dispatcher-board img')];
                return (await Promise.all(imgs.map(img=>img.decode().then(()=>img.naturalWidth>0,()=>false)))).every(Boolean);
            });
        }
        await ctx.setOffline(true);
        html = legacy;
        await p.goto(origin+route);
        assert.equal(await redraw(),false,'Prior image CSP must reproduce the regression');
        violations.length=0;
        for(const [name,doc] of [['retained42',legacy],['newArchive',captured]]) {
            html=nativeHTML(doc);
            assert.equal(nativeHTML(html),html);
            await p.goto(origin+route);
            assert(await redraw());
            await p.reload();
            assert(await redraw());
            // A draft's inert iframe inherits the parent CSP and reuses original tile URLs as it repaints.
            const all = await p.evaluate(async urls => {
                const frame=document.createElement('iframe');
                const loaded=new Promise(resolve=>frame.onload=resolve);
                frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; script-src \'none\'"><main></main>';
                document.body.append(frame);await loaded;
                return await Promise.all(urls.map(async url => {
                    const image=frame.contentDocument.createElement('img');image.src=url;frame.contentDocument.body.append(image);
                    await image.decode();return image.naturalWidth>0;
                }));
            },urls);
            assert.equal(all.filter(Boolean).length,10);
            checks[name]=true;
        }
        assert.deepEqual(outside,[]);
        assert.deepEqual(violations,[]);
        assert(icons.every(name=>iconResponses.includes(name)));
        console.log(JSON.stringify({priorRegressionReproduced:true,...checks,originalIcons:icons.length,offline:true,unexpectedRequests:outside.length,errors:violations.length}));
    } finally {
        if(browser)await browser.close();
        fs.rmSync(temp,{recursive:true,force:true});
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
