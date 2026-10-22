/* Read-only registry view. No transcript endpoints, send actions or model calls. */
(()=>{
  const $=id=>document.getElementById(id);
  const el=(tag,cls,value)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(value!=null)n.textContent=value;return n;};
  const labels={working:'Working',waiting:'Waiting',paused:'Paused',failed:'Failed',unknown:'Unknown'};
  let data=null,loading=false,failed=false,selected=null,focusCard=null;
  const filters={project:'',team:'',mode:'all',query:''};
  const date=v=>v&&Number.isFinite(Date.parse(v))?new Date(v).toLocaleString():'Unknown';
  const age=v=>{if(!v||!Number.isFinite(Date.parse(v))||Date.parse(v)>Date.now())return 'Unknown';const m=Math.floor((Date.now()-Date.parse(v))/60000);return m<1?'less than a minute':m<60?m+' min':Math.floor(m/60)+' h '+m%60+' min';};
  const fresh=(at,seconds)=>at&&Date.now()>=Date.parse(at)&&Date.now()-Date.parse(at)<=seconds*1000;
  const attentionLabels={failure:'Recorded task failure',blocker:'Explicit blocker',owner_decision:'Owner decision required'};
  function safe(value){
    if(typeof value!=='string')return null;
    if(/^codex:\/\/threads\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value))return value;
    try{const u=new URL(value);if(u.protocol==='https:'&&['github.com','tasks.google.com'].includes(u.hostname)&&!u.username&&!u.password&&!u.port)return u.href;}catch(_){}
    return null;
  }
  const link=(href,title)=>{const u=safe(href);if(!u)return null;const a=el('a','agent-link',title);a.href=u;if(u.startsWith('https:')){a.target='_blank';a.rel='noopener noreferrer';}return a;};
  function effective(a){
    const execution=a.execution||{};
    let state=execution.state||'unknown';
    const runtimeFresh=a.runtime?.freshness==='fresh'&&fresh(a.runtime?.source_at,60);
    const taskFresh=fresh(a.task?.updated_at,48*3600);
    if(state!=='paused'&&(failed||(state==='working'?!runtimeFresh:!taskFresh)))state='unknown';
    const reasons=!failed&&taskFresh&&a.attention?(a.attention_reasons||[]).filter(reason=>Object.hasOwn(attentionLabels,reason)):[];
    return {state,taskFresh,uncertain:failed||a.uncertain||state==='unknown'||!runtimeFresh||(a.task&&!taskFresh),reasons};
  }
  const currentAction=(a,e)=>!failed&&e.taskFresh&&e.state!=='paused'&&!['paused','completed','done','cancelled','failed'].includes(a.task?.workflow)?a.action:null;
  const dependencyLabel=(a,d)=>d.target+(!failed&&effective(a).taskFresh&&d.freshness==='fresh'&&fresh(d.source_at,48*3600)?'':' (historical / unknown)');
  function badge(text,state){return el('span','agent-badge agent-'+state,text);}
  function entry(parent,label,value){const row=el(parent.tagName==='BUTTON'?'span':'p','agent-field');row.append(el('strong',null,label+': '),document.createTextNode(value||'Unknown'));parent.append(row);}
  function times(parent,a){
    entry(parent,'Execution since',a.execution?.state_since?date(a.execution.state_since)+' · '+age(a.execution.state_since):'Unknown (no start timestamp)');
    entry(parent,'Observed at',date(a.execution?.observed_at));
    entry(parent,'Execution source',a.execution?.source||'None');
    entry(parent,'Source at',date(a.execution?.source_at));
    entry(parent,'Runtime source',a.runtime?.source||'None');
    entry(parent,'Runtime observed',date(a.runtime?.source_at)+' · '+(a.runtime?.freshness==='fresh'&&fresh(a.runtime?.source_at,60)?'Fresh':'Stale / unknown'));
  }
  function dependencies(parent,a){
    const deps=a.dependencies||[];
    if(!deps.length){entry(parent,'Waiting on','Unknown / no explicit dependency');return;}
    for(const dep of deps){
      const row=el('p','agent-field');row.append(el('strong',null,'Waiting on: '),document.createTextNode(dependencyLabel(a,dep)+' '));
      const l=link(dep.url,dep.request_id?'Open handoff '+dep.request_id:'Open dependency');if(l)row.append(l);
      const peer=data?.agents?.find(b=>b.bot_key===dep.bot_key);
      if(peer){const b=el('button','secondary','View '+peer.name);b.type='button';b.onclick=()=>showDetail(peer.bot_key);row.append(b);}
      parent.append(row);
    }
  }
  function showDetail(key,takeFocus=true){
    const a=data?.agents?.find(x=>x.bot_key===key);if(!a)return;
    selected=key;
    const dialog=$('agent-detail');
    const oldFocus=[...dialog.querySelectorAll('button,a')].indexOf(document.activeElement);
    dialog.replaceChildren();
    const head=el('div','agent-detail-head');
    const title=el('h2',null,a.name||a.bot_id);title.id='agent-detail-title';
    const close=el('button','secondary','Close');close.type='button';close.onclick=()=>dialog.close();head.append(title,close);dialog.append(head);
    const e=effective(a);
    dialog.append(badge('Execution: '+(labels[e.state]||'Unknown'),e.state),badge('Task: '+(a.task?.workflow||'Unknown')+(a.task&&!e.taskFresh?' (historical / unknown)':''),'workflow'));
    for(const reason of e.reasons)dialog.append(badge(attentionLabels[reason],'attention'));
    entry(dialog,'Identity',a.bot_id+' · '+a.role+' · '+a.project_name+' · '+(a.team_name||'No team'));
    entry(dialog,'Certainty',e.state==='unknown'?'Unknown':a.execution?.certainty||'Unknown');
    entry(dialog,'Current action',currentAction(a,e));
    if(a.task?.recorded_action&&!currentAction(a,e))entry(dialog,'Historical task action',a.task.recorded_action);
    entry(dialog,'Last recorded result',a.last_result?.text);
    entry(dialog,'Result timestamp',date(a.last_result?.at));
    entry(dialog,'Result source',a.last_result?.source);
    const resultLink=link(a.last_result?.url,'Open result evidence');if(resultLink)dialog.append(resultLink);
    if(a.blocker)entry(dialog,'Recorded blocker',a.blocker);
    dependencies(dialog,a);
    const nav=el('div','agent-nav');
    for(const l of [link(a.task?.url,'Open task'),link(a.link,'Open appointed chat')])if(l)nav.append(l);
    if(a.project_id&&a.project_id!=='current'){
      const p=el('button','secondary','View project');p.type='button';p.onclick=()=>{dialog.close();officeProjects.open(a.project_id);};nav.append(p);
    }
    if(a.is_current&&a.thread_id){const chat=el('button','secondary','Personal conversation');chat.type='button';chat.onclick=()=>{dialog.close();openPrivate(a.bot_id);};nav.append(chat);}
    dialog.append(nav);
    const evidence=el('section','agent-evidence');evidence.append(el('h3',null,'Evidence and freshness'));times(evidence,a);
    entry(evidence,'Task source',a.task?.source);entry(evidence,'Task updated',date(a.task?.updated_at)+' · '+(e.taskFresh?'Fresh':'Stale / unknown'));
    entry(evidence,'Recorded task state',a.task?.recorded_state);
    entry(evidence,'Task state since',date(a.task?.state_since));
    entry(evidence,'Coverage',a.coverage);
    entry(evidence,'Boundary','Runtime idle does not confirm availability. A receipt is not execution start; a completed turn or answered request is not task completion. Review and QA remain separate.');
    dialog.append(evidence,el('h3',null,'Recent recorded transitions'));
    const events=el('ol','agent-transitions');
    for(const event of a.transitions||[]){const row=el('li',null,date(event.at)+' · '+event.label+' · '+event.source+' ');const l=link(event.url,'Evidence');if(l)row.append(l);events.append(row);}
    if(!events.children.length)events.append(el('li',null,'Unknown — no scoped transition timestamps recorded.'));
    dialog.append(events);
    if(!dialog.open)dialog.showModal();
    if(takeFocus)close.focus();else if(oldFocus>=0)dialog.querySelectorAll('button,a')[oldFocus]?.focus();
  }
  function renderCards(){
    const grid=$('agent-grid');if(!grid)return;
    grid.replaceChildren();
    const visibleScope=(data?.agents||[]).filter(a=>{
      if(a.project_id && window.officeProjects && !window.officeProjects.isProjectVisible(a.project_id)) return false;
      return true;
    });
    const agents=visibleScope.filter(a=>{
      const e=effective(a);
      return (!filters.project||(a.project_id||'standalone')===filters.project)&&(!filters.team||(a.team_name||'none')===filters.team)&&
        (filters.mode==='all'||(filters.mode==='attention'?e.reasons.length:e.uncertain))&&
        (!filters.query||[a.name,a.role,a.bot_id,a.project_name,a.team_name].join(' ').toLowerCase().includes(filters.query.toLowerCase()));
    });
    $('agent-count').textContent=agents.length+' agents · '+visibleScope.filter(a=>effective(a).reasons.length).length+' need attention · '+visibleScope.filter(a=>effective(a).uncertain).length+' stale / unknown';
    for(const a of agents){
      const e=effective(a),card=el('article','agent-card');
      const button=el('button','agent-card-open');button.type='button';button.dataset.agentKey=a.bot_key;
      button.setAttribute('aria-label','View evidence for '+(a.name||a.bot_id));button.onclick=()=>{focusCard=a.bot_key;showDetail(a.bot_key);};
      const head=el('span','agent-card-heading'),avatar=el('span','avatar');avatar.textContent=(a.name||a.bot_id).split(/\s+/).map(x=>x[0]).slice(0,2).join('');avatar.setAttribute('aria-hidden','true');
      const name=el('span');name.append(el('strong',null,a.name||a.bot_id),el('small',null,a.role||a.bot_id));head.append(avatar,name);button.append(head);
      button.append(el('span','agent-scope',a.project_name+' · '+(a.team_name||'No team')));
      const states=el('span','agent-states');states.append(badge('Execution: '+labels[e.state],e.state),badge('Task: '+(a.task?.workflow||'Unknown')+(a.task&&!e.taskFresh?' (historical / unknown)':''),'workflow'));button.append(states);
      entry(button,'Current action',currentAction(a,e));
      entry(button,'Task',a.task?.title);
      entry(button,'Last result',a.last_result?.text);
      entry(button,'Waiting on',(a.dependencies||[]).map(d=>dependencyLabel(a,d)).join(', ')||null);
      for(const reason of e.reasons)button.append(badge(attentionLabels[reason],'attention'));
      if(e.uncertain)button.append(badge('Stale / unknown coverage','unknown'));
      entry(button,'Execution since',a.execution?.state_since?age(a.execution.state_since):'Unknown');
      button.append(el('small','agent-source','Observed '+date(a.execution?.observed_at)+' · '+(a.execution?.source||'No source')));
      button.append(el('small','agent-source','Source at '+date(a.execution?.source_at)));
      card.append(button);grid.append(card);
    }
    if(!agents.length)grid.append(el('p','empty','No matching agents. Clear the filters or refresh the registry.'));
  }
  function shell(){
    const view=$('agents-view');view.replaceChildren();
    const head=el('header','agent-page-head'),title=el('div');title.append(el('p','agent-eyebrow','BOTS / READ ONLY'),el('h1',null,'Agents at a glance'));
    const refresh=el('button','secondary','Refresh evidence');refresh.id='agents-refresh';refresh.type='button';refresh.onclick=load;head.append(title,refresh);view.append(head);
    const note=el('p','agent-note','Execution and task progress are separate. Missing evidence stays Unknown. Status reads never wake an agent.');view.append(note);
    const toolbar=el('div','agent-filters');
    const select=(id,label,key,options)=>{
      const lab=el('label',null,label),s=el('select');s.id=id;
      for(const [v,t] of options){const o=el('option',null,t);o.value=v;s.append(o);}s.value=filters[key];s.onchange=()=>{filters[key]=s.value;renderCards();};lab.append(s);toolbar.append(lab);
    };
    const visibleScope=(data?.agents||[]).filter(a=>!a.project_id||!window.officeProjects||window.officeProjects.isProjectVisible(a.project_id));
    const projectOptions=[['','All projects'],...new Map(visibleScope.map(a=>[a.project_id||'standalone',a.project_name])).entries()];
    if(filters.project && !projectOptions.some(([val])=>val===filters.project)) filters.project='';
    select('agent-project','Project','project',projectOptions);
    const teamOptions=[['','All teams'],...new Map(visibleScope.map(a=>[a.team_name||'none',a.team_name||'No team'])).entries()];
    if(filters.team && !teamOptions.some(([val])=>val===filters.team)) filters.team='';
    select('agent-team','Team','team',teamOptions);
    select('agent-mode','Show','mode',[['all','All agents'],['attention','Needs attention'],['unknown','Stale / unknown']]);
    const lab=el('label',null,'Search'),search=el('input');search.type='search';search.id='agent-search';search.value=filters.query;search.oninput=()=>{filters.query=search.value;renderCards();};lab.append(search);toolbar.append(lab);view.append(toolbar);
    const status=el('p','agent-note');status.id='agent-view-status';status.setAttribute('role','status');view.append(status);
    const count=el('p','agent-note');count.id='agent-count';count.setAttribute('role','status');view.append(count);
    const grid=el('div','agent-grid');grid.id='agent-grid';view.append(grid);
    renderCards();
    status.textContent=failed?'Evidence refresh failed — retained records are historical; execution is Unknown unless paused.':(data?.source_warnings||[]).join('; ')||data?.coverage||'';
  }
  async function load(){
    if(loading)return;loading=true;
    if($('agents-refresh'))$('agents-refresh').disabled=true;
    try{
      const r=await fetch('/api/registry/agents',{headers:{'X-Room-Token':document.querySelector('meta[name=room-token]').content}});
      if(!r.ok)throw Error('Evidence unavailable');
      const next=await r.json();if(!Array.isArray(next.agents))throw Error('Invalid agent evidence');
      data=next;failed=false;
    }catch(_){failed=true;}
    finally{loading=false;const refreshFocused=document.activeElement?.id==='agents-refresh';shell();if(refreshFocused)$('agents-refresh')?.focus();if(selected&&$('agent-detail').open){if(data?.agents?.some(a=>a.bot_key===selected))showDetail(selected,false);else $('agent-detail').close();}}
  }
  function open(){
    officeMedia.stop();officeSpeech.stop();officeProjects.hide();officeView='agents';selectMenu('bots');
    for(const id of ['room-view','personal-view','session-detail'])$(id).hidden=true;
    $('agents-view').hidden=false;shell();load();
  }
  const dialog=el('dialog','agent-detail');dialog.id='agent-detail';dialog.setAttribute('aria-labelledby','agent-detail-title');
  dialog.addEventListener('close',()=>{selected=null;const b=[...document.querySelectorAll('[data-agent-key]')].find(x=>x.dataset.agentKey===focusCard);if(b)b.focus();});document.body.append(dialog);
  window.officeAgents={open,refresh(){shell();}};
  // Re-evaluate freshness while visible; never fetch, infer stalls or run agents.
  setInterval(()=>{if(!$('agents-view').hidden){const key=document.activeElement?.dataset.agentKey;renderCards();if(dialog.open&&selected)showDetail(selected,false);else if(key)[...document.querySelectorAll('[data-agent-key]')].find(x=>x.dataset.agentKey===key)?.focus();}},30000);
})();
