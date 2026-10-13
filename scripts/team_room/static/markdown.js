// A small text-only Markdown renderer. Raw HTML never enters the DOM as markup.
function officeMarkdown(source) {
  const body=document.createElement('div');body.className='message-content';
  function inline(parent,text,depth=0){
    if(depth>8){parent.append(document.createTextNode(text));return;}
    const tokens=/(`[^`\n]+`|\*\*[^\n]+?\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\)|https?:\/\/[^\s<>]+)/g;
    let cursor=0;
    for(const match of text.matchAll(tokens)){
      parent.append(document.createTextNode(text.slice(cursor,match.index)));
      const token=match[0];let element;
      if(token.startsWith('`')){element=document.createElement('code');element.textContent=token.slice(1,-1);}
      else if(token.startsWith('**')){element=document.createElement('strong');inline(element,token.slice(2,-2),depth+1);}
      else if(token.startsWith('*')){element=document.createElement('em');inline(element,token.slice(1,-1),depth+1);}
      else{
        const link=token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const url=link?link[2]:token.replace(/[.,;!?]+$/,'');
        let safe=false;try{safe=['http:','https:','mailto:'].includes(new URL(url).protocol);}catch{}
        if(safe){element=document.createElement('a');element.href=url;element.target='_blank';element.rel='noopener noreferrer';element.textContent=link?link[1]:url;}
        else{element=document.createTextNode(token);}
        parent.append(element);if(!link&&url!==token)parent.append(document.createTextNode(token.slice(url.length)));
        cursor=match.index+token.length;continue;
      }
      parent.append(element);cursor=match.index+token.length;
    }
    parent.append(document.createTextNode(text.slice(cursor)));
  }
  const lines=String(source||'').replace(/\r\n?/g,'\n').split('\n');
  let paragraph=[],list=null;
  function flush(){if(paragraph.length){const p=document.createElement('p');inline(p,paragraph.join('\n'));body.append(p);paragraph=[];}}
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(/^\s*```/.test(line)){
      flush();list=null;const code=[];
      while(++i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i]);
      const pre=document.createElement('pre'),node=document.createElement('code');node.textContent=code.join('\n');pre.append(node);body.append(pre);continue;
    }
    const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)$/),quote=line.match(/^>\s?(.*)$/);
    if(!line.trim()){flush();list=null;continue;}
    if(heading){flush();list=null;const h=document.createElement('h2');inline(h,heading[1]);body.append(h);continue;}
    if(item){flush();const kind=item[1]?'ul':'ol';if(!list||list.tagName.toLowerCase()!==kind){list=document.createElement(kind);if(kind==='ol')list.start=Number(line.match(/^\s*(\d+)/)[1]);body.append(list);}const li=document.createElement('li');inline(li,item[2]);list.append(li);continue;}
    list=null;
    if(quote){flush();const q=document.createElement('blockquote');inline(q,quote[1]);body.append(q);continue;}
    paragraph.push(line);
  }
  flush();return body;
}
