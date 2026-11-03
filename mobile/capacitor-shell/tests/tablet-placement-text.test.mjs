import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {compose}=createRequire(import.meta.url)('../profiles/mining_master_tablet/assets/tablet/tablet-placement-text.js');

test('agreed two-line format preserves machine order and excludes unrelated settings',()=>{
    assert.equal(compose([
        {number:'К-3',horizon:'150',destinations:['отвал'],trucks:['12','14','26','23','46'],loading_block:'221',plan:99},
        {number:'K-8',horizon:'Гор. 135',destinations:['отвал','отвал'],trucks:['55','56','58','61','62','63']}
    ]),'Предварительная расстановка\n\n№3 · гор. 150 · отвал\n12, 14, 26, 23, 46\n\n№8 · гор. 135 · отвал\n55, 56, 58, 61, 62, 63');
});
test('aliases, absent settings and empty complexes stay readable',()=>{
    assert.equal(compose([{number:'САТ-9',horizon:'Гор. -',trucks:[]}],'Иванов И.И.'),
        'Предварительная расстановка\nМастер — Иванов И.И.\n\n№САТ-9\nБез самосвалов');
});
test('12 complexes and 53 trucks fit in one message',()=>{
    let n=1;
    const rows=Array.from({length:12},(_,i)=>({number:String(i+1),horizon:'150',destinations:['ККД','отвал'],
        trucks:Array.from({length:i<5?5:4},()=>String(n++))}));
    const text=compose(rows);
    assert.equal(n-1,53);
    assert.equal(text.match(/№/g).length,12);
    assert(text.length<4000);
    assert(text.endsWith('50, 51, 52, 53'));
});
test('empty or excessively long placement is reported instead of silently sent incomplete',()=>{
    assert.throws(()=>compose([]),/нет комплексов/);
    assert.throws(()=>compose([{number:'1',trucks:[],destinations:['x'.repeat(4000)]}]),/4000/);
});
