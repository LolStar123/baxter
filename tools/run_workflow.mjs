import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {validateTasks,nextWave,execute} from '../examples/portfolio/model.mjs';
const source=process.argv[2]||new URL('../examples/portfolio/data/workflow.json',import.meta.url);
const output=resolve(process.argv[3]||'output/workflow-receipts.json');
const workflow=JSON.parse(await readFile(source,'utf8'));validateTasks(workflow.tasks);
const files={...workflow.files},states=Object.fromEntries(workflow.tasks.map(t=>[t.id,'pending'])),receipts=[];
while(true){const wave=nextWave(workflow.tasks,states,3);if(!wave.length)break;
    for(const t of wave)states[t.id]='running';
    await Promise.all(wave.map(async t=>{const start=performance.now();try{const result=await execute(t,files);Object.assign(files,result.outputs);states[t.id]='passed';receipts.push({id:t.id,ok:true,proof:result.proof,outputs:Object.keys(result.outputs),elapsed:performance.now()-start});}catch(e){states[t.id]='failed';receipts.push({id:t.id,ok:false,error:e.message,elapsed:performance.now()-start});}}));
}
for(const id of Object.keys(states))if(states[id]==='pending')states[id]='blocked';
await mkdir(resolve(output,'..'),{recursive:true});await writeFile(output,JSON.stringify({tasks:workflow.tasks,states,files,receipts},null,2));
console.log(Object.values(states).filter(s=>s==='passed').length+'/'+workflow.tasks.length+' jobs passed; receipts: '+output);
if(Object.values(states).some(s=>s!=='passed'))process.exitCode=1;
