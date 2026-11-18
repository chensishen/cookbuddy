// Exercise the shipped renderer without interpreting any answer as HTML.
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const html=readFileSync(require('node:path').join(__dirname,'../app/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
class Node {
 constructor(tag,text=''){this.tag=tag;this.children=[];if(text)this.append(text);}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[...items];}
 addEventListener(){}
 setAttribute(key,value){this[key]=value;}
 set textContent(value){this.children=[value];}
 get textContent(){return this.children.map(n=>typeof n==='string'?n:n.textContent).join('');}
 find(tag){return this.children.flatMap(n=>typeof n==='string'?[]:[...(n.tag===tag?[n]:[]),...n.find(tag)]);}
}
const context={URL,location:{href:'http://127.0.0.1:8080/'},document:{createTextNode:text=>new Node('#text',text)},element:(tag,text)=>new Node(tag,text)};
vm.createContext(context);
vm.runInContext(readFileSync(require('node:path').join(__dirname,'../app/recipe_markdown.js'),'utf8'),context);
vm.runInContext(script.slice(script.indexOf('function inventoryQuestion'),script.indexOf('function createTurn')),context);
vm.runInContext(script.slice(script.indexOf('function answerText'),script.indexOf("$('cancel').onclick")),context);
vm.runInContext(script.slice(script.indexOf('function combinedRecipeOutputs'),script.indexOf('function answerText')),context);
vm.runInContext(script.slice(script.indexOf('function lazyDetails'),script.indexOf('function combinedRecipeOutputs')),context);
test('understanding outage shows one explanation and preserves the request identifier',()=>{
 const root=new Node('div');context.render({status:'degraded',mode:'langchain',unsupported_in_fallback:true,request_id:'test-request',
  service_notice:'现在暂时无法完成这个请求，请提供菜名。',outputs:[{tool:'general_chat',result:{status:'verified_source',short_answer:'理解服务暂时不可用，请稍后重试。'}}]}, {body:root});
 assert.ok(root.textContent.includes('理解服务暂时不可用'));
 assert.ok(!root.textContent.includes('现在暂时无法完成'));
 assert.ok(root.textContent.includes('test-request'));
});
test('clarification renders every returned option instead of hiding the fifth item',()=>{
 const root=new Node('div');context.render({status:'completed',outputs:[{tool:'general_chat',result:{
  status:'verified_source',short_answer:'刚才只推荐了5道菜，你想选哪道？',
  clarification_options:['1. 菜1','2. 菜2','3. 菜3','4. 菜4','5. 菜5'],sources:[],evidence:[]
 }}]}, {body:root});
 assert.deepEqual(root.find('button').map(node=>node.textContent),['1. 菜1','2. 菜2','3. 菜3','4. 菜4','5. 菜5']);
});
test('inventory recommendations display a specific reason on every card',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',recommendations:[
  {title:'炒滑蛋',source:'egg.md',reason:'可以用上你现有的鸡蛋。',matched:['鸡蛋'],missing:['牛奶'],coverage:.5}]});
 assert.ok(root.textContent.includes('推荐理由：可以用上你现有的鸡蛋。'));
 assert.ok(root.textContent.includes('待备：牛奶'));
});
test('recommendation cards expose mouse and keyboard detail entry by exact source',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',recommendations:[
  {title:'炒滑蛋',source:'egg.md',reason:'使用鸡蛋。',matched:['鸡蛋'],missing:['牛奶']}]});
 const card=root.find('article')[0];assert.equal(card.role,'button');assert.equal(card.tabIndex,0);
 assert.equal(card['aria-label'],'查看炒滑蛋的详细菜谱');assert.equal(typeof card.onclick,'function');assert.equal(typeof card.onkeydown,'function');
 assert.ok(card.textContent.includes('查看完整菜谱'));
});
test('inventory with preference filters still displays matched and missing foods',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',recommendation_kind:'preference',recommendation_mode:'inventory',recommendations:[
  {title:'土豆菜',source:'potato.md',description:'土豆菜的介绍。',reason:'使用土豆，不放鸡蛋。',matched:['土豆'],missing:['盐'],tools:['锅'],main_ingredients:['土豆']}]});
 assert.ok(root.textContent.includes('已有：土豆。待备：盐。'));
 assert.ok(root.textContent.includes('原文工具：锅'));
});
test('ingredient followups show the inventory comparison instead of dumping the source',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified_source',answer_layout:'inventory_check',
  answer:'好呀，还需要准备糍粑。',short_answer:'好呀，还需要准备糍粑。',
  sources:[{title:'蛋煎糍粑',source:'egg.md'}],evidence:[{source:'egg.md',quote:'原文只用于核对'}]});
 assert.ok(root.textContent.includes('好呀，还需要准备糍粑。'));
 assert.ok(!root.textContent.includes('原文只用于核对'));
});
test('image-only followups keep source paragraphs inside the audit, not the answer',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified_source',answer_layout:'images_only',
  answer:'找到3张酵头图片。',short_answer:'找到3张酵头图片。',sources:[{title:'面包',source:'bread.md'}],
  evidence:[{source:'bread.md',quote:'整段酵头制作步骤'}],images:[{source:'bread.md',image_path:'starter.jpg',alt:'酵头'}]});
 assert.equal(root.find('img').length,1);
 assert.ok(!root.textContent.includes('整段酵头制作步骤'));
 assert.ok(root.textContent.includes('找到3张酵头图片。'));
});
test('emphasis, nested emphasis, inline code and links display without delimiters',()=>{
 const result=context.readableText('**用料**：***30℃ 的温水***，*可选*，`2 g`，[来源](https://example.com)');
 assert.equal(result.textContent,'用料：30℃ 的温水，可选，2 g，来源');
 assert.equal(result.find('strong').length,2);assert.equal(result.find('em').length,2);
 assert.equal(result.find('a')[0].href,'https://example.com/');
});
test('steps retain order, quotes and tables have semantic containers',()=>{
 const result=context.readableText('### 操作 > 酵头\n1. 静置5分钟\n2. 发酵45～60分钟\n\n> 保持温暖\n\n| 原料 | 用量 |\n| --- | --- |\n| 盐 | 2g |');
 assert.equal(result.find('h3')[0].textContent,'酵头');assert.equal(result.find('ol').length,1);
 assert.equal(result.find('li').length,2);assert.equal(result.find('blockquote').length,1);
 assert.equal(result.find('table').length,1);assert.equal(result.find('td')[1].textContent,'2g');
});
test('markdown wrapper is removed and prose comparisons are retained',()=>{
 const result=context.answerDisplay('```markdown\n## 做法\n**温度** > 30℃。[1]\n```');
 assert.equal(result.textContent,'做法温度 > 30℃。');assert.equal(result.find('pre').length,0);
});
test('HTML is literal and active links cannot be created',()=>{
 const result=context.readableText('<img src=x onerror=alert(1)> [点我](javascript:alert)');
 assert.equal(result.find('img').length,0);assert.equal(result.find('a').length,0);
 assert.ok(result.textContent.includes('<img src=x onerror=alert(1)>'));
});
test('ingredient sublists keep their parent and quoted steps are rendered',()=>{
 const result=context.readableText('- 酵头\n  - 面粉1 cup\n  - 盐2g\n- 面团\n  - 面粉2½ cup\n\n>1. 检查水温\n>2. 延长发酵');
 const lists=result.find('ul');assert.equal(lists.length,3);
 assert.ok(lists[0].children[0].find('ul')[0].textContent.includes('盐2g'));
 assert.equal(result.find('blockquote')[0].find('ol').length,1);
});
test('historical chunk headings only appear at the start of a section',()=>{
 const result=context.answerDisplay('### 操作 > 面团的制作\n1. 搅拌\n### 面团的制作\n2. 发酵\n### 烘烤\n180℃烤30分钟');
 assert.deepEqual(result.find('h3').map(n=>n.textContent),['面团的制作','烘烤']);
 assert.ok(result.textContent.indexOf('面团的制作')<result.textContent.indexOf('搅拌'));
 assert.ok(result.textContent.includes('发酵'));
});
test('removing a historical heading leaves one continuous quote container',()=>{
 const result=context.answerDisplay('### 酵头的制作\n>1. 留好酵头。[1]\n\n### 酵头的制作\n>2. 用于制作。\n>3. 继续发酵。');
 assert.equal(result.find('blockquote').length,1);
 assert.equal(result.find('li').length,3);
});
test('adjacent source chunks are joined without losing image line positions',()=>{
 const rows=[{source:'a',section:'酵头',start_line:10,end_line:11,quote:'>1. 保存\n>继续'},
  {source:'a',section:'酵头',start_line:12,end_line:13,quote:'>2. 制作\n![图](a.jpg)'},
  {source:'a',section:'面团',start_line:14,end_line:14,quote:'搅拌'}];
 const merged=context.continuousSourceEvidence(rows);assert.equal(merged.length,2);
 assert.equal(merged[0].quote.split('\n')[13-merged[0].start_line],'![图](a.jpg)');
 assert.equal(context.readableText(merged[0].quote).find('blockquote').length,1);
 assert.equal(rows[0].quote,'>1. 保存\n>继续');
});
test('source cards sort chunks and avoid reinserting headings between steps',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified_source',sources:[{source:'bread.md',title:'面包'}],evidence:[
  {source:'bread.md',section:'操作 > 面团的制作',start_line:15,quote:'2. 发酵'},
  {source:'bread.md',section:'操作 > 面团的制作',start_line:10,quote:'### 面团的制作\n1. 搅拌'},
  {source:'bread.md',section:'材料 > 可选材料、工具及材料、工具的介绍',start_line:3,quote:'### 可选材料、工具及材料、工具的介绍\n量杯介绍'},
  {source:'bread.md',section:'材料 > 可选材料、工具及材料、工具的介绍',start_line:5,quote:'模具介绍'}]});
 assert.deepEqual(root.find('h2').map(n=>n.textContent),['面包']);
 assert.deepEqual(root.find('h3').map(n=>n.textContent),['可选材料、工具及材料、工具的介绍','面团的制作']);
 assert.ok(root.textContent.indexOf('搅拌')<root.textContent.indexOf('发酵'));
});
test('image failure retries once then offers manual recovery without a broken icon',()=>{
 const figure=context.recipeFigure({image_path:'data/菜谱/步骤 1.jpg',alt:'步骤图'},'菜谱');
 const image=figure.find('img')[0],button=figure.find('button')[0],status=figure.find('p')[0];
 assert.equal(image.width,800);assert.equal(image.height,600);
 assert.equal(new URL(image.src,context.location.href).searchParams.get('path'),'data/菜谱/步骤 1.jpg');
 image.onerror();assert.ok(image.src.includes('&retry=1'));assert.equal(button.hidden,true);
 image.onerror();assert.equal(image.hidden,true);assert.equal(button.hidden,false);
 button.onclick();assert.equal(image.hidden,false);assert.equal(button.hidden,true);
 image.onload();assert.equal(status.hidden,true);
});
test('opening the HTML directly goes to the running local app',()=>{
 const startup=script.slice(script.lastIndexOf("if(location.protocol==='file:')"));let destination=null,loaded=0;
 const page={location:{protocol:'file:',replace:url=>destination=url},loadHistory:()=>loaded++,loadModel:()=>{}};
 vm.runInNewContext(startup,page);assert.equal(destination,'http://127.0.0.1:8080/');assert.equal(loaded,0);
 page.location.protocol='http:';vm.runInNewContext(startup,page);assert.equal(loaded,1);
});
test('returning to a conversation keeps the reading paragraph after preceding content changes height',()=>{
 const area={scrollTop:500,getBoundingClientRect:()=>({top:64,bottom:500})};let paragraphTop=600;
 const paragraph={getBoundingClientRect:()=>({top:64+paragraphTop-area.scrollTop,bottom:64+paragraphTop-area.scrollTop+150})};
 const page={currentConversation:'bread',conversationPositions:new Map(),$:id=>id==='scrollArea'?area:{querySelectorAll:()=>[paragraph]}};
 vm.createContext(page);vm.runInContext(script.slice(script.indexOf('function readingNodes'),script.indexOf('async function openConversation')),page);
 page.rememberConversationPosition();paragraphTop+=300;area.scrollTop=0;
 page.restoreConversationPosition(page.conversationPositions.get('bread'));
 assert.equal(paragraph.getBoundingClientRect().top,164);
 assert.equal(area.scrollTop,800);
});
test('answer completion follows only when the user is still at the current turn',()=>{
 const page={$:()=>({getBoundingClientRect:()=>({top:64,bottom:500})})};
 vm.createContext(page);vm.runInContext(script.slice(script.indexOf('function readingNodes'),script.indexOf('async function openConversation')),page);
 const target=top=>({turn:{getBoundingClientRect:()=>({top})}});
 assert.equal(page.turnStartVisible(target(80)),true);
 assert.equal(page.turnStartVisible(target(800)),false);
 assert.equal(page.turnStartVisible(target(-100)),false);
});
test('typed recipe steps keep the reviewed conditions and images inside the matching numbered item',()=>{
 const root=new Node('div'),blocks=[
  {id:'a',kind:'ingredients',heading:'用料',text:'温水30℃，酵母2g',anchors:[{source:'bread.md'}]},
  {id:'b',kind:'step',heading:'酵头的制作',text:'混合后静置5分钟。',anchors:[{source:'bread.md'}]},
  {id:'c',kind:'step',heading:'酵头的制作',text:'盖布发酵45分钟，直到体积增大。',anchors:[{source:'bread.md'}]}];
 context.renderRecipeCards(root,{status:'verified',answer:'reviewed',short_answer:'reviewed',sources:[{title:'面包',source:'bread.md'}],display_blocks:blocks,
  images:[{source:'bread.md',answer_block_id:'c',image_path:'two.jpg',alt:'盖布发酵'},
          {source:'other.md',answer_block_id:'b',image_path:'wrong.jpg',alt:'错误图'},
          {source:'bread.md',image_path:'unbound.jpg',alt:'未绑定图'}]});
 const steps=root.find('ol')[0].find('li');
 assert.equal(steps.length,2);assert.equal(steps[0].find('img').length,0);
 assert.equal(steps[1].find('img')[0].alt,'盖布发酵');
 assert.equal(root.find('img').length,1);assert.ok(steps[1].textContent.includes('直到体积增大'));
 assert.equal(root.find('h3').filter(n=>n.textContent==='酵头的制作').length,1);
 assert.ok(root.textContent.includes('温水30℃，酵母2g'));
});
test('source fallback keeps ordered steps and places every photo at its source line',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',answer:'legacy answer',display_blocks:[],
  sources:[{source:'bread.md',title:'面包'}],display_source:[{source:'bread.md',section:'操作 > 酵头',start_line:3,end_line:6,
   quote:'1. 混合\n![图](one.jpg)\n2. 盖布\n![图](two.jpg)'}],
  images:[{source:'bread.md',source_line:4,image_path:'one.jpg',alt:'图一'},
          {source:'bread.md',source_line:6,image_path:'two.jpg',alt:'图二'},
          {source:'bread.md',source_line:40,image_path:'unrelated.jpg',alt:'工具介绍'}]});
 assert.equal(root.find('img').length,2);assert.equal(root.find('li').length,2);
 assert.ok(root.textContent.indexOf('混合')<root.textContent.indexOf('图一'));
 assert.ok(root.textContent.indexOf('图一')<root.textContent.indexOf('盖布'));
 assert.equal(root.textContent.includes('!['),false);
});
test('historical duplicate source-step blocks merge and retain images belonging to either part',()=>{
 const root=new Node('div'),anchor={source:'bread.md',start_line:86,end_line:86};
 const blocks=[{id:'a',kind:'step',heading:'酵头',text:'混合后静置5分钟。',step_number:1,anchors:[anchor]},
  {id:'b',kind:'step',heading:'酵头',text:'再拌入面粉，直到粘稠。',step_number:1,anchors:[anchor]}];
 context.renderAnswerBlocks(root,blocks,[{source:'bread.md',answer_block_id:'b',image_path:'one.jpg',alt:'拌匀'}]);
 assert.equal(root.find('li').length,1);assert.equal(root.find('li')[0].value,1);
 assert.equal(root.find('img').length,1);assert.ok(root.textContent.includes('直到粘稠'));
 assert.equal(blocks[0].text,'混合后静置5分钟。');
});
test('combining image and answer tools preserves reviewed text and image bindings in either order',()=>{
 const sources=[{source:'bread.md',title:'面包'}],photo={image_path:'one.jpg',source_line:10,source:'bread.md'};
 const qa={tool:'query_recipes',result:{status:'verified',sources,answer:'已核验步骤',display_blocks:[{id:'a'}],images:[{...photo,answer_block_id:'a'}]}};
 const pics={tool:'find_recipe_images',result:{status:'verified_source',sources,short_answer:'找到图片',images:[photo]}};
 for(const outputs of [[qa,pics],[pics,qa]]){
  const merged=context.combinedRecipeOutputs(outputs);
  assert.equal(merged.length,1);assert.equal(merged[0].result.answer,'已核验步骤');
  assert.equal(merged[0].result.status,'verified');assert.equal(merged[0].result.display_blocks.length,1);
  assert.equal(merged[0].result.images[0].answer_block_id,'a');
 }
});
test('failed historical answers never append unrelated recipe images',()=>{
 for(const status of ['degraded','validation_failed','insufficient','tool_failed']){
  const root=new Node('div');context.renderRecipeCards(root,{status,short_answer:'暂未核实',sources:[{source:'bread.md',title:'面包'}],images:[{source:'bread.md',image_path:'tool.jpg'}]});
  assert.equal(root.find('img').length,0);
 }
});
test('preference recommendations show matching reasons without fabricated inventory',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',recommendation_kind:'preference',recommendations:[{source:'chicken.md',title:'凉拌鸡丝',reason:'原料包含鸡肉；原文可选加辣：辣椒油。'}]});
 assert.ok(root.textContent.includes('原文可选加辣'));assert.ok(!root.textContent.includes('已有'));assert.ok(!root.textContent.includes('待备'));
});
test('rich recommendation cards contain an introduction, reason and ingredients without evidence clutter',()=>{
 const root=new Node('div');context.renderRecipeCards(root,{status:'verified',recommendation_kind:'preference',recommendations:[{source:'chicken.md',title:'宫保鸡丁',description:'鸡丁嫩滑，花生酥脆。',reason:'使用干辣椒，符合你想吃辣的要求。',main_ingredients:['鸡腿','花生'],notice:''}]});
 assert.ok(root.textContent.includes('鸡丁嫩滑'));assert.ok(root.textContent.includes('推荐理由：'));assert.ok(root.textContent.includes('主要用料：鸡腿、花生'));
 assert.ok(!root.textContent.includes('已有'));assert.equal(root.find('article')[0].class,'recipe-card recommendation-card');
});
test('inventory checks show direct titled sections without audit prose or decorative photos',()=>{
 const root=new Node('div'),anchor={source:'chicken.md',start_line:1,end_line:3};
 context.renderRecipeCards(root,{status:'verified',answer_layout:'inventory_check',answer:'已核对',short_answer:'材料不足',
  sources:[{source:'chicken.md',title:'宫保鸡丁'}],audit_notes:['原料表差异：仅用于核对'],
  display_blocks:[{id:'a',kind:'answer',heading:'能不能做',text:'材料还不齐。',anchors:[anchor]},
   {id:'b',kind:'answer',heading:'还缺哪些材料',text:'- 熟花生\n- 大葱',anchors:[anchor]}],
  images:[{source:'chicken.md',image_path:'chicken.jpg',answer_block_id:'a'}]});
 assert.deepEqual(root.find('h3').map(n=>n.textContent),['能不能做','还缺哪些材料']);
 assert.equal(root.find('li').length,2);assert.equal(root.find('img').length,0);
 assert.ok(!root.textContent.includes('原料表差异'));
});
test('inventory questions keep their existing progress classification',()=>{
 for(const q of ['我只有鸡蛋和西红柿，可以做西红柿炒鸡蛋吗？','我没有盐，可以做凉拌黄瓜吗？','我现在有鸡肉，还缺什么？'])assert.equal(context.inventoryQuestion(q),true,q);
 for(const q of ['我有鸡肉，推荐3道菜。','我有鸡肉，能保存多久？'])assert.equal(context.inventoryQuestion(q),false,q);
});

