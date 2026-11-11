const {readFileSync}=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
for(const page of ['index.html','langchain.html'])test(page+' defaults to images and reversibly toggles them',()=>{
 const html=readFileSync(path.join(__dirname,'../app',page),'utf8');
 assert.match(html,/<button id="imageToggle"[^>]*type="button"[^>]*aria-pressed="true"/);
 assert.match(html,/body\.images-off figure\{display:none!important\}/);
 let hidden=false;
 const context={document:{body:{classList:{toggle:(name,value)=>{assert.equal(name,'images-off');hidden=value;}}}}};
 vm.createContext(context);
 vm.runInContext(html.match(/function toggleImages\(button\)\{[^\n]+\}/)[0],context);
 const button={pressed:'true',getAttribute(){return this.pressed;},setAttribute(name,value){this.pressed=value;}};
 context.toggleImages(button);assert.equal(hidden,true);assert.equal(button.pressed,'false');assert.equal(button.textContent,'图片：关');
 context.toggleImages(button);assert.equal(hidden,false);assert.equal(button.pressed,'true');assert.equal(button.textContent,'图片：开');
});
