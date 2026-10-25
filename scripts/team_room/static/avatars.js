/* Sample agent avatars: a self-contained SVG set (no network, no external assets) and a stable pseudo-random
   assignment seeded by project and bot id, so an agent keeps its face across reloads, pages and computers.
   Initials stay the fallback when the id is missing. Light and dark friendly: every avatar paints its own background. */
(()=>{
  const SKINS=['#f6d5bd','#efc3a2','#e2a97f','#c98d62','#a9703f','#7d4b2c','#fbe1d0','#d8a07a'];
  const HAIRS=['#2f2a28','#5a3a24','#8a5a2f','#c7933f','#d9c27a','#9a3b2b','#6f7480','#1f3a4d'];
  const BGS=['#cfe6d6','#d6e3f5','#f4dcc6','#e6d9f0','#f6e4a8','#c9e8e6','#f3cfd6','#d9e6b8','#cfd8ee','#f0d3b8','#c3e0c8','#e3d2ea'];
  const SHIRTS=['#3f7d5c','#4b78a8','#b5573b','#7b5aa6','#c28a2a','#2f8a8a','#a8436a','#5f8a2f','#3e4f8a','#8a5a3a'];
  const STYLES=['short','side','long','bun','curly','bald','cap','pony','afro'];
  const COUNT=36;
  const clamp=n=>Math.max(0,Math.min(255,Math.round(n)));
  const shade=(hex,k)=>{const n=parseInt(hex.slice(1),16);return '#'+[(n>>16)&255,(n>>8)&255,n&255].map(c=>clamp(c*(1-k)).toString(16).padStart(2,'0')).join('');};
  const tint=(hex,k)=>{const n=parseInt(hex.slice(1),16);return '#'+[(n>>16)&255,(n>>8)&255,n&255].map(c=>clamp(c+(255-c)*k).toString(16).padStart(2,'0')).join('');};
  // The i-th avatar: strides coprime to each palette length make the combinations differ in every trait.
  function spec(i){
    const style=STYLES[(i+Math.floor(i/STYLES.length))%STYLES.length];
    return {bg:BGS[(i*7+1)%BGS.length],skin:SKINS[(i*3+Math.floor(i/8))%SKINS.length],hair:HAIRS[(i*5+2)%HAIRS.length],
      style,shirt:SHIRTS[(i*3+4)%SHIRTS.length],glasses:i%4===1,beard:i%6===4&&style!=='long'&&style!=='pony'&&style!=='bun'};
  }
  function hairBack(s){
    const h=s.hair;
    switch(s.style){
      case 'long':return '<path d="M19 27c0-10.500 5-16 13-16s13 5.500 13 16v13c0 3.500-3 5-6.500 5h-13c-3.500 0-6.500-1.500-6.500-5z" fill="'+h+'"/>';
      case 'bun':return '<circle cx="32" cy="10.500" r="5.500" fill="'+h+'"/>';
      case 'pony':return '<path d="M42 21c8-1 10 8 6 16-1-5-3-8-6-9z" fill="'+h+'"/>';
      case 'curly':return ['22,24,6.500','26,16.500,6.500','32,13.500,6.500','38,16.500,6.500','42,24,6.500'].map(c=>{const [x,y,r]=c.split(',');return '<circle cx="'+x+'" cy="'+y+'" r="'+r+'" fill="'+h+'"/>';}).join('');
      case 'afro':return '<circle cx="32" cy="22" r="16.500" fill="'+h+'"/>';
      default:return '';
    }
  }
  function hairFront(s){
    const h=s.hair;
    switch(s.style){
      case 'short':case 'bun':case 'pony':case 'long':
        return '<path d="M20.500 27c-.5-9.500 4.500-15.500 11.500-15.500S44 17.500 43.500 27c-1.500-5-5.500-8-11.500-8s-10 3-11.500 8z" fill="'+h+'"/>';
      case 'side':return '<path d="M20.500 27c-1-10 5-16 12-16 7.500 0 12 5.500 11 14-3-6-8-8-13-7-4.500.5-8 3.500-10 9z" fill="'+h+'"/>';
      case 'curly':return ['26,19.500,4.200','32,17.500,4.200','38,19.500,4.200'].map(c=>{const [x,y,r]=c.split(',');return '<circle cx="'+x+'" cy="'+y+'" r="'+r+'" fill="'+h+'"/>';}).join('');
      case 'cap':return '<path d="M20 24.500c0-9 5.500-14.500 12-14.500s12 5.500 12 14.500z" fill="'+h+'"/><rect x="19" y="22.500" width="26" height="4.500" rx="2.200" fill="'+tint(h,.28)+'"/><circle cx="32" cy="9.500" r="2.600" fill="'+tint(h,.28)+'"/>';
      default:return '';
    }
  }
  function svg(index){
    const i=((Number(index)||0)%COUNT+COUNT)%COUNT,s=spec(i),skin=s.skin,deep=shade(skin,.1);
    const feat='<circle cx="27.500" cy="28.500" r="1.350" fill="#2b2522"/><circle cx="36.500" cy="28.500" r="1.350" fill="#2b2522"/>'+
      '<path d="M27.800 34.300q4.200 3.600 8.400 0" fill="none" stroke="#2b2522" stroke-width="1.400" stroke-linecap="round"/>'+
      '<circle cx="24.500" cy="32.500" r="2" fill="#e08a7a" opacity=".28"/><circle cx="39.500" cy="32.500" r="2" fill="#e08a7a" opacity=".28"/>';
    const glasses=s.glasses?'<g fill="none" stroke="#2b2522" stroke-width="1.100"><circle cx="27.500" cy="28.500" r="4"/><circle cx="36.500" cy="28.500" r="4"/><path d="M31.500 28.500h1"/></g>':'';
    const beard=s.beard?'<path d="M21.800 31c0 9 4.500 13 10.200 13s10.200-4 10.200-13c-2 3.600-5 5.200-10.200 5.200S23.800 34.600 21.800 31z" fill="'+s.hair+'" opacity=".92"/><path d="M28.200 34.800q3.800 3 7.600 0" fill="none" stroke="#2b2522" stroke-width="1.300" stroke-linecap="round"/>':'';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" focusable="false" aria-hidden="true">'+
      '<rect width="64" height="64" fill="'+s.bg+'"/>'+hairBack(s)+
      '<path d="M9 66c0-12.500 9.500-20.500 23-20.500S55 53.500 55 66z" fill="'+s.shirt+'"/>'+
      '<path d="M27 46l5 6 5-6z" fill="'+deep+'"/>'+
      '<rect x="28" y="37" width="8" height="11" rx="3" fill="'+deep+'"/>'+
      '<circle cx="21.200" cy="29.500" r="2.400" fill="'+skin+'"/><circle cx="42.800" cy="29.500" r="2.400" fill="'+skin+'"/>'+
      '<ellipse cx="32" cy="28.500" rx="11" ry="12.500" fill="'+skin+'"/>'+
      (s.style==='bald'?'<ellipse cx="28" cy="19.500" rx="4" ry="1.800" fill="#fff" opacity=".22"/>':'')+
      feat+beard+hairFront(s)+glasses+'</svg>';
  }
  // FNV-1a over UTF-16 code units, then a murmur3 finaliser so near-identical ids (agent1, agent2) spread evenly.
  function hash(text){
    let h=0x811c9dc5;
    for(let n=0;n<text.length;n++){h^=text.charCodeAt(n);h=Math.imul(h,0x01000193);}
    h^=h>>>16;h=Math.imul(h,0x85ebca6b);h^=h>>>13;h=Math.imul(h,0xc2b2ae35);h^=h>>>16;
    return h>>>0;
  }
  const seedOf=(projectId,botId)=>String(projectId==null?'':projectId)+':'+String(botId==null?'':botId);
  const index=(projectId,botId)=>hash(seedOf(projectId,botId))%COUNT;
  const initials=name=>String(name||'').split(/\s+/).filter(Boolean).map(x=>Array.from(x)[0]).slice(0,2).join('')||'?';
  // Put the agent's avatar into an existing element. key is the registry bot_key (project id + ':' + bot id), the same
  // seed as index(projectId, botId); initials remain when neither is known.
  function fill(node,{key,projectId,botId,name}){
    if(!node)return node;
    node.setAttribute('aria-hidden','true');
    const seed=key?String(key):(botId==null||botId===''?'':seedOf(projectId,botId));
    if(!seed){node.textContent=initials(name);return node;}
    const n=hash(seed)%COUNT;
    node.classList.add('avatar-art');node.dataset.avatar=String(n);
    node.innerHTML=svg(n);
    return node;
  }
  // The project Manager chosen by the registry (overview.managers: project id -> bot_key), never re-derived from roles or
  // ids. Null when the registry named none (no Manager, or several), so no face is implied.
  function managerOf(overview,projectId){
    const key=overview&&overview.managers&&overview.managers[projectId];
    if(typeof key!=='string')return null;
    return ((overview&&overview.bots)||[]).find(b=>b.bot_key===key&&b.project_id===projectId)||null;
  }
  const api={count:COUNT,index,svg,seed:seedOf,fill,initials,managerOf,traits:i=>spec(((Number(i)||0)%COUNT+COUNT)%COUNT)};
  if(typeof window!=='undefined')window.officeAvatars=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})();

