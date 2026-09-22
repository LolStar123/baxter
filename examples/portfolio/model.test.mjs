import test from 'node:test';
import assert from 'node:assert/strict';
import * as m from './model.mjs';
test('workflow invariants and boundary cases',()=>{
const batches=m.schedule(m.defaults.tasks,4);assert.equal(batches.length,2);for(const b of batches){const files=b.flatMap(t=>t.files);assert.equal(files.length,new Set(files).size);}assert.equal(m.run(m.defaults).metrics['held for repair'],1);assert.equal(m.run({...m.defaults,repairPassed:true}).metrics['held for repair'],0);assert.throws(()=>m.schedule([{id:'no proof',files:['a']}],1));
});
