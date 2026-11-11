const {readFileSync}=require('node:fs');
const {join}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const script=readFileSync(join(__dirname,'../app/index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
function client(fetch){
 const panel={hidden:true};
 const context={fetch,$:()=>panel};
 vm.createContext(context);
 vm.runInContext(script.slice(script.indexOf('async function api('),script.indexOf('async function loadModel(')),context);
 return {api:context.api,panel};
}
test('unreachable service tells the user how to recover',async()=>{
 const {api}=client(async()=>{throw new TypeError('Failed to fetch');});
 await assert.rejects(api('/api/query'),/无法连接本地服务.*启动/);
});
test('authentication errors keep their server message and expose sign-in',async()=>{
 const {api,panel}=client(async()=>({ok:false,status:401,json:async()=>({error:'需要访问口令'})}));
 await assert.rejects(api('/api/query'),/需要访问口令/);
 assert.equal(panel.hidden,false);
});
test('invalid response and cancellation remain distinct from network failure',async()=>{
 const invalid=client(async()=>({json:async()=>{throw new SyntaxError();}}));
 await assert.rejects(invalid.api('/api/query'),/服务响应无效/);
 const abort=Object.assign(new Error('aborted'),{name:'AbortError'});
 const cancelled=client(async()=>{throw abort;});
 await assert.rejects(cancelled.api('/api/query'),error=>error===abort);
});
