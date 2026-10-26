/* Registry evidence and explicitly selected current-project profile settings. */
(()=>{
  const $=id=>document.getElementById(id);
  const el=(tag,cls,value)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(value!=null)n.textContent=value;return n;};
  const labels={working:'Working',waiting:'Waiting',paused:'Paused',failed:'Failed',unknown:'Unknown'};
  let data=null,loading=false,failed=false,selected=null,focusCard=null,pickerOpen=false;
  const filters={project:'',team:'',mode:'all',query:''};
  const profileDrafts=new Map(),profileSelection=new Map();
  const date=v=>v&&Number.isFinite(Date.parse(v))?new Date(v).toLocaleString():officeI18n.t('Unknown');
  const age=v=>{if(!v||!Number.isFinite(Date.parse(v))||Date.parse(v)>Date.now())return officeI18n.t('Unknown');const m=Math.floor((Date.now()-Date.parse(v))/60000);return m<1?'less than a minute':m<60?m+' min':Math.floor(m/60)+' h '+m%60+' min';};
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
  function entry(parent,label,value){const row=el(parent.tagName==='BUTTON'?'span':'p','agent-field');row.append(el('strong',null,label+': '),document.createTextNode(value||officeI18n.t('Unknown')));parent.append(row);}
  function times(parent,a){
    entry(parent,officeI18n.t('Execution since'),a.execution?.state_since?date(a.execution.state_since)+' · '+age(a.execution.state_since):officeI18n.t('Unknown (no start timestamp)'));
    entry(parent,officeI18n.t('Observed at'),date(a.execution?.observed_at));
    entry(parent,officeI18n.t('Execution source'),a.execution?.source||'None');
    entry(parent,officeI18n.t('Source at'),date(a.execution?.source_at));
    entry(parent,officeI18n.t('Runtime source'),a.runtime?.source||'None');
    entry(parent,officeI18n.t('Runtime observed'),date(a.runtime?.source_at)+' · '+(a.runtime?.freshness==='fresh'&&fresh(a.runtime?.source_at,60)?officeI18n.t('Fresh'):officeI18n.t('Stale / unknown')));
  }
  function dependencies(parent,a){
    const deps=a.dependencies||[];
    if(!deps.length){entry(parent,officeI18n.t('Waiting on'),officeI18n.t('Unknown / no explicit dependency'));return;}
    for(const dep of deps){
      const row=el('p','agent-field');row.append(el('strong',null,'Waiting on: '),document.createTextNode(dependencyLabel(a,dep)+' '));
      const l=link(dep.url,dep.request_id?'Open handoff '+dep.request_id:'Open dependency');if(l)row.append(l);
      const peer=data?.agents?.find(b=>b.bot_key===dep.bot_key);
      if(peer){const b=el('button','secondary',officeI18n.t('View {name}',{name:peer.name}));b.type='button';b.onclick=()=>showDetail(peer.bot_key);row.append(b);}
      parent.append(row);
    }
  }
  function profilePanel(a){
    const panel=el('section','agent-profile-settings');panel.append(el('h3',null,officeI18n.t('Profile and validated memory')));
    panel.append(el('p','agent-note',officeI18n.t('Profile identity, learned memory and runtime permissions are separate. Editing a profile does not change runtime permissions.')));
    const targetKey=a.bot_key||a.bot_id;
    const file=profileSelection.get(targetKey)||'PROFILE.md';
    const key=targetKey+':'+file;
    let draft=profileDrafts.get(key);
    if(!draft){draft={content:null,text:'',loading:false,busy:false,error:''};profileDrafts.set(key,draft);}
    const tabs=el('div','agent-profile-tabs');
    for(const [id,label] of [['PROFILE.md','Profile'],['SOUL.md','Soul'],['memory','Memory']]){
      const button=el('button','secondary',officeI18n.t(label));button.type='button';button.dataset.profileFile=id;button.setAttribute('aria-pressed',String(file===id));
      button.onclick=()=>{profileSelection.set(targetKey,id);showDetail(a.bot_key,false);};tabs.append(button);
    }
    panel.append(tabs);
    const status=el('p','agent-note',draft.error||officeI18n.t(draft.loading?'Loading…':draft.content===null?'Select a profile section to load it.':'Drafts are kept separately for each agent and file.'));status.setAttribute('role','status');panel.append(status);
    const paint=()=>{if(selected===a.bot_key&&!$('agent-detail').hidden)showDetail(a.bot_key,false);};
    function matchBot(row){
      if(!row||typeof row!=='object')return false;
      if(row.bot===targetKey||row.bot===a.bot_key)return true;
      if(row.bot===a.bot_id){
        if(row.project_id){
          return row.project_id===a.project_id||(Boolean(a.is_current)&&row.project_id==='current');
        }
        return Boolean(a.is_current);
      }
      return false;
    }
    async function read(){
      if(draft.loading||draft.busy)return;draft.loading=true;draft.error='';paint();
      try{
        const row=await api('/api/settings/bots/'+encodeURIComponent(targetKey)+'?file='+encodeURIComponent(file));
        if(!matchBot(row)||row.file!==file||typeof row.content!=='string'||!row.version)throw Error(officeI18n.t('Profile response did not match the selected agent and file.'));
        if(file==='memory'&&row.readonly!==true)throw Error(officeI18n.t('Validated memory must remain read-only.'));
        draft.content=row;draft.text=row.content;
      }catch(error){
        const msg=error.message||'';
        draft.error=/not found|not installed/i.test(msg)?officeI18n.t('Profile folder or files are not installed for this agent.'):msg;
      }finally{draft.loading=false;paint();}
    }
    const loadButton=el('button','secondary',officeI18n.t(draft.content===null?'Load selected section':'Reload and discard this draft'));loadButton.type='button';loadButton.disabled=draft.loading||draft.busy;loadButton.onclick=read;panel.append(loadButton);
    if(draft.content!==null){
      panel.append(el('p','agent-note',draft.content.path||''));
      const input=el('textarea','agent-profile-content');input.value=draft.text;input.readOnly=file==='memory'||draft.content.readonly===true;input.disabled=draft.busy;input.setAttribute('aria-label',officeI18n.t(file==='memory'?'Validated learned memory':'Profile content'));
      input.oninput=()=>{draft.text=input.value;};panel.append(input);
      if(!input.readOnly){
        const save=el('button',null,officeI18n.t('Save changes'));save.type='button';save.disabled=draft.busy;
        save.onclick=async()=>{
          if(draft.busy)return;draft.busy=true;draft.error='';paint();
          try{
            const row=await api('/api/settings/bots/'+encodeURIComponent(targetKey),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file,content:draft.text,version:draft.content.version})});
            if(!matchBot(row)||row.file!==file||typeof row.content!=='string'||!row.version)throw Error(officeI18n.t('Profile response did not match the selected agent and file.'));
            draft.content=row;draft.text=row.content;draft.error=officeI18n.t('Saved. Applies to subsequent Office turns.');
          }catch(error){draft.error=error.message;}finally{draft.busy=false;paint();}
        };panel.append(save);
      }
    }
    return panel;
  }

  function showDetail(key,focusTarget=true){
    const a=data?.agents?.find(x=>x.bot_key===key);if(!a)return;
    selected=key;
    const dialog=$('agent-detail');
    const oldFocus=[...dialog.querySelectorAll('button,a')].indexOf(document.activeElement);
    dialog.replaceChildren();
    const head=el('div','agent-detail-head');
    const title=el('h2',null,a.name||a.bot_id);title.id='agent-detail-title';
    const who=el('div','agent-detail-who');
    const wrap=el('span','avatar-edit-wrap');
    const face=el('span','avatar');
    officeAvatars.fill(face,{key:a.bot_key,projectId:a.project_id,botId:a.bot_id,name:a.name||a.bot_id});
    const editBtn=el('button','avatar-edit-btn');
    editBtn.type='button';
    editBtn.id='avatar-edit-button';
    editBtn.setAttribute('aria-label',officeI18n.t('Change avatar'));
    editBtn.title=officeI18n.t('Change avatar');
    editBtn.setAttribute('aria-expanded',String(pickerOpen));
    editBtn.setAttribute('aria-controls','avatar-picker-panel');
    editBtn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
    wrap.append(face,editBtn);
    who.append(wrap,title);
    const close=el('button','secondary',officeI18n.t('Close'));close.type='button';close.onclick=closeDetail;head.append(who,close);dialog.append(head);
    editBtn.onclick=()=>{
      pickerOpen=!pickerOpen;
      showDetail(key,pickerOpen?'picker-first':'pencil');
    };
    if(pickerOpen){
      const picker=el('section','avatar-picker-panel');
      picker.id='avatar-picker-panel';
      picker.setAttribute('aria-label',officeI18n.t('Choose an avatar'));
      const pHead=el('div','avatar-picker-head');
      pHead.append(el('h3',null,officeI18n.t('Choose an avatar')));
      const pActions=el('div','avatar-picker-actions');
      const resetBtn=el('button','secondary',officeI18n.t('Reset to default'));
      resetBtn.type='button';
      resetBtn.onclick=()=>{
        officeAvatars.clearCustom(a.bot_key);
        pickerOpen=false;
        window.dispatchEvent(new CustomEvent('office-avatar-changed',{detail:{botKey:a.bot_key}}));
        showDetail(key,'pencil');
      };
      const pClose=el('button','secondary',officeI18n.t('Close'));
      pClose.type='button';
      pClose.onclick=()=>{pickerOpen=false;showDetail(key,'pencil');};
      pActions.append(resetBtn,pClose);
      pHead.append(pActions);
      picker.append(pHead);
      const grid=el('div','avatar-picker-grid');
      const currentIdx=officeAvatars.getCustom(a.bot_key);
      const defaultIdx=officeAvatars.index(a.project_id,a.bot_id);
      const effectiveIdx=currentIdx!=null?currentIdx:defaultIdx;
      for(let i=0;i<officeAvatars.count;i++){
        const opt=el('button','avatar-picker-option');
        opt.type='button';
        opt.setAttribute('aria-label',officeI18n.t('Avatar {number}',{number:i+1}));
        const isCurrent=i===effectiveIdx;
        opt.setAttribute('aria-pressed',String(isCurrent));
        if(isCurrent)opt.classList.add('selected');
        const optAvatar=el('span','avatar avatar-art');
        optAvatar.dataset.avatar=String(i);
        optAvatar.innerHTML=officeAvatars.svg(i);
        opt.append(optAvatar);
        opt.onclick=()=>{
          officeAvatars.setCustom(a.bot_key,i);
          pickerOpen=false;
          window.dispatchEvent(new CustomEvent('office-avatar-changed',{detail:{botKey:a.bot_key,avatarIndex:i}}));
          showDetail(key,'pencil');
        };
        grid.append(opt);
      }
      picker.append(grid);
      dialog.append(picker);
    }
    const e=effective(a);
    dialog.append(badge(officeI18n.t('Execution')+': '+(officeI18n.t(labels[e.state]||officeI18n.t('Unknown'))),e.state),badge(officeI18n.t('Task')+': '+(officeI18n.state(a.task?.workflow))+(a.task&&!e.taskFresh?' (historical / unknown)':''),'workflow'));
    for(const reason of e.reasons)dialog.append(badge(officeI18n.t(attentionLabels[reason]),'attention'));
    entry(dialog,officeI18n.t('Identity'),a.bot_id+' · '+a.role+' · '+a.project_name+' · '+(a.team_name||officeI18n.t('No team')));
    entry(dialog,officeI18n.t('Certainty'),e.state==='unknown'?officeI18n.t('Unknown'):a.execution?.certainty||officeI18n.t('Unknown'));
    entry(dialog,officeI18n.t('Current action'),currentAction(a,e));
    if(a.task?.recorded_action&&!currentAction(a,e))entry(dialog,officeI18n.t('Historical task action'),a.task.recorded_action);
    entry(dialog,officeI18n.t('Last recorded result'),a.last_result?.text);
    entry(dialog,officeI18n.t('Result timestamp'),date(a.last_result?.at));
    entry(dialog,officeI18n.t('Result source'),a.last_result?.source);
    const resultLink=link(a.last_result?.url,officeI18n.t('Open result evidence'));if(resultLink)dialog.append(resultLink);
    if(a.blocker)entry(dialog,officeI18n.t('Recorded blocker'),a.blocker);
    dependencies(dialog,a);
    const nav=el('div','agent-nav');
    for(const l of [link(a.task?.url,officeI18n.t('Open task')),link(a.link,officeI18n.t('Open appointed chat'))])if(l)nav.append(l);
    if(a.project_id&&a.project_id!=='current'){
      const p=el('button','secondary',officeI18n.t('View project'));p.type='button';p.onclick=()=>{dialog.hidden=true;officeProjects.open(a.project_id);};nav.append(p);
    }
    if(a.is_current&&a.thread_id){const chat=el('button','secondary',officeI18n.t('Personal conversation'));chat.type='button';chat.onclick=()=>{dialog.hidden=true;openPrivate(a.bot_id);};nav.append(chat);}
    dialog.append(nav);
    dialog.append(profilePanel(a));
    const evidence=el('section','agent-evidence');evidence.append(el('h3',null,officeI18n.t('Evidence and freshness')));times(evidence,a);
    entry(evidence,officeI18n.t('Task source'),a.task?.source);entry(evidence,officeI18n.t('Task updated'),date(a.task?.updated_at)+' · '+(e.taskFresh?officeI18n.t('Fresh'):officeI18n.t('Stale / unknown')));
    entry(evidence,officeI18n.t('Recorded task state'),a.task?.recorded_state);
    entry(evidence,officeI18n.t('Task state since'),date(a.task?.state_since));
    entry(evidence,officeI18n.t('Coverage'),a.coverage);
    entry(evidence,officeI18n.t('Boundary'),officeI18n.t('Runtime idle does not confirm availability. A receipt is not execution start; a completed turn or answered request is not task completion. Review and QA remain separate.'));
    dialog.append(evidence,el('h3',null,officeI18n.t('Recent recorded transitions')));
    const events=el('ol','agent-transitions');
    for(const event of a.transitions||[]){const row=el('li',null,date(event.at)+' · '+event.label+' · '+event.source+' ');const l=link(event.url,officeI18n.t('Evidence'));if(l)row.append(l);events.append(row);}
    if(!events.children.length)events.append(el('li',null,officeI18n.t('Unknown — no scoped transition timestamps recorded.')));
    dialog.append(events);
    window.officeShell?.activate('agent',a.name||a.bot_id,()=>showDetail(key));dialog.hidden=false;
    if(focusTarget==='pencil'||focusTarget==='edit')editBtn.focus();
    else if(focusTarget==='picker-first'){
      const firstOpt=dialog.querySelector('.avatar-picker-option.selected')||dialog.querySelector('.avatar-picker-option');
      if(firstOpt)firstOpt.focus();else editBtn.focus();
    }
    else if(focusTarget===true||focusTarget==='close')close.focus();
    else if(oldFocus>=0)dialog.querySelectorAll('button,a')[oldFocus]?.focus();
  }
  function renderCards(){
    const grid=$('agent-grid');if(!grid)return;
    grid.replaceChildren();
    const visibleScope=(data?.agents||[]).filter(a=>{
      // Dashboard selection never hides the full agent directory.
      return true;
    });
    const agents=visibleScope.filter(a=>{
      const e=effective(a);
      return (!filters.project||(a.project_id||'standalone')===filters.project)&&(!filters.team||(a.team_name||'none')===filters.team)&&
        (filters.mode==='all'||(filters.mode==='attention'?e.reasons.length:e.uncertain))&&
        (!filters.query||[a.name,a.role,officeI18n.t(a.role||''),a.bot_id,a.project_name,a.team_name].join(' ').toLocaleLowerCase().includes(filters.query.toLocaleLowerCase()));
    });
    $('agent-count').textContent=officeI18n.t('{count} agents · {attention} need attention · {unknown} stale / unknown',{count:agents.length,attention:visibleScope.filter(a=>effective(a).reasons.length).length,unknown:visibleScope.filter(a=>effective(a).uncertain).length});
    for(const a of agents){
      const e=effective(a),card=el('article','agent-card');
      const button=el('button','agent-card-open');button.type='button';button.dataset.agentKey=a.bot_key;
      button.setAttribute('aria-label',officeI18n.t('View evidence for {name}',{name:a.name||a.bot_id}));button.onclick=()=>{focusCard=a.bot_key;showDetail(a.bot_key);};
      const head=el('span','agent-card-heading'),avatar=el('span','avatar');officeAvatars.fill(avatar,{key:a.bot_key,projectId:a.project_id,botId:a.bot_id,name:a.name||a.bot_id});
      const name=el('span');name.append(el('strong',null,a.name||a.bot_id),el('small',null,officeI18n.t(a.role||a.bot_id)));head.append(avatar,name);button.append(head);
      button.append(el('span','agent-scope',a.project_name+' · '+(a.team_name||officeI18n.t('No team'))));
      const states=el('span','agent-states');states.append(badge(officeI18n.t('Execution')+': '+officeI18n.t(labels[e.state]||'Unknown'),e.state),badge(officeI18n.t('Task')+': '+(officeI18n.state(a.task?.workflow))+(a.task&&!e.taskFresh?' (historical / unknown)':''),'workflow'));button.append(states);
      entry(button,officeI18n.t('Current action'),currentAction(a,e));
      entry(button,officeI18n.t('Task'),a.task?.title);
      if(e.taskFresh&&a.task?.url){const ticket=link(a.task.url,officeI18n.t('Open task'));if(ticket)card.append(ticket);}
      entry(button,officeI18n.t('Last result'),a.last_result?.text);
      entry(button,officeI18n.t('Waiting on'),(a.dependencies||[]).map(d=>dependencyLabel(a,d)).join(', ')||null);
      for(const reason of e.reasons)button.append(badge(officeI18n.t(attentionLabels[reason]),'attention'));
      if(e.uncertain)button.append(badge(officeI18n.t('Stale / unknown coverage'),'unknown'));
      entry(button,officeI18n.t('Execution since'),a.execution?.state_since?age(a.execution.state_since):officeI18n.t('Unknown'));
      button.append(el('small','agent-source',officeI18n.t('Observed at')+' '+date(a.execution?.observed_at)+' · '+(a.execution?.source||officeI18n.t('No source'))));
      button.append(el('small','agent-source',officeI18n.t('Source at')+' '+date(a.execution?.source_at)));
      card.append(button);grid.append(card);
    }
    if(!agents.length)grid.append(el('p','empty',officeI18n.t('No matching agents. Clear the filters or refresh the registry.')));
  }
  function shell(){
    const view=$('agents-view');view.replaceChildren();
    const head=el('header','agent-page-head'),title=el('div');title.append(el('p','agent-eyebrow',officeI18n.t('BOTS / READ ONLY')),el('h1',null,officeI18n.t('Agents at a glance')));
    const refresh=el('button','secondary',officeI18n.t('Refresh evidence'));refresh.id='agents-refresh';refresh.type='button';refresh.onclick=load;
    const chats=el('button','secondary',officeI18n.t('Personal chats'));chats.id='agent-personal-chats';chats.type='button';chats.onclick=()=>window.officeShell.go('personal-directory');head.append(title,chats,refresh);view.append(head);
    const note=el('p','agent-note',officeI18n.t('Execution and task progress are separate. Missing evidence stays Unknown. Status reads never wake an agent.'));view.append(note);
    const toolbar=el('div','agent-filters');
    const select=(id,label,key,options)=>{
      const lab=el('label',null,label),s=el('select');s.id=id;
      for(const [v,t] of options){const o=el('option',null,t);o.value=v;s.append(o);}s.value=filters[key];s.onchange=()=>{filters[key]=s.value;renderCards();};lab.append(s);toolbar.append(lab);
    };
    const visibleScope=(data?.agents||[]);
    const projectOptions=[['',officeI18n.t('All projects')],...new Map(visibleScope.map(a=>[a.project_id||'standalone',a.project_name])).entries()];
    if(filters.project && !projectOptions.some(([val])=>val===filters.project)) filters.project='';
    select('agent-project',officeI18n.t('Project'),'project',projectOptions);
    const teamOptions=[['',officeI18n.t('All teams')],...new Map(visibleScope.map(a=>[a.team_name||'none',a.team_name||officeI18n.t('No team')])).entries()];
    if(filters.team && !teamOptions.some(([val])=>val===filters.team)) filters.team='';
    select('agent-team',officeI18n.t('Team'),'team',teamOptions);
    select('agent-mode',officeI18n.t('Show'),'mode',[['all',officeI18n.t('All agents')],['attention',officeI18n.t('Needs attention')],['unknown',officeI18n.t('Stale / unknown')]]);
    const lab=el('label',null,officeI18n.t('Search')),search=el('input');search.type='search';search.id='agent-search';search.value=filters.query;search.oninput=()=>{filters.query=search.value;renderCards();};lab.append(search);toolbar.append(lab);view.append(toolbar);
    const status=el('p','agent-note');status.id='agent-view-status';status.setAttribute('role','status');view.append(status);
    const count=el('p','agent-note');count.id='agent-count';count.setAttribute('role','status');view.append(count);
    const grid=el('div','agent-grid');grid.id='agent-grid';view.append(grid);
    renderCards();
    status.textContent=failed?officeI18n.t('Evidence refresh failed — retained records are historical; execution is Unknown unless paused.'):(data?.source_warnings||[]).join('; ')||data?.coverage||'';
  }
  async function load(){
    if(loading)return;loading=true;
    if($('agents-refresh'))$('agents-refresh').disabled=true;
    try{
      const r=await fetch('/api/registry/agents',{headers:{'X-Room-Token':document.querySelector('meta[name=room-token]').content}});
      if(!r.ok)throw Error(officeI18n.t('Evidence unavailable'));
      const next=await r.json();if(!Array.isArray(next.agents))throw Error(officeI18n.t('Invalid agent evidence'));
      data=next;failed=false;
    }catch(_){failed=true;}
    finally{loading=false;const refreshFocused=document.activeElement?.id==='agents-refresh';shell();if(refreshFocused)$('agents-refresh')?.focus();if(selected&&!$('agent-detail').hidden){if(data?.agents?.some(a=>a.bot_key===selected))showDetail(selected,false);else closeDetail();}}
  }
  function open(){
    if(window.officeShell&&!window.officeShell.canNavigate())return;window.officeShell?.activate('agents');officeMedia.stop();officeSpeech.stop();officeProjects.hide();officeView='agents';selectMenu('bots');
    for(const id of ['room-view','personal-view','session-detail'])$(id).hidden=true;
    $('agents-view').hidden=false;shell();load();
  }
  const dialog=el('section','agent-detail');dialog.hidden=true;dialog.id='agent-detail';dialog.setAttribute('aria-labelledby','agent-detail-title');
  document.querySelector('.workspace').append(dialog);
  function closeDetail(){pickerOpen=false;selected=null;open();const b=[...document.querySelectorAll('[data-agent-key]')].find(x=>x.dataset.agentKey===focusCard);if(b)b.focus();}
  window.officeAgents={open,refresh(){shell();},async show(key){
    if(!data?.agents?.some(a=>a.bot_key===key))await load();
    if(data?.agents?.some(a=>a.bot_key===key)){focusCard=key;showDetail(key);}
    else{open();$('agent-view-status').textContent=officeI18n.t('Agent evidence is unavailable for this roster binding.');}
  }};
  window.addEventListener('office-language-changed',()=>{shell();if(selected&&!dialog.hidden)showDetail(selected,false);});
  window.addEventListener('office-avatar-changed',()=>{
    renderCards();
    if(selected&&!dialog.hidden)showDetail(selected,false);
  });
  // Re-evaluate freshness while visible; never fetch, infer stalls or run agents.
  setInterval(()=>{if(!$('agents-view').hidden){const key=document.activeElement?.dataset.agentKey;renderCards();if(!dialog.hidden&&selected)showDetail(selected,false);else if(key)[...document.querySelectorAll('[data-agent-key]')].find(x=>x.dataset.agentKey===key)?.focus();}},30000);
})();
