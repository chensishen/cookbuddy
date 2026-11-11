// Text and verified media are rendered separately; never interpret source HTML.
function inlineMarkdown(parent,text){
 text=text.replace(/!\[([^\]]*)\]\([^\n)]*\)/g,(_,alt)=>"【配图："+(alt||"原图")+"】");
 const token=/(`+)([^`]+)\1|\*\*\*([^*]+)\*\*\*|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|\[([^\]]+)\]\(([^\s)]+)\)|\*([^*]+)\*/g;let offset=0,match;
 while((match=token.exec(text))){parent.append(document.createTextNode(text.slice(offset,match.index)));let node;
  if(match[2])node=element('code',match[2]);
  else if(match[3]){node=element('strong');node.append(element('em',match[3]));}
  else if(match[4]||match[5]){node=element('strong');inlineMarkdown(node,match[4]||match[5]);}
  else if(match[6])node=element('del',match[6]);
  else if(match[7]){node=element('span',match[7]);try{const url=new URL(match[8],location.href);if(['http:','https:'].includes(url.protocol)){node=element('a',match[7]);node.href=url.href;node.target='_blank';node.rel='noopener noreferrer';}}catch{}}
  else node=element('em',match[9]);parent.append(node);offset=token.lastIndex;
 }parent.append(document.createTextNode(text.slice(offset)));
}
function readableText(text){
 const body=element('div',undefined,'recipe-text'),lines=(text||'').replace(/\r\n/g,'\n').replace(/^\s*```(?:markdown|md)\s*\n([\s\S]*?)\n\s*```\s*$/i,'$1').split('\n');let list=null,lists=[],quote=null;
 const cells=line=>line.trim().replace(/^\||\|$/g,'').split(/(?<!\\)\|/).map(cell=>cell.trim().replace(/\\\|/g,'|'));
 for(let i=0;i<lines.length;i++){const line=lines[i];if(!line.trim()){list=null;quote=null;continue;}
  if(/^\s*(```|~~~)/.test(line)){list=null;quote=null;const fence=line.trim().slice(0,3),code=[];while(++i<lines.length&&!lines[i].trim().startsWith(fence))code.push(lines[i]);const pre=element('pre');pre.append(element('code',code.join('\n')));body.append(pre);continue;}
  if(line.includes('|')&&i+1<lines.length&&/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])){list=null;quote=null;const table=element('table'),head=element('thead'),tr=element('tr');for(const cell of cells(line)){const th=element('th');inlineMarkdown(th,cell);tr.append(th);}head.append(tr);table.append(head);i++;const tbody=element('tbody');while(i+1<lines.length&&lines[i+1].includes('|')&&lines[i+1].trim()){const row=element('tr');for(const cell of cells(lines[++i])){const td=element('td');inlineMarkdown(td,cell);row.append(td);}tbody.append(row);}table.append(tbody);body.append(table);continue;}
  const heading=line.match(/^\s*#{1,6}\s+(.+?)(?:\s+#+)?$/),bullet=line.match(/^\s*(?:([-*+])\s+|(\d+)[.、)](?!\d)\s*)(.*)/),quoted=line.match(/^\s*>\s?(.*)/);
  if(bullet){quote=null;const type=bullet[2]?'ol':'ul',indent=line.match(/^\s*/)[0].replace(/\t/g,'    ').length;if(!list)lists=[];while(lists.length&&lists.at(-1).indent>indent)lists.pop();if(lists.length&&lists.at(-1).indent===indent&&lists.at(-1).type!==type)lists.pop();if(!lists.length||lists.at(-1).indent<indent){list=element(type);if(bullet[2])list.start=Number(bullet[2]);(lists.at(-1)?.item||body).append(list);lists.push({indent,type,list,item:null});}const current=lists.at(-1),item=element('li');inlineMarkdown(item,bullet[3]);current.list.append(item);current.item=item;list=current.list;continue;}
  list=null;if(quoted){const block=[quoted[1]];while(i+1<lines.length){if(/^\s*>/.test(lines[i+1])){block.push(lines[++i].replace(/^\s*>\s?/,''));continue;}let next=i+1;while(next<lines.length&&!lines[next].trim())next++;if(next>i+1&&next<lines.length&&/^\s*>/.test(lines[next])){block.push('');i=next-1;continue;}break;}quote=element('blockquote');quote.append(readableText(block.join('\n')));body.append(quote);continue;}quote=null;
  if(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)){body.append(element('hr'));continue;}
  const node=element(heading?'h3':'p');inlineMarkdown(node,heading?heading[1].split(' > ').at(-1).trim():line);body.append(node);
 }return body;
}