test('first inventory question exposes execution before a result arrives',()=>{
 const elements={welcome:new Node('div'),results:new Node('div')};
 const page={element:(tag,text)=>new Node(tag,text),$:id=>elements[id],focusTurn:()=>{}};
 vm.createContext(page);
 vm.runInContext(script.slice(script.indexOf('function inventoryQuestion'),script.indexOf('function timelineClock')),page);
 const turn=page.createTurn('我有鸡腿肉，推荐两道菜，说明还缺什么。');
 assert.equal(Boolean(turn.trace.hidden),false);
});

function performanceUI(){
 class UI extends Node {
  constructor(tag,text){super(tag,text);this.listeners={};this.replacements=0;}
  addEventListener(name,fn){this.listeners[name]=fn;}
  replaceChildren(...items){this.replacements++;this.children=[];this.append(...items);}
  querySelector(tag){return this.find(tag)[0];}
 }
 const page={element:(tag,text)=>new UI(tag,text),Date};vm.createContext(page);
 vm.runInContext(script.slice(script.indexOf('function timelineClock'),script.indexOf('function askQuestion')),page);
 return {page,UI};
}
test('closed timeline avoids list rendering and ticks preserve existing DOM nodes',()=>{
 const {page,UI}=performanceUI(),trace=new UI('details'),summary=new UI('summary'),list=new UI('ol');trace.append(summary,list);
 const target={trace},state={created:100,started:101,server_time:103,state:'running',progress:'检索中',events:[{message:'检索',at:102}]};
 page.drawTimeline(target,state);assert.equal(list.children.length,0);
 const label=summary.children[0],elapsed=summary.children[1];
 page.timelineClock(target,{...state,server_time:104});assert.equal(elapsed.textContent,'4.0 秒');
 assert.equal(summary.children[0],label);assert.equal(summary.replacements,1);
 trace.open=true;trace.listeners.toggle();assert.equal(list.children.length,3);
 const last=list.children[2];page.drawTimeline(target,{...state,server_time:105});
 assert.equal(list.children[2],last);assert.equal(last.find('time')[0].textContent,'3.0 s');
 const finished={...state,state:'done',server_time:108,finished:107};page.drawTimeline(target,finished);
 assert.equal(label.textContent,'查看执行过程');assert.equal(elapsed.textContent,'7.0 秒');
 trace.open=false;trace.listeners.toggle();trace.open=true;trace.listeners.toggle();
 assert.equal(elapsed.textContent,'7.0 秒');assert.equal(list.children[2].find('time')[0].textContent,'5.0 s');
});
test('completed timeline shows recorded understanding and service durations',()=>{
 const {page,UI}=performanceUI(),trace=new UI('details'),summary=new UI('summary'),list=new UI('ol');trace.append(summary,list);trace.open=true;
 const target={trace};page.drawTimeline(target,{created:100,started:101,finished:105,state:'done',progress:'完成',events:[],result:{trace:{stages:[
  {phase:'service',operation:'prepare',status:'ok',elapsed_ms:2100},
  {phase:'understanding',elapsed_ms:1250},
  {phase:'service',operation:'detail',status:'ok',elapsed_ms:600},
  {phase:'tool',tool:'get_recipe_detail',elapsed_ms:610},
  {phase:'service',operation:'persist',status:'ok',elapsed_ms:80},
  {phase:'overall',elapsed_ms:3800}]}}});
 assert.ok(list.textContent.includes('意图理解'),list.textContent);
 assert.ok(list.textContent.includes('1.3 s'),list.textContent);
 assert.ok(list.textContent.includes('读取菜谱原文'),list.textContent);
 assert.ok(list.textContent.includes('0.6 s'),list.textContent);
 assert.ok(list.textContent.includes('保存对话'),list.textContent);
 assert.ok(!list.textContent.includes('get_recipe_detail'));
 const times=list.find('time').map(n=>n.textContent);page.timelineClock(target,{created:100,finished:105});
 assert.deepEqual(list.find('time').map(n=>n.textContent),times);
});
test('evidence is built only on first expansion and survives repeated toggles',()=>{
 const {page,UI}=performanceUI(),details=new UI('details');let built=0;
 page.lazyDetails(details,()=>{built++;details.append(new UI('p','原文依据'));});
 assert.equal(details.children.length,0);details.listeners.toggle();assert.equal(built,0);
 details.open=true;details.listeners.toggle();assert.equal(built,1);
 details.open=false;details.listeners.toggle();details.open=true;details.listeners.toggle();
 assert.equal(built,1);assert.equal(details.textContent,'原文依据');
});
test('history rendering yields to interaction and stops stale conversation work',async()=>{
 let clock=0,yields=0;const messages=[],page={historyRequest:1,performance:{now:()=>clock+=9},
  createTurn:text=>{messages.push(text);return {trace:{},body:{append(){}}};},
  setTimeout:fn=>{yields++;page.historyRequest=2;fn();}};
 vm.createContext(page);vm.runInContext(script.slice(script.indexOf('async function renderHistoryMessages'),script.indexOf('async function openConversation')),page);
 const result=await page.renderHistoryMessages([{role:'user',content:'第一条'},{role:'user',content:'旧对话第二条'}],1);
 assert.equal(yields,1);assert.equal(result,null);assert.deepEqual(messages,['第一条']);
});
test('LangChain original and images render as one source card with one image',()=>{
 const sources=[{source:'bread.md',title:'面包',line:1}];
 const evidence=[{source:'bread.md',section:'操作',start_line:1,end_line:2,quote:'1. 搅拌。\n![图](one.jpg)'}];
 const image={image_path:'one.jpg',source_line:2,source:'bread.md',alt:'图'};
 const detail={tool:'get_recipe_detail',result:{status:'verified_source',sources,evidence,short_answer:'原文',images:[image]}};
 const pics={tool:'find_recipe_images',result:{status:'verified_source',sources,evidence,short_answer:'图片',images:[image]}};
 for(const outputs of [[detail,pics],[pics,detail]]){
  const merged=context.combinedRecipeOutputs(outputs);assert.equal(merged.length,1);
  assert.equal(merged[0].result.images.length,1);assert.equal(merged[0].result.short_answer,'原文');
  const root=new Node('div');context.renderRecipeCards(root,merged[0].result);
  assert.equal(root.find('article').length,1);assert.equal(root.find('img').length,1);
 }
});
test('LangChain verified answer takes precedence over same-source original',()=>{
 const sources=[{source:'bread.md',title:'面包',line:1}];
 const detail={tool:'get_recipe_detail',result:{status:'verified_source',sources,short_answer:'长原文'}};
 const answer={tool:'search_recipes',result:{status:'verified',sources,answer:'酵母2g，水温30℃，发酵45–60分钟。'}};
 for(const outputs of [[detail,answer],[answer,detail]]){
  const merged=context.combinedRecipeOutputs(outputs);assert.equal(merged.length,1);
  assert.equal(merged[0].result.answer,answer.result.answer);
 }
});
test('source image markup cannot produce broken root links',()=>{
 const result=context.readableText('原文配图：![酵头](./5-1成品.jpg)');
 assert.equal(result.find('a').length,0);
 assert.ok(result.textContent.includes('酵头'));
 assert.ok(!result.textContent.includes('!['));
});
test('SSE renders readable markdown and retains verified media across later tokens',async()=>{
 class LiveNode extends Node {constructor(tag,text){super(tag,text);this.style={};}replaceChildren(...items){this.children=[];this.append(...items);}}
 const ids=Object.fromEntries(['access','new','send','stop','form','messages','status','question'].map(id=>[id,new LiveNode(id)]));
 ids.access.value='';ids.question.value='查看做法';
 const frames=[['token',{text:'## 原料\n- **面粉**\n![配图](./bad.jpg)'}],['citations',{images:[{image_path:'data/HowToCook/a.jpg',alt:'原图'}]}],['token',{text:'\n1. 搅拌'}],['done',{degraded:false}]];
 const body=new ReadableStream({start(controller){for(const [kind,data] of frames)controller.enqueue(new TextEncoder().encode(`event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`));controller.close();}});
 const live={URL,AbortController,TextDecoder,document:{createElement:tag=>new LiveNode(tag),createTextNode:text=>new LiveNode('#text',text),getElementById:id=>ids[id]},location:{href:'http://127.0.0.1:8082/'},fetch:async url=>url==='/sessions'?{ok:true,json:async()=>({session_id:'s'})}:{ok:true,body}};
 vm.createContext(live);
 vm.runInContext(readFileSync(require('node:path').join(__dirname,'../app/recipe_markdown.js'),'utf8'),live);
 const sse=readFileSync(require('node:path').join(__dirname,'../app/langchain.html'),'utf8');
 assert.ok(sse.includes('src="/recipe_markdown.js"'));
 vm.runInContext(sse.match(/<script>([\s\S]*?)<\/script>/)[1],live);
 await ids.form.onsubmit({preventDefault(){}});
 assert.equal(ids.messages.find('h3')[0].textContent,'原料');
 assert.equal(ids.messages.find('strong')[0].textContent,'面粉');
 assert.equal(ids.messages.find('img').length,1);
 assert.equal(ids.messages.find('a').length,0);
 assert.ok(ids.messages.textContent.includes('搅拌'));
 assert.ok(!ids.messages.textContent.includes('!['));
 assert.equal(ids.status.textContent,'回答完成');
});

test('failed job timeline retains elapsed time and explains the failure',()=>{
 const {page,UI}=performanceUI(),trace=new UI('details'),summary=new UI('summary'),list=new UI('ol');trace.append(summary,list);trace.open=true;
 page.drawTimeline({trace},{created:100,started:101,finished:105,state:'failed',progress:'会话服务暂不可用',events:[],stages:[
  {phase:'service',operation:'persist',status:'error',elapsed_ms:600}
 ]});
 assert.equal(summary.textContent,'本次未完成5.0 秒');
 assert.match(list.textContent,/保存对话（失败）/);
 assert.match(list.textContent,/会话服务暂不可用/);
});

test('an unanswered historical turn does not look like an active submission',async()=>{
 const trace={hidden:false},page={historyRequest:1,performance:{now:()=>0},
  createTurn:()=>({trace,body:{append(){}}})};
 vm.createContext(page);
 vm.runInContext(script.slice(script.indexOf('async function renderHistoryMessages'),script.indexOf('async function openConversation')),page);
 await page.renderHistoryMessages([{role:'user',content:'执行中断的问题'}],1);
 assert.equal(trace.hidden,true);
});
