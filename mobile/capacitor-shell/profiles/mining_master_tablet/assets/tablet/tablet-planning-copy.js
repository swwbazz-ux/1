/* The planning tab uses the mounted production board as its template.
   Its document is scriptless. No production form or mutation handler is copied. */
(function (global) {
    "use strict";
    if (global.CopperTabletPlanning) return;
    const sourceSelector = "[data-dispatcher-drag][data-equipment-id]";
    const complexSelector = ".dispatcher-complex-card[data-dispatcher-drop='complex']";
    const truckSelector = "[data-dispatcher-drag='truck'][data-equipment-id]";
    const garageSelector = "[data-garage-item='excavator'][data-equipment-id]";
    const host = global.document;
    let current = null;

    function make(doc, tag, className, label) {
        const element = doc.createElement(tag);
        if (className) element.className = className;
        if (label) element.textContent = label;
        return element;
    }
    function maxAction(button) {
        const doc=button.ownerDocument, text=button.textContent;
        const svg=doc.createElementNS("http://www.w3.org/2000/svg","svg");
        svg.setAttribute("viewBox","0 0 100 100");svg.setAttribute("aria-hidden","true");
        svg.classList.add("cr-mm-max-mark");
        const path=doc.createElementNS(svg.namespaceURI,"path");
        path.setAttribute("fill","currentColor");path.setAttribute("fill-rule","evenodd");path.setAttribute("clip-rule","evenodd");
        // The same official MAX mark already used by the project's support link.
        path.setAttribute("d","M50.7571.2617C78.2929.2617 99.8857 22.5974 99.8857 50.1474S77.6071 99.4903 51.0214 99.4903c-9.4357 0-14.0071-1.3286-21.3714-6.5429-.5071-.3571-1.2-.2643-1.6286.1929C22.3571 99.1831 7.85 103.426 7.1857 95.176c0-14.3857-7.1857-23.7286-7.1857-45.3C0 21.5546 23.2214.2617 50.7571.2617Zm.7715 24.55c-13.0643-.6857-23.2643 8.3857-25.5143 22.5714-1.8643 11.75 1.4357 26.0715 4.2643 26.7929 1.2.3072 4.0785-1.9 6.1785-3.8786.3929-.3714.9929-.4357 1.45-.15 3.2715 2 6.9715 3.5 11.05 3.7143 13.4143.7 25.3-9.8 26.0072-23.2143.7-13.4143-10.0214-25.1428-23.4357-25.8357Z");
        svg.appendChild(path);button.classList.add("cr-mm-max-action");
        button.replaceChildren(svg,make(doc,"span","cr-mm-max-label",text));
    }
    function safeClone(node, doc) {
        const copy = doc.importNode(node, true);
        copy.querySelectorAll("script,iframe,frame,object,embed,link,style,base,meta,template,input[type=hidden]").forEach(n => n.remove());
        for (const child of [copy, ...copy.querySelectorAll("*")]) {
            // A search in the live pult does not select a truck in this copy.
            if (child.classList.contains("cr-mm-truck-hit")) child.classList.remove("cr-mm-truck-hit");
            for (const attr of [...child.attributes]) {
                if (/^on/i.test(attr.name) || /^(action|formaction|target|srcdoc|ping)$/i.test(attr.name) ||
                        /^(href|xlink:href)$/i.test(attr.name) && !attr.value.startsWith("#")) child.removeAttribute(attr.name);
            }
            if (child.matches("a[href]")) child.removeAttribute("href");
            if (child.matches("button")) child.type = "button";
        }
        return copy;
    }
    function copyAttributes(src, dest, names) {
        for (const name of names) if (src.hasAttribute(name)) dest.setAttribute(name,src.getAttribute(name));
    }
    function copyStyles(source, doc) {
        for(const node of source.querySelectorAll("head style, head link[rel~=stylesheet]")) {
            const copy = doc.importNode(node,true);
            if (node.tagName==="LINK") copy.href = new URL(node.getAttribute("href"),source.baseURI).href;
            doc.head.appendChild(copy);
        }
        const extra=host.getElementById("cr-tablet-planning-copy-css");
        if(extra && !doc.getElementById(extra.id)) doc.head.appendChild(doc.importNode(extra,true));
    }
    function numeric(node) { return Number(node?.dataset.equipmentId)||0; }
    function machineNumber(node) {
        if(!node)return "";
        if(node.matches(truckSelector)) return (node.querySelector(":scope > strong")?.textContent || node.dataset.equipmentName || "").trim();
        return (node.dataset.excavatorSlot || node.querySelector(".complex-title-state h2,:scope > strong")?.textContent || node.dataset.equipmentName || "").trim();
    }
    function catalog(board) {
        const cards=[...board.querySelectorAll(complexSelector)];
        const zones=new Map(cards.filter(numeric).map(card=>[numeric(card),card]));
        const excavators=new Map([...board.querySelectorAll(garageSelector),...zones.values()].map(n=>[numeric(n),n]));
        const trucks=new Map([...board.querySelectorAll(truckSelector)].map(n=>[numeric(n),n]));
        const live={excavators:cards.filter(numeric).map(card=>({excavator_id:numeric(card),truck_ids:[...card.querySelectorAll(truckSelector)].map(numeric)}))};
        return {cards,zones,excavators,trucks,live};
    }
    function parentNavigation(source, frameDoc, close) {
        const originals=[...source.querySelectorAll(".dispatcher-nav a")];
        const links=[...frameDoc.querySelectorAll(".dispatcher-nav a")];
        links.forEach((link,i)=>{
            const original=originals[i];
            const label=link.textContent.trim();
            link.classList.toggle("is-active",/Расстановка|Отч[её]ты/.test(label));
            link.tabIndex=0;link.setAttribute("role","link");
            if(/Расстановка|Отч[её]ты/.test(label)){
                (link.querySelector("span")||link).textContent="Расстановка";
                link.setAttribute("aria-current","page");
            }else{
                const go=()=>{
                    if(label==="Пульт") {close();return;}
                    const url=original?.getAttribute("href");
                    if(url && url!=="#"){
                        const target=new URL(url,host.baseURI);
                        if(target.origin===global.location.origin && target.pathname.startsWith("/mining-master/tablet/")){
                            close();global.location.assign(target.href);
                        }
                    }
                };
                link.addEventListener("click",go);
                link.addEventListener("keydown",e=>{if(e.key==="Enter")go();});
            }
        });
    }
    function buildDocument(state,doc) {
        const source=state.source;
        const sourceBoard=source.querySelector(".dispatcher-board");
        const sourceShell=sourceBoard.closest(".dispatcher-shell");
        const sourceCanvas=sourceBoard.closest(".dispatcher-canvas");
        const attrs=node=>[...node.attributes].map(a=>a.name).filter(name=>/^(class|style|dir|lang|data-)/.test(name));
        copyAttributes(source.documentElement,doc.documentElement,attrs(source.documentElement));
        copyAttributes(source.body,doc.body,attrs(source.body));
        doc.documentElement.dataset.crPlanCopy="true";
        doc.documentElement.classList.add("cr-mm-tablet","cr-mm-panels");
        doc.documentElement.dataset.crPanelsReady="true";
        copyStyles(source,doc);
        const canvas=make(doc,"div");copyAttributes(sourceCanvas,canvas,attrs(sourceCanvas));
        if (!canvas.classList.contains("dispatcher-canvas")) canvas.classList.add("dispatcher-canvas");
        canvas.dataset.dispatcherCanvas="on";
        const shell=make(doc,"main");copyAttributes(sourceShell,shell,attrs(sourceShell));
        const board=safeClone(sourceBoard,doc);
        global.CopperTabletPlanningCache.copyInitials(sourceBoard,board);
        const title=board.querySelector(".dispatcher-command-context > strong");
        if(title)title.textContent="Предварительная расстановка";
        const zoneTitle=board.querySelector(".dispatcher-zone-head h2");
        if(zoneTitle)zoneTitle.textContent="Предварительная расстановка";
        canvas.appendChild(shell);shell.appendChild(board);doc.body.appendChild(canvas);
        // A cloned HTML form can still react to Enter by itself. Block every submission.
        doc.addEventListener("submit",e=>{e.preventDefault();e.stopImmediatePropagation();},true);
        board.querySelectorAll(".dispatcher-shift-slot button, .dispatcher-command-utility button").forEach(n=>{
            n.disabled=true;n.title="Управление сменой на вкладке «Пульт»";
        });
        const toolbar=make(doc,"div");toolbar.id="cr-mm-toolbar";toolbar.dataset.crPanel="bottom";
        toolbar.setAttribute("aria-label","Предварительная расстановка");
        const label=make(doc,"label","cr-mm-assign-truck");
        const icon=source.getElementById("cr-mm-toolbar")?.querySelector(".cr-mm-assign-truck img");
        if(icon)label.appendChild(safeClone(icon,doc));
        const search=make(doc,"input");search.type="text";search.inputMode="numeric";search.autocomplete="off";
        search.setAttribute("aria-label","Живой поиск самосвала");search.placeholder="№ самосвала";search.dataset.planSearch="";
        label.appendChild(search);toolbar.appendChild(label);
        for(const [key,title] of [["share","В MAX"],["activate","Активировать расстановку"]]){
            const button=make(doc,"button","",title);button.type="button";button.setAttribute("data-plan-"+key,"");
            if(key==="share"){maxAction(button);button.setAttribute("aria-label","Подготовить расстановку для отправки в MAX");}
            toolbar.appendChild(button);
        }
        doc.body.appendChild(toolbar);
        for(const grip of source.querySelectorAll(".cr-mm-panel-handle,.cr-mm-panel-edge"))doc.body.appendChild(safeClone(grip,doc));
        for(const side of ["top","bottom","left","right"]){
            if(!doc.querySelector(".cr-mm-panel-handle[data-cr-panel-side='"+side+"']")){
                const handle=make(doc,"button","cr-mm-panel-handle");
                handle.type="button";handle.dataset.crPanelSide=side;doc.body.appendChild(handle);
                const edge=make(doc,"div","cr-mm-panel-edge");edge.dataset.crPanelSide=side;doc.body.appendChild(edge);
            }
        }
        const indicator=source.querySelector("body > .app-connection-dot");
        const topGrip=doc.querySelector(".cr-mm-panel-handle[data-cr-panel-side='top']");
        if(topGrip){
            const caption=make(doc,"span","","Черновик");
            caption.dataset.planGripCaption="";caption.setAttribute("aria-hidden","true");topGrip.appendChild(caption);
        }
        if(indicator)doc.body.appendChild(safeClone(indicator,doc));
        const standard=source.getElementById("app-confirm-modal");
        state.modal=standard?safeClone(standard,doc):fallbackModal(doc);
        doc.body.appendChild(state.modal);
        state.notice=make(doc,"div");state.notice.dataset.planNotice="";state.notice.setAttribute("role","status");state.notice.hidden=true;doc.body.appendChild(state.notice);
        Object.assign(state,{doc,board,search,catalog:catalog(sourceBoard)});
        parentNavigation(source,doc,close);
    }
    function fallbackModal(doc){
        const modal=make(doc,"div","app-confirm-modal");modal.id="app-confirm-modal";modal.hidden=true;
        modal.innerHTML='<div class="app-confirm-dialog" role="dialog" aria-modal="true"><button type="button" class="app-confirm-close" data-confirm-close>×</button><div class="app-confirm-content"><h2 id="app-confirm-title"></h2><p id="app-confirm-message"></p></div><div class="app-confirm-actions"><button type="button" class="secondary" data-confirm-cancel>Отмена</button><button type="button" data-confirm-accept>Готово</button></div></div>';
        return modal;
    }
    function message(state,text) {
        clearTimeout(state.noticeTimer);state.notice.textContent=text;state.notice.hidden=false;
        state.noticeTimer=setTimeout(()=>{state.notice.hidden=true;},4200);
    }
    function ask(state,title,body,action,callback){
        const modal=state.modal;
        modal.querySelector("#app-confirm-title").textContent=title;
        modal.querySelector("#app-confirm-message").textContent=body;
        const accept=modal.querySelector("[data-confirm-accept]");
        accept.classList.remove("cr-mm-max-action");
        accept.textContent=action;accept.hidden=false;accept.classList.toggle("danger",!!callback);
        modal.querySelector("[data-confirm-cancel]").hidden=!callback;
        const shut=()=>{modal.hidden=true;state.panels?.pause(false);state.panels?.activity();};
        modal.querySelector("[data-confirm-close]").onclick=shut;
        modal.querySelector("[data-confirm-cancel]").onclick=shut;
        accept.onclick=()=>{shut();if(callback)callback();};
        modal.hidden=false;state.panels?.pause(true);accept.focus({preventScroll:true});
    }
    function panels(state) {
        const {doc,board}=state, root=doc.documentElement;
        const node={top:board.querySelector(".dispatcher-topbar"),bottom:doc.getElementById("cr-mm-toolbar"),left:board.querySelector(".dispatcher-left"),right:board.querySelector(".dispatcher-right")};
        const factory=global.CopperMiningMasterTablet?.createPanelState;
        if(typeof factory!=="function")throw Error("Механика панелей пульта недоступна.");
        const machine=factory({
            setTimer:(callback,delay)=>global.setTimeout(callback,delay),clearTimer:id=>global.clearTimeout(id),
            blocked:()=>!!(state.dragging||!state.modal.hidden||doc.activeElement===state.search),
            render:({active,phase})=>{
                root.dataset.crPanelOpen=active||"";
                for(const [side,panel] of Object.entries(node)){
                    const open=active===side&&phase==="open";
                    panel.dataset.crPanel=side;panel.dataset.crPanelState=side===active?phase:"closed";
                    panel.inert=!open;panel.setAttribute("aria-hidden",String(!open));
                    const handle=doc.querySelector(".cr-mm-panel-handle[data-cr-panel-side='"+side+"']");
                    handle.setAttribute("aria-expanded",String(open));
                    handle.setAttribute("aria-label",(open?"Скрыть":"Показать")+" панель");
                    const edge=doc.querySelector(".cr-mm-panel-edge[data-cr-panel-side='"+side+"']");
                    if(edge)edge.hidden=active===side;
                }
            }
        });
        let start=null,ignoreClick=0;
        doc.addEventListener("pointerdown",e=>{
            machine.activity(); const grip=e.target.closest(".cr-mm-panel-handle,.cr-mm-panel-edge");
            if(grip && state.modal.hidden)start={grip,x:e.clientX,y:e.clientY,side:grip.dataset.crPanelSide};
        });
        doc.addEventListener("pointermove",e=>{
            if(!start)return;const dx=e.clientX-start.x,dy=e.clientY-start.y;
            if(Math.max(Math.abs(dx),Math.abs(dy))>9){try{start.grip.setPointerCapture(e.pointerId);}catch{}}
        });
        doc.addEventListener("pointercancel",()=>{start=null;});
        doc.addEventListener("pointerup",e=>{
            if(!start)return;const {side,x,y}=start;start=null;
            const dx=e.clientX-x,dy=e.clientY-y;
            const amount={left:dx,right:-dx,top:dy,bottom:-dy}[side];
            if(Math.abs(amount)>=36){ignoreClick=Date.now()+420;machine.request(amount>0?side:null);e.preventDefault();}
        });
        doc.addEventListener("click",e=>{
            const grip=e.target.closest(".cr-mm-panel-handle");
            if(grip && Date.now()>ignoreClick && state.modal.hidden){
                const side=grip.dataset.crPanelSide;machine.request(machine.state().active===side?null:side);
            }
        });
        state.search.addEventListener("focus",()=>machine.pause(true));
        state.search.addEventListener("blur",()=>machine.pause(false));
        state.panels=machine;
        machine.request("top");
    }
    function emptyCard(state,slot) {
        const doc=state.doc;
        const original=state.catalog.cards.find(n=>!numeric(n));
        const card=original?safeClone(original,doc):make(doc,"article","dispatcher-complex-card status-empty");
        if(!original)card.appendChild(make(doc,"div","complex-empty"));
        card.dataset.dispatcherDrop="complex";card.dataset.zoneId=slot.dataset.zoneId||"";card.dataset.zoneLabel=slot.dataset.zoneLabel||"";
        card.style.setProperty("--cr-complex-order",slot.style.getPropertyValue("--cr-complex-order"));
        return card;
    }
    function newComplex(state,id,slot){
        const {doc,catalog}=state, equipment=catalog.excavators.get(id);
        const card=emptyCard(state,slot);card.replaceChildren();
        card.classList.remove("status-empty");card.setAttribute("role","button");card.tabIndex=0;
        card.dataset.dispatcherDrag="complex";card.dataset.equipmentId=id;card.dataset.equipmentName=equipment.dataset.equipmentName||"";
        card.dataset.excavatorSlot=machineNumber(equipment);card.dataset.placementZone="active";
        const heading=make(doc,"div","complex-title-state");
        heading.appendChild(make(doc,"h2","", "К-"+machineNumber(equipment).replace(/^[КK]-?/iu,"")));
        const sourceHasWrapper=state.source.body.classList.contains("mining-master-mobile-screen");
        const wrapper=sourceHasWrapper?make(doc,"div","complex-work-head"):card;
        wrapper.append(heading,make(doc,"div","complex-context"));if(sourceHasWrapper)card.append(wrapper);
        card.appendChild(make(doc,"div","complex-assigned-trucks"));return card;
    }
    function truckTile(state,id,inGarage){
        const source=state.catalog.trucks.get(id);if(!source)return null;
        const tile=safeClone(source,state.doc);tile.hidden=false;tile.draggable=false;tile.classList.remove("is-assigned","is-search-hit","dispatcher-dragging");
        tile.classList.toggle("complex-truck-tile",!inGarage);
        if(inGarage){tile.dataset.garageItem="truck";delete tile.dataset.complexTruck;delete tile.dataset.assignedZone;}
        else {tile.dataset.complexTruck="true";delete tile.dataset.garageItem;}
        return tile;
    }
    function garageExcavator(state,id) {
        const original=state.catalog.excavators.get(id);
        if(original.matches(garageSelector))return safeClone(original,state.doc);
        const exemplar=state.source.querySelector(garageSelector);
        const tile=exemplar?safeClone(exemplar,state.doc):make(state.doc,"article","dispatcher-equipment-tile dispatcher-excavator-garage-tile");
        tile.replaceChildren();
        tile.className="dispatcher-equipment-tile dispatcher-excavator-garage-tile";
        tile.removeAttribute("style");tile.dataset.dispatcherDrag="excavator";tile.dataset.garageItem="excavator";
        tile.dataset.equipmentId=id;tile.dataset.equipmentName=original.dataset.equipmentName||"";tile.dataset.excavatorSlot=machineNumber(original);
        tile.dataset.placementZone="inactive";tile.tabIndex=0;tile.setAttribute("role","button");
        const image=state.doc.querySelector(".dispatcher-excavator-garage-tile img");
        tile.appendChild(make(state.doc,"strong","",machineNumber(original)));
        if(image)tile.appendChild(safeClone(image,state.doc));
        tile.appendChild(make(state.doc,"span","",original.dataset.equipmentName||""));
        return tile;
    }
    function sameIds(a,b) {return a.length===b.length && a.every((n,i)=>n===b[i]);}
    function setRack(state,card,ids) {
        const rack=card.querySelector(".complex-assigned-trucks");if(!rack)return;
        if(sameIds([...rack.querySelectorAll(truckSelector)].map(numeric),ids))return;
        rack.replaceChildren();
        for(const id of ids){const tile=truckTile(state,id,false);if(tile)rack.appendChild(tile);}
        if(!ids.length)rack.appendChild(make(state.doc,"em","complex-truck-empty","самосвалы не назначены"));
        const count=card.querySelector('[data-kpi="trucks"] b small');
        if(count)count.textContent="/"+ids.length;
    }
    function fillGarage(list,ids,selector,create){
        const present=[...list.querySelectorAll(selector)].filter(n=>!n.classList.contains("is-placeholder"));
        if(sameIds(present.map(numeric),ids))return;
        const filler=list.querySelector(".is-placeholder");
        present.forEach(n=>n.remove());
        for(const id of ids){const tile=create(id);if(tile)list.insertBefore(tile,filler);}
    }
    function stripLiveReadouts(state) {
        // projectSavedBoard also renders the live surface when offline. Only
        // the separate planning document should omit shift performance.
        if (!state.doc.documentElement.hasAttribute("data-cr-plan-copy")) return;
        const root=state.board;
        root.querySelectorAll('.dispatcher-plan-strip,.dispatcher-plan-loop-badge,.dispatcher-tile-plan-percent,.dispatcher-tile-plan-label,.mm-mobile-plan-ring,.complex-kpi[data-kpi="plan"],.complex-kpi[data-kpi="volume"],.complex-downtime-status,[data-gd-downtime-timer]').forEach(node=>node.remove());
        for(const tile of root.querySelectorAll(".dispatcher-complex-card,.dispatcher-truck-tile,.dispatcher-excavator-garage-tile")){
            if(tile.classList.contains("is-placeholder")||tile.classList.contains("status-empty"))continue;
            for(const attr of [...tile.attributes]){
                if(attr.name.startsWith("data-plan-") && attr.name!=="data-plan-selected")tile.removeAttribute(attr.name);
            }
            for(const prop of ["--tile-progress","--tile-total-progress","--complex-progress","--complex-total-progress"])tile.style.removeProperty(prop);
            tile.classList.remove("is-plan-overrun","has-plan-progress","is-inactive-plan-fill");
            for(const name of [...tile.classList])if(name.startsWith("status-"))tile.classList.remove(name);
            tile.removeAttribute("data-equipment-state");
            if(tile.matches(".dispatcher-complex-card")){
                tile.querySelector(".complex-state-chip")?.remove();
                const count=tile.querySelector('[data-kpi="trucks"]');
                if(count){
                    count.classList.remove("is-minus","is-plus");
                    count.title="Самосвалов в черновике";
                    const number=count.querySelector("b");
                    if(number)number.textContent=String(tile.querySelectorAll(truckSelector).length);
                }
            }else{
                const label=tile.querySelector(":scope > span");
                if(label)label.textContent=tile.closest(".dispatcher-complex-card")?"В комплексе":"В гараже";
            }
        }
    }
    function renderPlan(state) {
        const {board,catalog}=state;
        const rows=new Map(state.value.payload.excavators.map(r=>[r.excavator_id,r.truck_ids]));
        const grid=board.querySelector(".dispatcher-zone-grid");
        const slots=catalog.cards.map(card=>rows.has(numeric(card))?numeric(card):0);
        for(const id of rows.keys()){
            if(slots.includes(id)||!catalog.excavators.has(id))continue;
            const number=machineNumber(catalog.excavators.get(id)).replace(/^[KК]-?/iu,"");
            let place=slots.findIndex((value,i)=>!value && (catalog.cards[i].dataset.zoneLabel||"").replace(/^[KК]-?/iu,"")===number);
            if(place<0)place=slots.indexOf(0);
            if(place<0)slots.push(id);else slots[place]=id;
        }
        const children=[...grid.querySelectorAll(":scope > "+complexSelector)];
        slots.forEach((id,i)=>{
            let card=children[i];const slot=catalog.cards[i]||catalog.cards[catalog.cards.length-1];
            if(!slot)return;
            if(!card || numeric(card)!==id){
                const fresh=!id?emptyCard(state,slot):catalog.zones.has(id)?safeClone(catalog.zones.get(id),state.doc):newComplex(state,id,slot);
                fresh.style.setProperty("--cr-complex-order",slot.style.getPropertyValue("--cr-complex-order")||String(i));
                if(card)card.replaceWith(fresh);else grid.appendChild(fresh);
                card=fresh;
            }
            if(id)setRack(state,card,rows.get(id));
        });
        children.slice(slots.length).forEach(c=>c.remove());
        const assigned=new Set([...rows.values()].flat());
        const garageCompare=(map)=>(a,b)=>machineNumber(map.get(a)).localeCompare(machineNumber(map.get(b)),"ru",{numeric:true});
        const idleTrucks=[...catalog.trucks.keys()].filter(id=>!assigned.has(id)).sort(garageCompare(catalog.trucks));
        const idleExcavators=[...catalog.excavators.keys()].filter(id=>!rows.has(id)).sort(garageCompare(catalog.excavators));
        fillGarage(board.querySelector(".dispatcher-trucks"),idleTrucks,truckSelector,id=>truckTile(state,id,true));
        fillGarage(board.querySelector(".dispatcher-excavators"),idleExcavators,garageSelector,id=>garageExcavator(state,id));
        board.querySelectorAll(sourceSelector).forEach(n=>{n.draggable=false;});
        stripLiveReadouts(state);
        const activateButton=state.doc.querySelector("[data-plan-activate]");
        if(activateButton)activateButton.disabled=!mayEdit(state);
        showSelection(state);
    }
    function mayEdit(state) {
        return state.source.querySelector(".dispatcher-board")?.dataset.dispatcherOwnShiftOpen==="true" && state.source.body.dataset.observerMode!=="true";
    }
    function showSelection(state){
        state.board.querySelectorAll("[data-plan-selected]").forEach(n=>delete n.dataset.planSelected);
        const selected=state.selected;
        if(selected){
            const found=[...state.board.querySelectorAll(sourceSelector)].find(n=>numeric(n)===selected.id && n.dataset.dispatcherDrag===selected.kind);
            if(found)found.dataset.planSelected="true";
        }
    }
    function search(state) {
        const digits=state.search.value.replace(/\D/g,"");state.search.value=digits;
        let first;
        for(const tile of state.board.querySelectorAll(truckSelector)){
            const hit=!!digits && machineNumber(tile).replace(/\D/g,"")===digits;
            tile.classList.toggle("cr-mm-truck-hit",hit);
            if(hit && !first)first=tile;
        }
        if(first?.closest(".dispatcher-complex-card"))first.scrollIntoView({block:"nearest"});
        return first;
    }
    async function save(state,payload){
        if(!mayEdit(state)){message(state,"Изменять расстановку можно в своей открытой смене.");return;}
        if(state.busy)return;
        state.busy=true;
        try{
            const next=await global.CopperTabletDraft.save(payload,{...state.ctx,revision:state.value.revision});
            if(current!==state)return;
            state.value=next;state.selected=null;renderPlan(state);search(state);
        }catch(err){message(state,err.message||"Черновик не сохранён.");}
        finally{state.busy=false;}
    }
    function interactions(state){
        const {doc,board}=state;
        for(const n of board.querySelectorAll(sourceSelector))n.draggable=false;
        const getItem=el=>{
            const node=el?.closest(sourceSelector);
            return node ? {id:numeric(node),kind:node.dataset.dispatcherDrag}:null;
        };
        doc.querySelector("[data-plan-activate]").addEventListener("click",()=>{
            ask(state,"Активировать расстановку?","Применить подготовленный состав к действующему пульту смены?","Активировать",()=>activate(state));
        });
        doc.querySelector("[data-plan-share]").addEventListener("click",()=>sharePlacement(state));
        state.search.addEventListener("input",()=>search(state));
        state.search.addEventListener("keydown",e=>{
            if(e.key!=="Enter")return;
            e.preventDefault();const hit=search(state);state.search.blur();
            state.panels.request(hit?.closest(".dispatcher-right")?"right":null);
            if(!hit)message(state,"Самосвал с таким номером не найден.");
        });
        let press=null, suppressClickUntil=0;
        function clearPress(){
            if(press)clearTimeout(press.timer);
            press=null;state.dragging=false;state.panels.pause(false);state.panels.activity();
        }
        function trackDrag(x,y) {
            if(!press || !state.dragging)return;
            const w=doc.documentElement.clientWidth;
            if(x<30)state.panels.request("left");
            if(x>w-30)state.panels.request("right");
            if(x>=30 && x<w-30 && (press.side==="left" || press.side==="right") && !press.leftGarage){
                const entered=doc.elementFromPoint(x,y)?.closest(".dispatcher-complexes");
                if(entered){press.leftGarage=true;state.panels.request(null);}
            }
        }
        board.addEventListener("pointerdown",e=>{
            const item=getItem(e.target);if(!item||!mayEdit(state)||e.button!==0||!e.isPrimary)return;
            clearPress();press={item,x:e.clientX,y:e.clientY,id:e.pointerId,side:state.panels.state().active,source:e.target};
            const begin=()=>{
                if(!press)return;
                state.dragging=true;state.selected=item;showSelection(state);state.panels.pause(true);
                try{press.source.setPointerCapture(press.id);}catch{}
            };
            if(e.pointerType==="mouse")press.begin=begin;
            press.timer=setTimeout(begin,270);
        });
        doc.addEventListener("pointermove",e=>{
            if(!press || e.pointerId!==press.id)return;
            const dist=Math.hypot(e.clientX-press.x,e.clientY-press.y);
            if(dist>7 && !state.dragging){
                if(press.begin)press.begin();
                else{clearPress();return;}
            }
            if(state.dragging){trackDrag(e.clientX,e.clientY);e.preventDefault();}
        },{passive:false});
        doc.addEventListener("touchmove",e=>{
            if(!state.dragging || !e.cancelable)return;
            e.preventDefault();
        },{passive:false});
        doc.addEventListener("pointerup",e=>{
            if(!press || e.pointerId!==press.id)return;
            if(state.dragging){
                const target=doc.elementFromPoint(e.clientX,e.clientY);
                if(Math.hypot(e.clientX-press.x,e.clientY-press.y)>12)move(state,press.item,target);
                suppressClickUntil=Date.now()+400;
            }
            clearPress();
        });
        doc.addEventListener("pointercancel",clearPress);
        doc.addEventListener("contextmenu",e=>{if(e.target.closest(sourceSelector))e.preventDefault();});
        board.addEventListener("click",e=>{
            if(Date.now()<suppressClickUntil || !state.modal.hidden)return;
            if(e.target.closest(".dispatcher-topbar"))return;
            const overflow=e.target.closest(".complex-truck-more");
            if(overflow){overflow.closest(".complex-assigned-trucks")?.classList.toggle("cr-mm-expanded-rack");return;}
            const item=getItem(e.target);
            if(state.selected && move(state,state.selected,e.target))return;
            if(!item){state.selected=null;showSelection(state);return;}
            if(!mayEdit(state)){message(state,"Предварительная расстановка. Смена закрыта.");return;}
            state.selected=state.selected?.kind===item.kind && state.selected?.id===item.id?null:item;
            showSelection(state);
            if(state.selected?.kind==="excavator")state.panels.request(null);
            else if(state.selected?.kind==="truck" && e.target.closest(".dispatcher-right"))state.panels.request(null);
        });
        state.cleanup=clearPress;
    }
    function close(){
        const state=current;if(!state)return;
        current=null;
        state.cleanup?.();state.panels?.destroy();clearTimeout(state.noticeTimer);
        state.frame.remove();
        if(typeof state.ctx.onClose==="function")state.ctx.onClose();
    }
    function open(context={}){
        if(current)return current.ready;
        const source=context.document||host;
        if(!source.querySelector(".dispatcher-canvas .dispatcher-shell .dispatcher-board"))return Promise.reject(new Error("Исходный пульт ещё не готов."));
        const frame=make(host,"iframe");frame.dataset.crPlanningFrame="";
        // WebView also suppresses listeners registered by the parent if sandboxed scripts are disabled.
        // CSP denies every script in the copy itself. Only the parent installs our local listeners.
        frame.title="Предварительная расстановка";frame.setAttribute("sandbox","allow-same-origin allow-scripts");
        Object.assign(frame.style,{position:"fixed",inset:"0",width:"100%",height:"100%",border:"0",zIndex:"2147482000",background:"#10191e"});
        const state={source,ctx:{...context,document:source},frame,selected:null,busy:false,dragging:false};
        current=state;
        state.ready=new Promise((resolve,reject)=>{
            const render=async()=>{
                try{
                    const doc=frame.contentDocument;
                    if(!doc?.querySelector("[data-plan-bootstrap]") || current!==state)return;
                    frame.removeEventListener("load",render);
                    buildDocument(state,doc);
                    state.value=await global.CopperTabletDraft.load(state.catalog.live,state.ctx);
                    if(current!==state){resolve(false);return;}
                    panels(state);renderPlan(state);interactions(state);
                    frame.dataset.ready="true";global.FieldPortrait?.paint();resolve(true);
                }catch(error){if(current===state)close();reject(error);}
            };
            frame.addEventListener("load",render);
            frame.srcdoc=`<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta data-plan-bootstrap><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; form-action 'none'; base-uri 'none'; script-src 'none'; connect-src 'none'; frame-src 'none'"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body></body></html>`;
            host.body.appendChild(frame);
        });
        return state.ready;
    }
    function move(state,from,target) {
        if(!from || !target)return false;
        const dest=target.closest("[data-dispatcher-drop]");
        if(!dest)return false;
        const kind=dest.dataset.dispatcherDrop, id=numeric(dest);
        const next=structuredClone(state.value.payload), rows=next.excavators;
        if(from.kind==="truck"){
            if(!((kind==="complex"&&id) || kind==="truck-garage"))return false;
            if(kind==="truck-garage"&&!rows.some(r=>r.truck_ids.includes(from.id)))return false;
            if(kind==="complex"&&rows.find(r=>r.excavator_id===id)?.truck_ids.includes(from.id))return false;
            rows.forEach(r=>{r.truck_ids=r.truck_ids.filter(n=>n!==from.id);});
            if(id)rows.find(r=>r.excavator_id===id).truck_ids.push(from.id);
        }else if(from.kind==="excavator"){
            if(kind!=="complex" || rows.some(r=>r.excavator_id===from.id))return false;
            rows.push({excavator_id:from.id,truck_ids:[]});
        }else if(from.kind==="complex"){
            if(kind==="truck-garage"){
                const row=rows.find(r=>r.excavator_id===from.id);if(!row||!row.truck_ids.length)return false;
                row.truck_ids=[];
                ask(state,"Освободить самосвалы?","Все самосвалы этого комплекса вернутся в гараж черновика.","Освободить",()=>save(state,next));return true;
            }
            if(kind!=="excavator-garage")return false;
            next.excavators=rows.filter(r=>r.excavator_id!==from.id);
            ask(state,"Расформировать комплекс?","Экскаватор и его самосвалы вернутся в гаражи черновика.","Расформировать",()=>save(state,next));return true;
        }else return false;
        void save(state,next);return true;
    }
    async function planText(state) {
        const snapshot=global.FieldLedger?await global.FieldLedger.read():{};
        let cards={};
        try{cards=JSON.parse(state.source.getElementById("gd-equipment-cards-data")?.textContent||"{}");}catch{/* Use the visible original card. */}
        const rows=state.value.payload.excavators.map(row=>{
            const id=row.excavator_id,original=state.catalog.zones.get(id);
            const keys=["complex:"+id,"equipment:"+id,"complex-equipment-"+id,String(id)];
            const details=keys.map(key=>snapshot.details?.[key]).find(Boolean)||keys.map(key=>cards[key]).find(Boolean);
            const cfg=details?.settings;
            const points=cfg && Array.isArray(cfg.destinations) ?
                cfg.destinations.map(d=>d.name||cfg.dump_points?.find(p=>Number(p.id)===Number(d.dump_point_id))?.name).filter(Boolean) :
                [...(original?.querySelectorAll(".chip-unload")||[])].map(n=>n.textContent.trim().replace(/\s+\d+(?:[.,]\d+)?%$/u,""));
            return {number:machineNumber(state.catalog.excavators.get(id)),
                horizon:cfg?cfg.loading_horizon:original?.querySelector(".chip-horizon")?.textContent,
                destinations:points,trucks:row.truck_ids.map(id=>machineNumber(state.catalog.trucks.get(id)))};
        });
        return {text:global.CopperPlacementText.compose(rows,snapshot.actor?.full_name),
            stamp:JSON.stringify([snapshot.actor?.key,snapshot.actor?.generation,snapshot.shift?.local_shift_id,
                snapshot.shift?.is_open,snapshot.locked,state.value.revision])};
    }
    async function sharePlacement(state){
        try{
            const prepared=await planText(state);
            if(current!==state)return;
            ask(state,"Расстановка для MAX",prepared.text,"В MAX",async()=>{
                try{
                    const fresh=await planText(state);
                    if(current!==state)return;
                    if(fresh.stamp!==prepared.stamp || fresh.text!==prepared.text){
                        message(state,"Расстановка изменилась. Проверьте обновлённый текст.");
                        await sharePlacement(state);return;
                    }
                    if(global.CopperFieldPage){
                        global.CopperFieldPage.postMessage(JSON.stringify({op:"share_placement_max",text:prepared.text}));
                    }else if(global.navigator.share)await global.navigator.share({text:prepared.text});
                    else{
                        await global.navigator.clipboard.writeText(prepared.text);
                        message(state,"Расстановка скопирована. Вставьте её в MAX.");
                    }
                }catch(error){if(error.name!=="AbortError")message(state,error.message||"Не удалось открыть MAX.");}
            });
            state.modal.querySelector("[data-confirm-accept]").classList.remove("danger");
            maxAction(state.modal.querySelector("[data-confirm-accept]"));
        }catch(error){message(state,error.message||"Не удалось подготовить расстановку.");}
    }
    async function activate(state){
        if(state.busy||!mayEdit(state))return;
        state.busy=true;
        try{
            const result=await global.CopperTabletDraft.activate({...state.ctx,revision:state.value.revision});
            if(current!==state)return;
            message(state,result.message || (result.status==="applied"?"Расстановка применена.":"Действие сохранено на планшете."));
            if(result.status==="applied"){
                if(state.ctx.onApplied)state.ctx.onApplied(result);
                else host.dispatchEvent(new CustomEvent("cr-mm-plan-applied",{detail:result}));
            }
            state.value=await global.CopperTabletDraft.load(state.catalog.live,state.ctx);
            if(current===state)renderPlan(state);
            if(current===state && result.status==="conflict" && global.CopperTabletDraft.rebase){
                ask(state,"Сверить черновик с пультом?",(result.message||"Состав изменился.")+" Сверка обновит черновик. Для применения отдельно нажмите «Активировать».","Сверить",async()=>{
                    try{
                        state.value=await global.CopperTabletDraft.rebase({...state.ctx,revision:state.value.revision});
                        if(current===state){renderPlan(state);message(state,"Черновик сверен. Проверьте состав перед применением.");}
                    }catch(err){message(state,err.message||"Не удалось сверить черновик.");}
                });
            }
        }catch(err){message(state,err.message||"Не удалось активировать расстановку.");}
        finally{state.busy=false;}
    }
    function projectSavedBoard(doc,payload) {
        if(!doc?.documentElement.hasAttribute("data-cr-saved-board") || !Array.isArray(payload?.excavators))return false;
        const board=doc.querySelector(".dispatcher-board");
        if(!board)return false;
        const state={doc,source:doc,board,catalog:catalog(board),value:{payload}};
        renderPlan(state);
        return true;
    }
    global.CopperTabletPlanning={open,close,isOpen:()=>!!current,projectSavedBoard};
})(window);
