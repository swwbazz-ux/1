/* Compact placement text is independent of the messenger and works without a network. */
(function(root){
    "use strict";
    const clean=value=>String(value??"").replace(/\s+/g," ").trim();
    function compose(rows,master=""){
        const blocks=rows.map(row=>{
            const number=clean(row.number).replace(/^[КK]-/u,"");
            if(!number)throw Error("Не указан номер экскаватора.");
            const horizon=clean(row.horizon).replace(/^гор\.?\s*/iu,"");
            const points=[...new Set((row.destinations||[]).map(clean).filter(Boolean))];
            const heading=["№"+number];
            if(horizon && horizon!=="-")heading.push("гор. "+horizon);
            if(points.length)heading.push(points.join(", "));
            const trucks=row.trucks.map(clean).filter(Boolean);
            return heading.join(" · ")+"\n"+(trucks.length?trucks.join(", "):"Без самосвалов");
        });
        if(!blocks.length)throw Error("В расстановке пока нет комплексов.");
        const name=clean(master);
        const header="Предварительная расстановка"+(name?"\nМастер — "+name:"");
        const text=header+"\n\n"+blocks.join("\n\n");
        if(text.length>4000)throw Error("Расстановка превышает 4000 символов. Сократите названия точек разгрузки.");
        return text;
    }
    root.CopperPlacementText={compose};
    if(typeof module==="object" && module.exports)module.exports={compose};
})(typeof window==="object"?window:globalThis);
