(()=>{
  const token=document.querySelector('meta[name=room-token]').content;
  const STATUS={working:'Working',needs_decision:'Needs decision',blocked:'Blocked',completed:'Completed',paused:'Paused',unknown:'Unknown',unconfigured:'Not configured'};
  let data=null,view='overview',loading=false,problem=null;
  const projectDrafts=new Map();
  const projectDraftImages=new Map();
  const pendingUploadsCount=new Map();
  const pendingAttempts=new Map();
  const chatStates=new Map();
  const projectChatLoading=new Set();
  const projectActivities=new Map();
  const projectActivityInflight=new Map();
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
  const safe=url=>typeof url==='string'&&/^(codex:\/\/threads\/[0-9a-f-]{36}|https:\/\/(github\.com|tasks\.google\.com)\/)/.test(url)?url:null;
  const link=(url,label)=>{const u=safe(url);if(!u)return el('span','pc-muted',label);const a=el('a','pc-link',label);a.href=u;if(u.startsWith('https:')){a.target='_blank';a.rel='noopener noreferrer';}return a;};
  const when=t=>{const d=new Date(t);return Number.isNaN(d.valueOf())?officeI18n.t('unknown time'):d.toLocaleString(officeI18n.language,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
  const stale=p=>{const age=Date.now()-Date.parse(p.source?.as_of);return !Number.isFinite(age)||age<0||age>(p.stale_after_hours||48)*3600e3;};
  const pending=p=>(p.decisions||[]).filter(d=>d.state==='pending');
  let registryData=null;
  let registryFailed=true,agentData=null,agentFailed=true;
  let inboxData=null,inboxFailed=false,inboxLoading=false,inboxSelected=null;
  const inboxFilters={project:'',kind:'',freshness:'',urgent:false,query:''};
  const VISIBILITY_KEY='office_visible_projects';
  let projSearchQuery='';

  function getVisibleProjectIds(){
    try{
      const raw=localStorage.getItem(VISIBILITY_KEY);
      if(raw){const p=JSON.parse(raw);if(Array.isArray(p))return new Set(p);}
    }catch(_){}
    return null;
  }
  function isProjectVisible(id){
    if(!id)return true;
    const s=getVisibleProjectIds();
    return s===null||s.has(id);
  }
  function setVisibleProjectIds(ids){
    try{
      if(ids===null)localStorage.removeItem(VISIBILITY_KEY);
      else localStorage.setItem(VISIBILITY_KEY,JSON.stringify(Array.from(ids)));
    }catch(_){}
    onVisibilityChanged();
  }
  function onVisibilityChanged(){
    render();
    renderList();
    if(typeof window.refreshOfficeDirectoryVisibility==='function'){
      window.refreshOfficeDirectoryVisibility();
    }
    if(window.officeAgents && typeof window.officeAgents.refresh==='function'){
      window.officeAgents.refresh();
    }
  }
  function allKnownProjects(){
    const list=[];const seen=new Set();
    const fromReg=registryData?.projects||[];
    for(const p of fromReg){
      if(!p.id||seen.has(p.id))continue;
      seen.add(p.id);
      list.push({id:p.id,name:p.name||p.id,root:p.root||'',status:p.status||'ok',reason:p.reason||null,is_current:Boolean(p.is_current),applicable:p.applicable!==false});
    }
    const fromSnap=data?.snapshot?.projects||[];
    for(const p of fromSnap){
      if(!p.id||seen.has(p.id))continue;
      seen.add(p.id);
      list.push({id:p.id,name:p.name||p.id,root:'',status:'unknown',reason:officeI18n.t('Not in project registry'),is_current:false,applicable:false});
    }
    return list;
  }
  const allProjects=()=>{
    const snapshot=data?.snapshot?.projects||[],seen=new Set(snapshot.map(p=>p.id));
    const missing=(['dashboard','inbox'].includes(view)?[]:registryData?.projects||[]).filter(p=>!seen.has(p.id)).map(p=>({
      id:p.id,name:p.name||p.id,status:'unknown',summary:officeI18n.t('Project summary unavailable.'),
      source:{available:false,label:officeI18n.t('Project registry'),as_of:null,error:p.reason||null}
    }));
    return [...snapshot,...missing];
  };
  const projects=()=>view==='dashboard'?allProjects().filter(p=>isProjectVisible(p.id)):allProjects();

  const INBOX_KINDS={decision:'Decision',request:'Request',blocker:'Blocker',setup:'Setup'};
  const INBOX_ACTIONS={appoint_team:'Appoint the team',record_issue:'Record an issue'};
  const kindLabel=kind=>officeI18n.t(INBOX_KINDS[kind]||'Other');
  const issueDrafts=new Map(),issueErrors=new Map(),issueBusy=new Set(),handoffCopied=new Set();
  const inboxIssue=url=>typeof url==='string'&&/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/issues\/[1-9][0-9]*$/.test(url)?url:null;
  const inboxFresh=item=>!inboxFailed&&item.freshness==='fresh'&&Number.isFinite(Date.parse(item.source_at))&&Date.now()>=Date.parse(item.source_at)&&Date.now()-Date.parse(item.source_at)<=48*3600e3;
  function inboxComplete(){const age=Date.now()-Date.parse(inboxData?.observed_at);return Boolean(inboxData&&!inboxFailed&&inboxData.complete&&inboxData.warnings.length===0&&Number.isFinite(age)&&age>=0&&age<=48*3600e3);}
  async function loadInbox(){
    if(inboxLoading)return;inboxLoading=true;
    try{
      const next=await api('/api/owner-inbox');
      if(next.schema!==1||!Array.isArray(next.items)||typeof next.complete!=='boolean'||!Array.isArray(next.warnings)||next.warnings.some(warning=>typeof warning!=='string')||typeof next.coverage!=='string'||!Number.isFinite(Date.parse(next.observed_at))||Date.parse(next.observed_at)>Date.now()||next.items.some(item=>!item||typeof item.id!=='string'||!item.id||typeof item.title!=='string'||typeof item.kind!=='string'||!item.kind||!Array.isArray(item.sources)||item.sources.some(source=>!source||typeof source.project_id!=='string'||typeof source.kind!=='string'))||new Set(next.items.map(item=>item.id)).size!==next.items.length)throw Error(officeI18n.t('Invalid owner inbox response.'));
      inboxData=next;inboxFailed=false;
    }catch(error){inboxFailed=true;}finally{inboxLoading=false;}
    if(document.getElementById('projects-view').hidden)return;
    if(view==='dashboard'){const aside=document.getElementById('owner-dashboard-attention');if(aside)paintInboxPreview(aside);}
    else if(view==='inbox'){
      const active=document.activeElement,id=active?.id,position=active?.selectionStart;render();
      if(id?.startsWith('owner-filter-')||id==='owner-issue-input'){const input=document.getElementById(id);input?.focus();if(typeof position==='number'&&input?.type==='search')input.setSelectionRange(position,position);}
    }
  }
  function inboxCoverage(parent){
    const notice=el('p','owner-inbox-coverage');notice.setAttribute('role','status');
    notice.textContent=officeI18n.t(inboxFailed?'Owner inbox unavailable. Retained items are historical.':!inboxData?'Loading owner inbox…':!inboxComplete()?'Owner inbox coverage is incomplete. Unknown is not zero.':'Only recorded open requests and pending decisions are included.');parent.append(notice);
    if(inboxData?.warnings.length){const warnings=el('ul','owner-inbox-warnings');for(const warning of inboxData.warnings)warnings.append(el('li',null,officeI18n.t(warning)));parent.append(warnings);}
    if(inboxData)notice.textContent+=' · '+officeI18n.t('Observed at')+' '+when(inboxData.observed_at);
  }
  function inboxRow(item){
    const row=el('li','owner-inbox-row');row.dataset.ownerItemId=item.id;
    const openItem=el('button','owner-inbox-title',item.title);openItem.type='button';openItem.onclick=()=>open('inbox',item.id);row.append(openItem);
    const meta=el('div','owner-inbox-meta');meta.append(el('span',null,item.project_name||item.project_id||officeI18n.t('Unknown')),el('span',null,item.agent||officeI18n.t('Unknown')),el('span','pc-tag',kindLabel(item.kind)));
    if(INBOX_ACTIONS[item.action])meta.append(el('span','pc-tag',officeI18n.t(INBOX_ACTIONS[item.action])));
    if(item.urgent)meta.append(el('span','pc-tag',officeI18n.t('Urgent')));
    meta.append(el('span','pc-tag',officeI18n.t(inboxFresh(item)?'Fresh':inboxFailed?'Historical / unavailable':item.freshness==='stale'?'Stale / unknown':'Unknown')));
    if(item.mixed_freshness)meta.append(el('span','pc-tag',officeI18n.t('Mixed source freshness')));
    if(item.tracking_required)meta.append(el('span','pc-tag',officeI18n.t('Issue tracking required')));
    if(item.due)meta.append(el('span',null,officeI18n.t('Due')+' '+when(item.due)));
    row.append(meta);
    if(item.kind==='blocker'&&item.blocked_work)row.append(el('p','owner-inbox-reason',item.blocked_work));
    return row;
  }
  function inboxIssueForm(item){
    const wrap=el('div','owner-issue'),input=el('input');input.id='owner-issue-input';input.type='url';input.placeholder='https://github.com/owner/repo/issues/1';input.setAttribute('aria-label',officeI18n.t('GitHub issue URL'));input.value=issueDrafts.get(item.id)||'';input.oninput=()=>issueDrafts.set(item.id,input.value);
    const go=el('button',null,officeI18n.t(issueBusy.has(item.id)?'Linking…':'Link issue'));go.type='button';go.id='owner-issue-link';go.disabled=issueBusy.has(item.id);go.onclick=()=>linkInboxIssue(item,input.value.trim());
    const error=el('p','np-error',issueErrors.get(item.id)?officeI18n.t(issueErrors.get(item.id)):'');error.id='owner-issue-error';error.setAttribute('role','alert');error.hidden=!issueErrors.get(item.id);
    wrap.append(input,go,error);return wrap;
  }
  async function linkInboxIssue(item,url){
    if(issueBusy.has(item.id))return;
    if(!inboxIssue(url)){issueErrors.set(item.id,'Enter a GitHub issue URL such as https://github.com/owner/repo/issues/1.');render();return;}
    issueBusy.add(item.id);issueErrors.delete(item.id);render();
    try{
      const json={method:'POST',headers:{'Content-Type':'application/json'}};
      if(item.kind==='setup')await api('/api/projects/pending-team/issue',{...json,body:JSON.stringify({project_id:item.project_id,issue_url:url})});
      else{
        const source=item.sources.find(s=>s.kind==='room_outcome'&&typeof s.room_id==='string'),room='/api/rooms/'+encodeURIComponent(source.room_id)+'/outcomes';
        const outcome=((await api(room)).items||[]).find(o=>o.id===source.id),stopped=outcome?.assignments?.find(a=>['blocked','failed'].includes(a.state)&&!a.issue_url);
        if(!stopped)throw Error('This blocker is no longer listed in its room. Refresh the inbox.');
        await api(room+'/'+encodeURIComponent(outcome.id)+'/assignments/'+encodeURIComponent(stopped.id)+'/issue',{...json,body:JSON.stringify({version:outcome.version,issue_url:url})});
      }
      issueDrafts.delete(item.id);
    }catch(error){issueErrors.set(item.id,error.message);}
    issueBusy.delete(item.id);await loadInbox();
    // The server derives an item's id from its issue link, so a linked item reappears under a new id; keep the owner on it.
    if(inboxSelected===item.id&&!inboxData?.items.some(row=>row.id===item.id)){
      const same=inboxData?.items.find(row=>row.kind===item.kind&&row.project_id===item.project_id&&row.sources.some(s=>item.sources.some(o=>o.kind===s.kind&&o.id===s.id)));
      if(same)inboxSelected=same.id;
    }
    if(view==='inbox')render();
  }
  function handoffBox(item){
    const box=el('div','owner-handoff'),area=el('textarea','np-prompt');area.id='owner-handoff';area.readOnly=true;area.rows=5;area.value=item.blocked_work;
    const copy=el('button','secondary',officeI18n.t(handoffCopied.has(item.id)?'Copied':'Copy instruction'));copy.type='button';copy.id='owner-handoff-copy';
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(area.value);}catch{area.select();}handoffCopied.add(item.id);copy.textContent=officeI18n.t('Copied');};
    box.append(el('strong',null,officeI18n.t('Instruction for Codex')),area,copy);return box;
  }
  function paintInboxPreview(parent){
    parent.replaceChildren(el('h2',null,officeI18n.t('Waiting for me')));inboxCoverage(parent);
    const items=inboxData?.items||[],urgent=items.filter(item=>item.urgent),shown=(urgent.length?urgent:items).slice(0,5);
    const list=el('ul','owner-inbox-list');shown.forEach(item=>list.append(inboxRow(item)));parent.append(list);
    if(!items.length)parent.append(empty(officeI18n.t(inboxComplete()?'No recorded owner items.':inboxFailed?'Status unavailable':!inboxData?'Loading…':'Owner wait count is unknown.')));
    const all=el('button','secondary',officeI18n.t('View all owner items'));all.type='button';all.onclick=()=>open('inbox');parent.append(all);
  }
  function renderInbox(body){
    inboxCoverage(body);
    const refresh=el('button','secondary',officeI18n.t('Refresh'));refresh.type='button';refresh.disabled=inboxLoading;refresh.onclick=loadInbox;
    if(inboxSelected){
      body.append(refresh);
      const item=inboxData?.items.find(row=>row.id===inboxSelected);
      if(!item){body.append(empty(officeI18n.t('This item is no longer listed. Check its source; no resolution is inferred.')));return;}
      const detail=el('section','owner-inbox-detail');detail.dataset.ownerItemId=item.id;
      const back=el('button','secondary',officeI18n.t('Back to owner inbox'));back.type='button';back.onclick=()=>open('inbox');detail.append(back);
      detail.append(el('h2',null,item.title),el('p',null,(item.project_name||item.project_id)+' · '+(item.agent||officeI18n.t('Unknown'))));
      detail.append(el('p','owner-detail-state',kindLabel(item.kind)+' · '+officeI18n.t(inboxFresh(item)?'Fresh':inboxFailed?'Historical / unavailable':'Stale / unknown')+(item.urgent?' · '+officeI18n.t('Urgent'):'')+(item.mixed_freshness?' · '+officeI18n.t('Mixed source freshness'):'')+(item.tracking_required?' · '+officeI18n.t('Issue tracking required'):'')+(item.due?' · '+officeI18n.t('Due')+' '+when(item.due):'')));
      if(item.blocked_work&&item.kind==='setup')detail.append(handoffBox(item));
      else if(item.blocked_work)detail.append(el('p','owner-blocked-work',(item.kind==='blocker'?officeI18n.t('Reason')+': ':'')+item.blocked_work));
      const sources=el('ul','owner-item-sources');
      for(const source of item.sources){
        const sourceRow=el('li');sourceRow.append(el('strong',null,(source.project_id||'')+' · '+(source.kind||'')+' · '+(source.id||'')),el('p','pc-muted',officeI18n.t('Source at')+' '+(source.source_at?when(source.source_at):officeI18n.t('Unknown'))+' · '+officeI18n.t(source.freshness==='fresh'&&!inboxFailed?'Fresh':source.freshness==='stale'?'Stale / unknown':'Unknown')));
        const url=inboxIssue(source.url);if(url)sourceRow.append(link(url,officeI18n.t('Open source issue')));sources.append(sourceRow);
      }
      detail.append(sources);
      const url=!item.tracking_required?inboxIssue(item.issue_url):null;
      if(url)detail.append(link(url,officeI18n.t(item.kind==='setup'||item.kind==='blocker'?'Open tracking issue':'Open source issue')));
      const roomSource=item.kind==='blocker'&&item.sources.some(s=>s.kind==='room_outcome'&&typeof s.room_id==='string');
      if(item.tracking_required&&(item.kind==='setup'||roomSource))detail.append(inboxIssueForm(item));
      detail.append(el('p','owner-inbox-action-note',officeI18n.t(item.action==='appoint_team'?'Open the new project in Codex and give Codex the instruction above. Office cannot appoint the team. This item disappears when a Manager is appointed.':url?'Resolve or answer in the source issue. Office cannot resolve or synchronize this item.':'Create or link a tracking issue in the authoritative project. Include the reason, blocked work, unblock owner, requested action, resume condition, next check and evidence. Office cannot create or resolve it here.')));
      detail.append(el('p','pc-muted',officeI18n.t('Refreshing reads the source again. A click does not complete the work.')));body.append(detail);return;
    }
    const toolbar=el('div','owner-inbox-filters');toolbar.append(refresh);
    const select=(key,label,choices)=>{const wrapper=el('label',null,officeI18n.t(label)),input=el('select');input.id='owner-filter-'+key;for(const [value,text] of choices){const option=el('option',null,text);option.value=value;input.append(option);}if(!choices.some(([value])=>value===inboxFilters[key]))inboxFilters[key]='';input.value=inboxFilters[key];input.onchange=()=>{inboxFilters[key]=input.value;paintRows();};wrapper.append(input);toolbar.append(wrapper);};
    const projectOptions=new Map((inboxData?.items||[]).flatMap(item=>[...item.sources.map(source=>[source.project_id,source.project_id]),[item.project_id,item.project_name||item.project_id]]).filter(([id])=>id));
    select('project','Project',[['',officeI18n.t('All projects')],...projectOptions.entries()]);
    select('kind','Kind',[['',officeI18n.t('All')],...Object.keys(INBOX_KINDS).map(kind=>[kind,kindLabel(kind)]),...((inboxData?.items||[]).some(item=>!INBOX_KINDS[item.kind])?[['other',officeI18n.t('Other')]]:[])]);
    select('freshness','Source',[['',officeI18n.t('All')],['fresh',officeI18n.t('Fresh')],['unknown',officeI18n.t('Stale / unknown')]]);
    const query=el('input');query.type='search';query.id='owner-filter-query';query.placeholder=officeI18n.t('Search owner items');query.setAttribute('aria-label',officeI18n.t('Search owner items'));query.value=inboxFilters.query;query.oninput=()=>{inboxFilters.query=query.value;paintRows();};toolbar.append(query);
    const urgent=el('label'),checkbox=el('input');checkbox.type='checkbox';checkbox.id='owner-filter-urgent';checkbox.checked=inboxFilters.urgent;checkbox.onchange=()=>{inboxFilters.urgent=checkbox.checked;paintRows();};urgent.append(checkbox,document.createTextNode(officeI18n.t('Urgent only')));toolbar.append(urgent);body.append(toolbar);
    const count=el('p','owner-inbox-count'),list=el('ul','owner-inbox-list');body.append(count,list);
    function paintRows(){
      const items=(inboxData?.items||[]).filter(item=>(!inboxFilters.project||item.project_id===inboxFilters.project||item.sources.some(source=>source.project_id===inboxFilters.project))&&(!inboxFilters.kind||(inboxFilters.kind==='other'?!INBOX_KINDS[item.kind]:item.kind===inboxFilters.kind))&&(!inboxFilters.urgent||item.urgent)&&(!inboxFilters.freshness||(inboxFilters.freshness==='fresh'?inboxFresh(item):!inboxFresh(item)))&&(!inboxFilters.query||[item.title,item.project_name,item.agent,item.blocked_work].join(' ').toLocaleLowerCase().includes(inboxFilters.query.toLocaleLowerCase())));
      count.textContent=!inboxComplete()&&!items.length?officeI18n.t('Owner wait count is unknown.'):officeI18n.t(inboxComplete()?'{count} recorded owner items':'{count} recorded items · total unknown',{count:items.length});list.replaceChildren(...items.map(inboxRow));
      if(!items.length)list.append(empty(officeI18n.t(inboxComplete()?(inboxData.items.length?'No matching owner items.':'No recorded owner items.'):'Owner wait count is unknown.')));
    }
    paintRows();if(inboxData)body.append(el('p','pc-muted',officeI18n.t(inboxData.coverage)));
  }

  async function load(){
    if(loading)return;loading=true;
    registryFailed=true;agentFailed=true;
    try{
      const [rProjects,rOverview,rAgents]=await Promise.allSettled([
        fetch('/api/projects',{headers:{'X-Room-Token':token}}),
        fetch('/api/registry/overview',{headers:{'X-Room-Token':token}}),
        fetch('/api/registry/agents',{headers:{'X-Room-Token':token}})
      ]);
      if(rProjects.status==='fulfilled'){
        const j=await rProjects.value.json();
        if(!rProjects.value.ok)throw Error(j.error||officeI18n.t('Projects unavailable'));
        if(j.available){data=j;problem=null;}
        else if(data?.available)problem=j.error||'Project status is unavailable';
        else{data=j;problem=null;}
      }else{
        const error=officeI18n.t('Projects unavailable');
        if(data?.available)problem=error;else{data={available:false,error};problem=null;}
      }
      registryFailed=true;agentFailed=true;
      if(rOverview.status==='fulfilled'&&rOverview.value.ok){
        registryData=await rOverview.value.json();
        registryFailed=!Array.isArray(registryData.teams)||!Array.isArray(registryData.bots);
      }
      if(rAgents.status==='fulfilled'&&rAgents.value.ok){
        agentData=await rAgents.value.json();agentFailed=!Array.isArray(agentData.agents);
      }
    }catch(e){
      if(data?.available)problem=e.message;
      else{data={available:false,error:e.message};problem=null;}
    }finally{loading=false;}
    renderList();
    if(!document.getElementById('projects-view').hidden&&view!=='inbox'){
      if(view!=='overview'&&view!=='inbox'&&view!=='dashboard'&&document.getElementById('pm-chat-container')){
        updateProjectLeftCol(view);
      }else{
        render();
      }
    }
    if(view!=='overview'&&view!=='inbox'&&view!=='dashboard'){
      loadProjectChat(view);
      loadProjectActivity(view);
    }
  }

  async function loadProjectChat(pid){
    if(projectChatLoading.has(pid))return;projectChatLoading.add(pid);
    try{
      const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/chat',{headers:{'X-Room-Token':token}});
      const j=await r.json();
      if(!r.ok){
        chatStates.set(pid,{available:false,error:j.error||('Chat error HTTP '+r.status)});
      }else{
        chatStates.set(pid,j);
      }
      if(!document.getElementById('projects-view').hidden&&view===pid)updateChatPane(pid);
    }catch(e){
      chatStates.set(pid,{available:false,error:e.message});
      if(!document.getElementById('projects-view').hidden&&view===pid)updateChatPane(pid);
    }finally{projectChatLoading.delete(pid);}
  }

  async function loadProjectActivity(pid){
    if(projectActivityInflight.has(pid)) return projectActivityInflight.get(pid);
    const prom = (async ()=>{
      try{
        const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/activity',{headers:{'X-Room-Token':token}});
        if(!r.ok) throw new Error('HTTP '+r.status);
        const j=await r.json();
        projectActivities.set(pid,j);
      }catch(err){
        const prev=projectActivities.get(pid);
        projectActivities.set(pid,{
          ...(prev||{}),
          project_id: pid,
          status: 'unknown',
          error: 'Activity refresh failed',
          stale: Boolean(prev),
          last_active_bots: (prev?.status==='active') ? (prev.active_bots||[]) : (prev?.last_active_bots||[]),
          fetched_at: prev?.fetched_at || null,
          source: prev?.source || null,
          failed_at: new Date().toISOString()
        });
      }finally{
        if(!document.getElementById('projects-view').hidden){
          if(view===pid) updateProjectHeaderActivity(pid);
          else if(view==='overview') updateOverviewCardActivity(pid);
        }
        projectActivityInflight.delete(pid);
      }
    })();
    projectActivityInflight.set(pid, prom);
    return prom;
  }

  function renderActivityPill(p,container){
    const act=projectActivities.get(p.id);
    if(!act)return;
    if(act.status==='active'&&!act.stale){
      let label=officeI18n.t('Active')+(act.active_bots?.length?' ('+act.active_bots.join(', ')+')':'');
      if(act.partial_failure) label+=' (partial)';
      const pill=el('span','pc-activity pc-act-active',label);
      let titleText='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
      if(act.partial_failure) titleText+=' · partial coverage: some bots unknown';
      pill.title=titleText;
      container.append(pill);
      if(act.partial_failure){
        container.append(el('span','pc-flag pc-warn',officeI18n.t('Partial coverage')));
      }
      if(p.status==='paused'){
        container.append(el('span','pc-flag pc-warn',officeI18n.t('Contradiction: active bot')));
      }
    }else if(act.status==='inactive'&&!act.stale){
      const pill=el('span','pc-activity pc-act-inactive','Inactive');
      if(act.fetched_at) pill.title='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
      container.append(pill);
    }else{
      let label='Activity unknown';
      if(act.stale) label+=' (stale)';
      const pill=el('span','pc-activity pc-act-unknown',label);
      if(act.fetched_at) pill.title='Last checked '+when(act.fetched_at)+' via '+(act.source||'relay');
      container.append(pill);
    }
  }

  function updateOverviewCardActivity(pid){
    const container=document.getElementById('pc-card-badges-'+pid);
    const p=projects().find(x=>x.id===pid);
    if(!container||!p)return;
    container.replaceChildren();
    container.append(badge(p));
    renderActivityPill(p,container);
  }

  function updateProjectHeaderActivity(pid){
    const p=projects().find(x=>x.id===pid);
    if(!p)return;
    const sub=document.getElementById('pm-header-subtitle');
    if(!sub)return;
    sub.replaceChildren();
    sub.append(el('span',null,officeI18n.t(STATUS[p.status])+(p.manager?' · '+p.manager:'')));
    const act=projectActivities.get(pid);
    if(act){
      if(act.status==='active'&&!act.stale){
        let label=officeI18n.t('Active')+(act.active_bots?.length?' ('+act.active_bots.join(', ')+')':'');
        if(act.partial_failure) label+=' (partial)';
        const pill=el('span','pc-activity pc-act-active',label);
        let titleText='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
        if(act.partial_failure) titleText+=' · partial coverage: some bots unknown';
        pill.title=titleText;
        sub.append(pill);
        if(act.partial_failure){
          sub.append(el('span','pc-flag pc-warn',officeI18n.t('Partial coverage')));
          sub.append(el('span','pc-provenance','checked '+when(act.fetched_at)+' via '+(act.source||'relay')+' (partial: some bots unknown)'));
        }
        if(p.status==='paused'){
          const contra=el('span','pc-contradiction-flag','Paused · Active bot contradiction: '+(act.active_bots?.join(', ')||'bot')+' active in Codex');
          sub.append(contra);
        }
      }else if(act.status==='inactive'&&!act.stale){
        const pill=el('span','pc-activity pc-act-inactive',officeI18n.t('Inactive (all bots idle)'));
        if(act.fetched_at) pill.title='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
        sub.append(pill);
      }else{
        let label='Activity unknown';
        if(act.stale) label+=' (stale)';
        const pill=el('span','pc-activity pc-act-unknown',label);
        sub.append(pill);
        if(act.stale&&act.last_active_bots?.length){
          sub.append(el('span','pc-provenance','(last seen: '+act.last_active_bots.join(', ')+')'));
        }
        if(act.fetched_at){
          sub.append(el('span','pc-provenance','checked '+when(act.fetched_at)+' via '+(act.source||'relay')+(act.partial_failure?' (partial)':'')));
        }
      }
    }
  }

  function freshness(p){const box=el('div','pc-source');const s=p.source;box.append(link(s.url,s.label));box.append(el('span','pc-muted',' · '+(s.as_of?officeI18n.t('as of')+' '+when(s.as_of):officeI18n.t('no timestamp'))));if(!s.available||problem)box.append(el('span','pc-flag pc-warn',officeI18n.t('Source unavailable')+((problem||s.error)?': '+(problem||s.error):'')));else if(s.error)box.append(el('span','pc-flag pc-warn',s.error));else if(stale(p))box.append(el('span','pc-flag pc-warn',officeI18n.t('Stale / unknown')));return box;}
  function badge(p){
    return el('span','pc-status pc-'+p.status,officeI18n.t(STATUS[p.status]||p.status));
  }
  function decisionItem(d,p){
    const li=el('li','pc-decision'+(d.urgency==='urgent'?' pc-urgent':''));
    li.append(el('strong',null,d.question));
    if(p)li.append(el('span','pc-tag',p.name));
    if(d.context)li.append(el('p',null,d.context));
    const meta=el('div','pc-muted',(d.urgency==='urgent'?officeI18n.t('Urgent')+' · ':'')+officeI18n.t('Asked by')+' '+d.requested_by+' · '+when(d.requested_at)+(d.due?' · due '+when(d.due):'')+' · ');
    meta.append(link(d.source_url,officeI18n.t('source')));
    li.append(meta);

    if(d.suggestion){
      const sug=el('div','pc-suggestion');
      const sugHead=el('div','pc-suggestion-head');
      sugHead.append(el('strong',null,officeI18n.t('Proposed recommendation')));
      if(d.suggestion.by)sugHead.append(el('span','pc-muted',' · by '+d.suggestion.by));
      if(d.suggestion.at)sugHead.append(el('span','pc-muted',' · '+when(d.suggestion.at)));
      if(d.suggestion.source_url)sugHead.append(el('span','pc-muted',' · '),link(d.suggestion.source_url,officeI18n.t('source')));
      sug.append(sugHead);
      if(d.suggestion.context){
        sug.append(el('p','pc-suggestion-context',d.suggestion.context));
      }
      if(d.suggestion.options&&d.suggestion.options.length){
        const ul=el('ul','pc-suggestion-options');
        d.suggestion.options.forEach(opt=>{
          const isRec=Boolean(d.suggestion.recommended&&opt===d.suggestion.recommended);
          const optLi=el('li','pc-suggestion-option'+(isRec?' pc-recommended':''));
          optLi.append(el('span','pc-option-text',opt));
          if(isRec)optLi.append(el('span','pc-tag',officeI18n.t('Recommended')));
          ul.append(optLi);
        });
        sug.append(ul);
      }
      li.append(sug);
    }
    return li;
  }
  function card(p){
    const c=el('article','pc-card');
    c.id='pc-card-'+p.id;
    const head=el('button','pc-card-head');
    head.type='button';
    const badges=el('div','pc-card-badges');
    badges.id='pc-card-badges-'+p.id;
    badges.append(badge(p));
    renderActivityPill(p,badges);
    head.append(el('strong',null,p.name),badges);
    head.onclick=()=>open(p.id);
    c.append(head);
    const ds=pending(p);
    if(ds.length){
      const pBox=el('div','pc-pending-summary');
      const pLabel=el('span','pc-label',officeI18n.t('Waiting for you')+' \u00b7 '+ds.length);
      pBox.append(pLabel);
      const ul=el('ul','pc-pending-list');
      ds.slice(0, 2).forEach(d=>{
        const li=el('li','pc-pending-item',d.question);
        li.title=d.question;
        li.setAttribute('aria-label',d.question);
        ul.append(li);
      });
      if(ds.length > 2){
        ul.append(el('li','pc-pending-more',officeI18n.t('{count} more',{count:ds.length-2})));
      }
      pBox.append(ul);
      c.append(pBox);
    }
    const sumP=el('p','pc-summary',p.summary);
    if(p.summary){
      sumP.title=p.summary;
      sumP.setAttribute('aria-label',p.summary);
    }
    c.append(sumP);
    const facts=el('div','pc-muted pc-manager-line');{const mb=managerBot(p.id);if(mb){const face=el('span','avatar');officeAvatars.fill(face,{key:mb.bot_key,projectId:mb.project_id,botId:mb.bot_id,name:mb.name||mb.bot_id});facts.append(face);}facts.append(document.createTextNode(p.manager||(mb&&(mb.name||mb.bot_id))||''));}
    c.append(facts);
    c.append(projectMetrics(p));
    const rs=[...(p.reports||[])].sort((a,b)=>new Date(b.at)-new Date(a.at));
    const r=rs[0];
    if(r){
      const latestText=officeI18n.t('Latest')+': '+r.text+(r.at?' \u00b7 '+when(r.at):'');
      const latestP=el('p','pc-latest',latestText);
      latestP.title=latestText;
      latestP.setAttribute('aria-label',latestText);
      c.append(latestP);
    }
    c.append(freshness(p));
    return c;
  }

  function managerBot(pid){return registryFailed?null:officeAvatars.managerOf(registryData,pid);}
  function rosterTeams(pid){return registryFailed?null:(registryData.teams||[]).filter(team=>team.project_id===pid);}
  function observation(bot){
    if(agentFailed)return null;
    const matches=(agentData.agents||[]).filter(a=>a.bot_key===bot.bot_key&&a.bot_id===bot.bot_id&&a.project_id===bot.project_id&&a.thread_id===bot.thread_id);
    return matches.length===1?matches[0]:null;
  }
  function isWorking(bot){
    const a=observation(bot),at=Date.parse(a?.runtime?.source_at);
    return Boolean(bot.thread_id&&a?.execution?.state==='working'&&a.runtime?.status==='active'&&a.runtime?.freshness==='fresh'&&Number.isFinite(at)&&Date.now()>=at&&Date.now()-at<=60000);
  }
  function projectMetrics(p){
    const metrics=el('dl','project-metrics');
    const sourceKnown=p.source?.available===true&&!stale(p)&&!problem;
    const teams=rosterTeams(p.id),teamKnown=teams&&teams.length&&teams.every(t=>['ok','no_teams'].includes(t.status));
    const bots=registryFailed?[]:(registryData.bots||[]).filter(b=>b.project_id===p.id);
    const runtimeKnown=teamKnown&&!agentFailed&&bots.every(bot=>{
      const a=observation(bot),at=Date.parse(a?.runtime?.source_at);
      return bot.thread_id&&a?.runtime?.freshness==='fresh'&&['active','idle'].includes(a.runtime.status)&&Number.isFinite(at)&&Date.now()>=at&&Date.now()-at<=60000;
    });
    const fields=[['Open tasks',sourceKnown&&Number.isInteger(p.open_tasks)?p.open_tasks:null],
      ['Recorded completed',!sourceKnown?null:p.work?(p.work.state==='known'&&Number.isInteger(p.work.done)?p.work.done:null):Array.isArray(p.tasks)?p.tasks.filter(task=>task.state==='done').length:null],
      ['Teams',teamKnown?teams.filter(t=>t.status==='ok').length:null],
      ['Owner waits',sourceKnown&&Array.isArray(p.decisions)?pending(p).length:null],
      ['Verified active agents',runtimeKnown?bots.filter(isWorking).length:null]];
    for(const [label,value] of fields){const row=el('div');row.append(el('dt',null,officeI18n.t(label)),el('dd',null,value===null?officeI18n.t('Unknown'):String(value)));metrics.append(row);}
    // An unavailable work source says why, so Unknown is not mistaken for zero.
    if(p.source?.available!==true&&typeof p.source?.error==='string'&&p.source.error){const row=el('div','project-metric-reason');row.append(el('dt',null,officeI18n.t('Why unknown')),el('dd',null,officeI18n.t(p.source.error)));metrics.append(row);}
    return metrics;
  }
  function projectTeams(p){
    const section=el('section','project-teams');section.append(el('h2','pc-h2',officeI18n.t('Teams')));
    const teams=rosterTeams(p.id);
    if(!teams||!teams.length||teams.some(t=>!['ok','no_teams'].includes(t.status))){section.append(empty(officeI18n.t('Team source is unavailable.')));return section;}
    const named=teams.filter(t=>t.status==='ok');
    if(!named.length){section.append(empty(officeI18n.t('No teams')));return section;}
    for(const team of named){
      const group=el('section','project-team');group.append(el('h3',null,team.team));
      const grid=el('div','project-team-grid');
      for(const member of team.members||[]){
        const bot={...member,project_id:p.id};
        const card=el('article','project-agent-card'),button=el('button','project-agent-open');button.type='button';button.dataset.botKey=bot.bot_key;
        const avatar=el('span','avatar');officeAvatars.fill(avatar,{key:bot.bot_key,projectId:bot.project_id,botId:bot.bot_id,name:bot.display_name||bot.bot_id});
        button.append(avatar,el('strong',null,bot.display_name||bot.bot_id),el('span','pc-muted',officeI18n.t(bot.role||'Unknown')),
          el('span','pc-status',officeI18n.t(isWorking(bot)?'Working':'Unknown')));
        button.onclick=()=>window.officeAgents.show(bot.bot_key);card.append(button);
        const a=observation(bot),task=a?.task;
        if(task&&task.updated_at&&Date.now()>=Date.parse(task.updated_at)&&Date.now()-Date.parse(task.updated_at)<=48*3600e3){
          card.append(link(task.url,task.title||task.id||officeI18n.t('Open task')));
        }
        grid.append(card);
      }
      if(!grid.children.length)group.append(empty(officeI18n.t('No active roster members.')));else group.append(grid);
      section.append(group);
    }
    return section;
  }
  function warning(){if(!problem||!data?.available)return null;const w=el('p','pc-banner','Update failed: '+problem+'. Showing the last valid data from '+when(data.snapshot.generated_at)+'; it may be out of date.');w.setAttribute('role','alert');return w;}
  function header(title,sub,newProject){
    const h=el('header','pc-header');
    const t=el('div');
    const subSpan=el('span','room-description',sub);
    subSpan.id='pm-header-subtitle';
    t.append(el('h1',null,title),subSpan);
    h.append(t);
    if(newProject){const add=el('button','secondary new-project-open',officeI18n.t('New project'));add.type='button';add.onclick=()=>window.officeNewProject.open(true);h.append(add);}
    return h;
  }
  function empty(text){return el('p','empty',text);}

  function buildChatPane(pid){
    const pane=el('div','pm-right-col');
    pane.id='pm-chat-container';
    const heading=el('div','pm-chat-heading');
    heading.id='pm-chat-title';
    pane.append(heading);
    const conv=el('div','pm-conversation');
    conv.id='pm-conv-box';
    conv.onscroll=()=>{
      const isAtBottom=(conv.scrollHeight-conv.scrollTop-conv.clientHeight)<40;
      const jBtn=document.getElementById('pm-jump-latest-btn');
      if(jBtn)jBtn.style.display=isAtBottom?'none':'block';
    };
    pane.append(conv);
    const jumpBtn=el('button','pm-jump-latest-btn','Jump to latest ↓');
    jumpBtn.id='pm-jump-latest-btn';
    jumpBtn.type='button';
    jumpBtn.setAttribute('aria-label','Jump to latest messages');
    jumpBtn.style.display='none';
    jumpBtn.onclick=()=>{
      const cBox=document.getElementById('pm-conv-box');
      if(cBox)cBox.scrollTo({top:cBox.scrollHeight,behavior:'smooth'});
    };
    pane.append(jumpBtn);
    const delivs=el('div','pm-deliveries');
    delivs.id='pm-deliv-box';
    pane.append(delivs);
    const serverErr=el('p','pm-inline-notice error');
    serverErr.id='pm-server-error';
    serverErr.style.display='none';
    pane.append(serverErr);
    const notice=el('p','pm-inline-notice');
    notice.id='pm-chat-notice';
    pane.append(notice);
    const previewBox=el('div','pm-composer-previews');
    previewBox.id='pm-composer-previews';
    pane.append(previewBox);
    const form=el('form','pm-composer');
    form.id='pm-chat-form';
    const inputWrap=el('div','input-wrap');
    const ta=el('textarea');
    ta.id='pm-composer-textarea';
    ta.rows=2;
    ta.value=projectDrafts.get(pid)||'';
    ta.oninput=()=>{
      projectDrafts.set(pid,ta.value);
      checkDraftMismatch(pid);
    };
    ta.onkeydown=(e)=>{
      const intent=composerIntent(e);
      if(intent){
        e.preventDefault();
        if(intent==='send')submitForm();
        else if(intent==='queue'){notice.textContent=officeI18n.t('Waiting is available in rooms only. Press Enter to send.');notice.className='pm-inline-notice';}
      }
    };
    ta.onpaste=async(e)=>{
      const cb=e.clipboardData||window.clipboardData;
      if(!cb)return;
      const imgFiles=[];
      if(cb.files&&cb.files.length){
        for(let i=0;i<cb.files.length;i++){
          const f=cb.files[i];
          if(f.type&&f.type.startsWith('image/'))imgFiles.push(f);
        }
      }else if(cb.items&&cb.items.length){
        for(let i=0;i<cb.items.length;i++){
          const it=cb.items[i];
          if(it.type&&it.type.startsWith('image/')){
            const f=it.getAsFile?it.getAsFile():null;
            if(f)imgFiles.push(f);
          }
        }
      }
      if(!imgFiles.length)return;
      e.preventDefault();
      const curImgs=projectDraftImages.get(pid)||[];
      const inFlight=pendingUploadsCount.get(pid)||0;
      if(curImgs.length+inFlight+imgFiles.length>4){
        if(notice){
          notice.textContent='Maximum 4 images allowed per message.';
          notice.className='pm-inline-notice error';
        }
        return;
      }
      pendingUploadsCount.set(pid, inFlight + imgFiles.length);
      for(const file of imgFiles){
        if(file.size>8*1024*1024){
          if(notice){
            notice.textContent='Images must be 8 MB or less.';
            notice.className='pm-inline-notice error';
          }
          pendingUploadsCount.set(pid, Math.max(0, (pendingUploadsCount.get(pid)||1) - 1));
          continue;
        }
        try{
          const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/images',{
            method:'POST',
            headers:{'X-Room-Token':token,'Content-Type':file.type},
            body:file
          });
          if(!r.ok){
            const err=await r.json().catch(()=>({}));
            throw new Error(err.error||('HTTP '+r.status));
          }
          const json=await r.json();
          const list=projectDraftImages.get(pid)||[];
          list.push({id:json.id,type:file.type,size:file.size,name:file.name||'Pasted image'});
          projectDraftImages.set(pid,list);
          renderComposerPreviews(pid);
          checkDraftMismatch(pid);
          if(view===pid)updateChatPane(pid);
        }catch(err){
          if(notice){
            notice.textContent='Failed to attach image: '+err.message;
            notice.className='pm-inline-notice error';
          }
        }finally{
          pendingUploadsCount.set(pid, Math.max(0, (pendingUploadsCount.get(pid)||1) - 1));
        }
      }
    };
    inputWrap.append(ta);
    const sendBtn=el('button',null,officeI18n.t('Send'));
    sendBtn.id='pm-send-btn';
    sendBtn.type='submit';
    sendBtn.disabled=true;
    const retryBtn=el('button','pm-resume-btn','Retry Original');
    retryBtn.id='pm-retry-orig-btn';
    retryBtn.type='button';
    retryBtn.style.display='none';
    form.append(inputWrap,sendBtn,retryBtn);
    async function submitForm(){
      const txt=ta.value;
      const imgs=projectDraftImages.get(pid)||[];
      if(imgs.length>0){
        if(notice){
          notice.textContent='Native Desktop chat does not support image attachments. Remove attached image to send, or send text only.';
          notice.className='pm-inline-notice error';
        }
        return;
      }
      if(!txt.trim())return;
      let att=pendingAttempts.get(pid);
      if(att&&att.inflight)return;
      const currentMgr=chatStates.get(pid)?.manager;
      const mgrId=currentMgr?.thread_id;
      const st=chatStates.get(pid);
      const hasUnresolved=(st?.deliveries||[]).some(d=>['sending','accepted','ambiguous'].includes(d.state));
      if(!st||!st.available||st.hold||st.status==='working in Codex'||hasUnresolved){
        return;
      }
      const imgIds=imgs.map(x=>x.id);
      if(att&&att.unconfirmed){
        if(att.mgrId!==mgrId){
          notice.textContent='Cannot send: Manager thread changed while an attempt is unconfirmed.';
          notice.className='pm-inline-notice error';
          return;
        }
        const textMatch=att.text===txt.trim();
        const imgsMatch=(att.images||[]).length===imgIds.length&&(att.images||[]).every((id,idx)=>id===imgIds[idx]);
        if(!textMatch||!imgsMatch){
          notice.textContent='Unconfirmed original attempt pending. Retry original or wait for reconciliation before sending new text.';
          notice.className='pm-inline-notice held';
          retryBtn.style.display='inline-block';
          return;
        }
      }else{
        att={
          cid:crypto.randomUUID(),
          text:txt.trim(),
          snapshotText:txt,
          images:imgIds,
          mgrId:mgrId,
          inflight:false,
          unconfirmed:false
        };
        pendingAttempts.set(pid,att);
      }
      await executeSend(pid,att,att.snapshotText,att.images,currentMgr);
    }
    form.onsubmit=(e)=>{
      e.preventDefault();
      submitForm();
    };
    retryBtn.onclick=async()=>{
      const att=pendingAttempts.get(pid);
      if(!att||att.inflight)return;
      const currentMgr=chatStates.get(pid)?.manager;
      await executeSend(pid,att,att.snapshotText,att.images,currentMgr);
    };
    pane.append(form);
    return pane;
  }

  async function renderComposerPreviews(pid){
    if(view!==pid)return;
    const box=document.getElementById('pm-composer-previews');
    if(!box)return;
    box.replaceChildren();
    const imgs=projectDraftImages.get(pid)||[];
    const sendBtn=document.getElementById('pm-send-btn');
    const notice=document.getElementById('pm-chat-notice');
    if(!imgs.length){
      if(notice&&notice.textContent.includes('Native Desktop chat does not support image attachments')){
        notice.textContent='';
        notice.className='pm-inline-notice';
      }
      return;
    }
    if(notice){
      notice.textContent='Native Desktop chat does not support image attachments. Remove attached image to send, or send text only.';
      notice.className='pm-inline-notice error';
    }
    if(sendBtn){
      sendBtn.disabled=true;
      sendBtn.textContent=officeI18n.t('Send');
    }
    for(let i=0;i<imgs.length;i++){
      const pic=imgs[i];
      const item=el('div','pm-preview-item');
      const img=el('img');
      img.alt=pic.name||'Attachment';
      if(pic.objectUrl){
        img.src=pic.objectUrl;
      }else{
        try{
          const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/images/'+encodeURIComponent(pic.id),{
            headers:{'X-Room-Token':token}
          });
          if(r.ok){
            const blob=await r.blob();
            const u=URL.createObjectURL(blob);
            pic.objectUrl=u;
            img.src=u;
          }
        }catch(_){}
      }
      const rmBtn=el('button','pm-preview-remove',String.fromCharCode(215));
      rmBtn.type='button';
      rmBtn.setAttribute('aria-label','Remove image');
      rmBtn.onclick=()=>{
        const cur=projectDraftImages.get(pid)||[];
        if(pic.objectUrl){
          try{URL.revokeObjectURL(pic.objectUrl);}catch(_){}
        }
        fetch('/api/projects/'+encodeURIComponent(pid)+'/images/'+encodeURIComponent(pic.id),{
          method:'DELETE',headers:{'X-Room-Token':token}
        }).catch(()=>{});
        cur.splice(i,1);
        projectDraftImages.set(pid,cur);
        renderComposerPreviews(pid);
        checkDraftMismatch(pid);
        if(view===pid)updateChatPane(pid);
      };
      item.append(img,rmBtn);
      box.append(item);
    }
    const note=el('div','pm-preview-note','Image preview (local preview; native desktop delivery unsupported; remove to send text).');
    box.append(note);
  }

  function checkDraftMismatch(pid){
    if(view!==pid)return;
    const att=pendingAttempts.get(pid);
    const retryBtn=document.getElementById('pm-retry-orig-btn');
    const notice=document.getElementById('pm-chat-notice');
    const ta=document.getElementById('pm-composer-textarea');
    const st=chatStates.get(pid);
    const currentMgr=st?.manager;
    const mgrId=currentMgr?.thread_id;
    if(!att||!att.unconfirmed){
      if(retryBtn) retryBtn.style.display='none';
      return;
    }
    const eligible=Boolean(st && st.available && !st.hold && (st.status !== officeI18n.t('working in Codex')) && att.mgrId && mgrId && att.mgrId === mgrId && !att.inflight);
    const curImgs=projectDraftImages.get(pid)||[];
    const imgIds=curImgs.map(x=>x.id);
    const textMatch=ta&&ta.value.trim()===att.text;
    const imgsMatch=(att.images||[]).length===imgIds.length&&(att.images||[]).every((id,idx)=>id===imgIds[idx]);
    if(!textMatch||!imgsMatch){
      if(retryBtn){
        retryBtn.style.display='inline-block';
        retryBtn.disabled=!eligible;
      }
      if(notice) { notice.textContent='Unconfirmed delivery of "'+att.text+'" pending. Retry original or wait for confirmation.'; notice.className='pm-inline-notice held'; }
    }else{
      if(retryBtn) retryBtn.style.display='none';
    }
  }

  async function executeSend(pid,att,snapshotText,snapshotImages,currentMgr){
    const sendBtn=document.getElementById('pm-send-btn');
    const retryBtn=document.getElementById('pm-retry-orig-btn');
    const notice=document.getElementById('pm-chat-notice');
    const ta=document.getElementById('pm-composer-textarea');
    const st=chatStates.get(pid);
    const hasUnresolved=(st?.deliveries||[]).some(d=>['sending','accepted','ambiguous'].includes(d.state));
    if(!st||!st.available||st.hold||st.status==='working in Codex'||(!att.unconfirmed && hasUnresolved)){
      return;
    }
    if(!att.mgrId || !currentMgr?.thread_id || att.mgrId !== currentMgr.thread_id){
      if(notice){
        notice.textContent='Cannot send: Manager thread changed or unavailable since attempt began. Cannot deliver unconfirmed message to a different thread.';
        notice.className='pm-inline-notice error';
      }
      return;
    }
    att.inflight=true;
    if(sendBtn) sendBtn.disabled=true;
    if(retryBtn) retryBtn.disabled=true;
    if(notice) { notice.textContent=officeI18n.t('Sending...'); notice.className='pm-inline-notice'; }
    try{
      const res=await api('/api/projects/'+encodeURIComponent(pid)+'/messages',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          text:att.text,
          client_message_id:att.cid,
          expected_manager_thread:att.mgrId
        })
      });

      const isAccepted = Boolean(res && (res.state === 'accepted' || res.state === 'pending'));
      if (isAccepted) {
        pendingAttempts.delete(pid);
        if(retryBtn) retryBtn.style.display='none';

        // EXACT RAW snapshot equality ONLY - no trim equivalence!
        const curText=projectDrafts.get(pid)??(ta?ta.value:'');
        const curImgs=projectDraftImages.get(pid)||[];
        const textStillMatches=(curText===snapshotText);
        const sImgs=snapshotImages||[];
        const imgsStillMatch=(curImgs.length===sImgs.length&&curImgs.every((x,i)=>x.id===sImgs[i]));
        if(textStillMatches&&imgsStillMatch){
          projectDrafts.set(pid,'');
          projectDraftImages.set(pid,[]);
          if(view===pid){
            const curTa=document.getElementById('pm-composer-textarea');
            if(curTa) curTa.value='';
            renderComposerPreviews(pid);
          }
        }
        if(notice) { notice.textContent='Delivered to Manager outbox.'; notice.className='pm-inline-notice'; }
        await loadProjectChat(pid);
      } else {
        // Ambiguous, failed, or uncertain outcome
        att.unconfirmed=true;
        if(notice) {
          const stateMsg = res?.state ? ('Delivery outcome: ' + res.state) : 'Send unconfirmed';
          notice.textContent = stateMsg + ' (original attempt retained)';
          notice.className = 'pm-inline-notice held';
        }
        checkDraftMismatch(pid);
      }
    }catch(err){
      att.unconfirmed=true;
      if(notice) { notice.textContent='Send unconfirmed: '+err.message+' (original attempt retained)'; notice.className='pm-inline-notice error'; }
      checkDraftMismatch(pid);
    }finally{
      if(pendingAttempts.get(pid)===att) att.inflight=false;
      if(view===pid) updateChatPane(pid);
    }
  }

  function updateProjectLeftCol(pid){
    if(view!==pid)return;
    const left=document.getElementById('pm-left-col-box');
    if(!left)return;
    const p=projects().find(x=>x.id===pid);
    if(!p)return;
    left.replaceChildren();
    left.append(el('p','pc-summary',p.summary),freshness(p));
    left.append(projectMetrics(p),projectTeams(p));
    if(p.tracking){
      const t=el('p','pc-muted','Task authority: '+p.tracking.authority+' ');
      if(p.tracking.url)t.append(link(p.tracking.url,'open tracker'));
      left.append(t);
    }
    const ds=p.decisions||[];
    left.append(el('h2','pc-h2',officeI18n.t('Decisions')));
    if(ds.length){
      const ul=el('ul','pc-decisions');
      ds.forEach(d=>{
        const li=decisionItem(d);
        if(d.state!=='pending'){
          li.classList.add('pc-closed');
          li.prepend(el('span','pc-tag',d.state));
          if(d.answer){
            const a=el('p',null,'Answer: '+d.answer.text+' — '+d.answer.by+', '+when(d.answer.at)+' ');
            a.append(link(d.answer.source_url,'evidence'));
            li.append(a);
          }
        }
        ul.append(li);
      });
      left.append(ul);
    }else left.append(empty(officeI18n.t('No owner decisions recorded.')));
    left.append(el('h2','pc-h2',officeI18n.t('Tasks')));
    const ts=p.tasks||[];
    if(ts.length){
      const ul=el('ul','pc-list');
      ts.forEach(t=>{
        const li=el('li');
        li.append(link(t.url,t.id+' '+t.title),el('span','pc-tag',t.state.replace('_',' ')));
        ul.append(li);
      });
      left.append(ul);
    }else left.append(empty(officeI18n.t('No tasks listed in the snapshot.')));
    left.append(el('h2','pc-h2',officeI18n.t('Reports')));
    const rs=[...(p.reports||[])].sort((a,b)=>new Date(b.at)-new Date(a.at));
    if(rs.length){
      const ul=el('ul','pc-list pc-reports');
      rs.forEach(r=>{
        const li=el('li');
        li.append(el('strong',null,r.from+' · '+when(r.at)),el('p',null,r.text),link(r.source_url,officeI18n.t('source')));
        ul.append(li);
      });
      left.append(ul);
    }else left.append(empty('No reports yet.'));
  }

  function updateChatPane(pid){
    if(view!==pid)return;
    const container=document.getElementById('pm-chat-container');
    if(!container)return;
    const conv=document.getElementById('pm-conv-box');
    const isFirstLoad=!conv||!conv.hasChildNodes()||conv.dataset.loadedPid!==pid;
    const wasAtBottom=!conv||(conv.scrollHeight-conv.scrollTop-conv.clientHeight)<40;
    const prevScrollTop=conv?conv.scrollTop:0;
    const st=chatStates.get(pid);
    const mgrName=st?.manager?.name||'Project Manager';
    const heading=document.getElementById('pm-chat-title');
    if(heading){
      heading.replaceChildren();
      const h2=el('h2','pc-h2','Manager Chat · '+mgrName);if(st?.manager?.bot_key){const face=el('span','avatar');officeAvatars.fill(face,{key:st.manager.bot_key,name:mgrName});h2.prepend(face);}heading.append(h2);
      if(st){
        const bar=el('div','pm-chat-status-bar');
        const statusTxt = st.hold ? officeI18n.t('Held') : (st.status || (st.available ? officeI18n.t('Idle') : officeI18n.t('Unavailable')));
        bar.append(el('span','pc-tag',statusTxt));
        if(st.hold){
          const rBtn=el('button','pm-resume-btn',officeI18n.t('Resume'));
          rBtn.type='button';
          rBtn.onclick=async()=>{
            rBtn.disabled=true;
            try{
              await api('/api/projects/'+encodeURIComponent(pid)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
              await loadProjectChat(pid);
            }catch(err){ document.getElementById('pm-chat-notice').textContent=officeI18n.t('Resume failed: {error}',{error:err.message}); }finally{ rBtn.disabled=false; }
          };
          bar.append(rBtn);
        }else if(st.available && !st.hold && (st.status==='working in Codex'||(st.deliveries||[]).some(d=>['sending','accepted','ambiguous'].includes(d.state)))){
          const sBtn=el('button','pm-stop-btn',officeI18n.t('Stop'));
          sBtn.type='button';
          sBtn.onclick=async()=>{
            sBtn.disabled=true;
            try{
              await api('/api/projects/'+encodeURIComponent(pid)+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
              await loadProjectChat(pid);
            }catch(err){ document.getElementById('pm-chat-notice').textContent=officeI18n.t('Stop failed: {error}',{error:err.message}); }finally{ sBtn.disabled=false; }
          };
          bar.append(sBtn);
        }
        heading.append(bar);
      }
    }
    const ta=document.getElementById('pm-composer-textarea');
    const sendBtn=document.getElementById('pm-send-btn');
    const retryBtn=document.getElementById('pm-retry-orig-btn');
    const notice=document.getElementById('pm-chat-notice');
    const serverErr=document.getElementById('pm-server-error');
    if(ta) ta.placeholder=officeI18n.t('Message {name} directly…',{name:mgrName});

    if(!st){
      if(conv) conv.replaceChildren(empty(officeI18n.t('Loading Manager conversation…')));
      if(ta) ta.disabled=true;
      if(sendBtn){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
      }
      if(retryBtn){
        retryBtn.disabled=true;
        retryBtn.style.display='none';
      }
      if(serverErr) serverErr.style.display='none';
      return;
    }
    if(!st.available){
      if(conv){
        const box=el('div','pm-chat-disabled');
        box.append(el('strong',null,officeI18n.t('Chat Unavailable (Read-only)')));
        box.append(el('p','pc-muted',st.error||officeI18n.t('Manager chat is not accessible on Desktop.')));
        conv.replaceChildren(box);
      }
      if(ta) ta.disabled=true;
      if(sendBtn){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
      }
      if(retryBtn){
        retryBtn.disabled=true;
        retryBtn.style.display='none';
      }
      if(notice) notice.textContent='';
      if(serverErr) serverErr.style.display='none';
      return;
    }
    if(ta) ta.disabled=false;

    if(conv){
      const journal=st.journal||[];
      conv.replaceChildren();
      if(!journal.length){
        conv.append(empty('No messages yet with '+mgrName+'.'));
      }else{
        for(const m of journal){
          const art=el('article','message '+(m.sender==='owner'?'owner':'bot'));
          const head=el('div','message-head');
          const author=el('span','author',m.sender==='owner'?officeI18n.t('You'):mgrName);
          head.append(author);
          if(m.created){
            const t=el('time','timestamp',new Date(m.created*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}));
            head.append(t);
          }
          const p=el('p',null,m.text);
          art.append(head,p);
          if(m.images&&m.images.length){
            const imgWrap=el('div','message-images');
            for(const img of m.images){
              const thumb=el('img','message-thumbnail');
              thumb.src='/api/projects/'+encodeURIComponent(pid)+'/images/'+encodeURIComponent(img.id);
              thumb.alt=officeI18n.t('Attached image');
              imgWrap.append(thumb);
            }
            art.append(imgWrap);
          }
          conv.append(art);
        }
      }
      conv.dataset.loadedPid=pid;
    }

    const delivs=document.getElementById('pm-deliv-box');
    if(delivs){
      delivs.replaceChildren();
      for(const d of (st.deliveries||[])){
        delivs.append(el('div','delivery','Status: '+d.state+(d.error?' ('+d.error+')':'')));
      }
    }

    if(serverErr){
      if(st.error){
        serverErr.textContent = st.error;
        serverErr.style.display = 'block';
      }else{
        serverErr.textContent = '';
        serverErr.style.display = 'none';
      }
    }

    const hasUnresolved = (st.deliveries||[]).some(d=>['sending','accepted','ambiguous'].includes(d.state));
    if(notice){
      const att=pendingAttempts.get(pid);
      const curImgs=projectDraftImages.get(pid)||[];
      if(curImgs.length>0){
        notice.textContent='Native Desktop chat does not support image attachments. Remove attached image to send, or send text only.';
        notice.className='pm-inline-notice error';
      }else if(att&&att.unconfirmed){
        notice.textContent='Unconfirmed delivery of "'+att.text+'" pending. Retry original or wait for confirmation.';
        notice.className='pm-inline-notice held';
      }else if(st.hold){
        notice.textContent='Chat is held. Resume before sending new messages.';
        notice.className='pm-inline-notice held';
      }else if(notice.textContent.includes('Native Desktop chat does not support image attachments') || notice.textContent.includes('Unconfirmed delivery')){
        notice.textContent='';
      }
    }

    if(sendBtn){
      const att=pendingAttempts.get(pid);
      if(att&&att.inflight){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
      }else if(st.status==='working in Codex'){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
      }else if(st.hold){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
        if(notice&&!notice.textContent){ notice.textContent='Chat is held. Resume before sending new messages.'; notice.className='pm-inline-notice held'; }
      }else if(hasUnresolved){
        sendBtn.disabled=true;
        sendBtn.textContent=officeI18n.t('Send');
        if(notice&&!notice.textContent){ notice.textContent='Pending delivery in progress.'; notice.className='pm-inline-notice held'; }
      }else{
        const curImgs=projectDraftImages.get(pid)||[];
        if(curImgs.length>0){
          sendBtn.disabled=true;
          sendBtn.textContent=officeI18n.t('Send');
        }else{
          sendBtn.disabled=false;
          sendBtn.textContent=officeI18n.t('Send');
        }
      }
    }
    checkDraftMismatch(pid);

    if(conv){
      if(isFirstLoad||wasAtBottom){
        conv.scrollTop=conv.scrollHeight;
      }else{
        conv.scrollTop=prevScrollTop;
      }
      const isNowAtBottom=(conv.scrollHeight-conv.scrollTop-conv.clientHeight)<40;
      const jBtn=document.getElementById('pm-jump-latest-btn');
      if(jBtn)jBtn.style.display=isNowAtBottom?'none':'block';
    }
  }

  function initSplitDivider(split,left,right,divider){
    const MIN_LEFT = 320;
    const MIN_RIGHT = 324; // accommodates 2px border so clientWidth >= 320px
    const DIVIDER_W = 12;

    let currentLeftPx = 0;

    function applyTarget(targetLeftPx){
      const totalWidth = split.clientWidth;
      const available = totalWidth - DIVIDER_W;
      if(available < MIN_LEFT + MIN_RIGHT){
        split.classList.add('pm-stacked');
        left.style.flex = '';
        left.style.maxWidth = '';
        right.style.flex = '';
        right.style.minWidth = '';
        return;
      }
      split.classList.remove('pm-stacked');
      const minLeft = MIN_LEFT;
      const maxLeft = available - MIN_RIGHT;
      currentLeftPx = Math.max(minLeft, Math.min(maxLeft, targetLeftPx));
      left.style.flex = '0 0 ' + currentLeftPx + 'px';
      left.style.maxWidth = currentLeftPx + 'px';
      right.style.flex = '1 1 0';
      right.style.minWidth = MIN_RIGHT + 'px';

      const pct = Math.round((currentLeftPx / available) * 100);
      divider.setAttribute('aria-valuenow', String(pct));
      divider.setAttribute('aria-valuemin', String(Math.round((minLeft / available) * 100)));
      divider.setAttribute('aria-valuemax', String(Math.round((maxLeft / available) * 100)));
      try{ localStorage.setItem('office_pm_split_ratio', String(pct)); }catch(_){}
    }

    let initialRatio = 52;
    try{
      const val = localStorage.getItem('office_pm_split_ratio');
      if(val){
        const parsed = parseFloat(val);
        if(!isNaN(parsed) && parsed >= 20 && parsed <= 80) initialRatio = parsed;
      }
    }catch(_){}

    const totalWidth = split.clientWidth;
    const available = totalWidth - DIVIDER_W;
    applyTarget(available > 0 ? available * (initialRatio / 100) : MIN_LEFT);

    let dragging = false;
    divider.onpointerdown = e => {
      if(e.button !== 0) return;
      dragging = true;
      divider.classList.add('dragging');
      divider.setPointerCapture(e.pointerId);
    };

    divider.onpointermove = e => {
      if(!dragging) return;
      const rect = split.getBoundingClientRect();
      if(rect.width <= 0) return;
      applyTarget(e.clientX - rect.left);
    };

    const stopDrag = e => {
      if(!dragging) return;
      dragging = false;
      divider.classList.remove('dragging');
      try{ divider.releasePointerCapture(e.pointerId); }catch(_){}
    };
    divider.onpointerup = stopDrag;
    divider.onpointercancel = stopDrag;

    divider.onkeydown = e => {
      const totalW = split.clientWidth;
      const avail = totalW - DIVIDER_W;
      if(avail < MIN_LEFT + MIN_RIGHT) return;
      const minL = MIN_LEFT;
      const maxL = avail - MIN_RIGHT;
      if(e.key === 'ArrowLeft'){
        e.preventDefault();
        applyTarget(currentLeftPx - 20);
      }else if(e.key === 'ArrowRight'){
        e.preventDefault();
        applyTarget(currentLeftPx + 20);
      }else if(e.key === 'Home'){
        e.preventDefault();
        applyTarget(minL);
      }else if(e.key === 'End'){
        e.preventDefault();
        applyTarget(maxL);
      }
    };

    window.addEventListener('resize', () => {
      if(document.getElementById('projects-view').hidden || view === 'overview' || view === 'inbox') return;
      applyTarget(currentLeftPx);
    });
  }

  function renderVisibilityControls(){
    const known=allKnownProjects();
    if(!known.length)return null;
    const panel=el('div','pc-visibility-panel');
    const top=el('div','pc-vis-top');

    const searchInput=el('input','pc-search-input');
    searchInput.type='search';
    searchInput.id='pc-proj-search';
    searchInput.placeholder=officeI18n.t('Search known projects…');
    searchInput.setAttribute('aria-label','Search known projects');
    searchInput.value=projSearchQuery;
    searchInput.oninput=(e)=>{
      projSearchQuery=e.target.value.trim().toLowerCase();
      updateChecklistDisplay();
    };

    const actions=el('div','pc-vis-actions');
    const visibleSet=getVisibleProjectIds();
    const isFiltered=visibleSet!==null;
    const allCount=known.length;
    const visibleCount=isFiltered?known.filter(p=>visibleSet.has(p.id)).length:allCount;

    const countMeta=el('span','pc-muted');
    countMeta.id='pc-vis-count';
    countMeta.textContent=isFiltered?officeI18n.t('Showing {count} of {total} ({hidden} hidden)',{count:visibleCount,total:allCount,hidden:allCount-visibleCount}):officeI18n.t('All {count} projects visible',{count:allCount});

    const selectApplicableBtn=el('button','pc-vis-btn',officeI18n.t('Select all applicable'));
    selectApplicableBtn.type='button';
    selectApplicableBtn.id='pc-select-applicable-btn';
    selectApplicableBtn.onclick=()=>{
      const applicableIds=known.filter(p=>p.applicable).map(p=>p.id);
      setVisibleProjectIds(applicableIds);
    };

    const restoreAllBtn=el('button','pc-vis-btn',officeI18n.t('Restore all'));
    restoreAllBtn.type='button';
    restoreAllBtn.id='pc-restore-all-btn';
    restoreAllBtn.disabled=!isFiltered;
    restoreAllBtn.onclick=()=>setVisibleProjectIds(null);

    actions.append(countMeta,selectApplicableBtn,restoreAllBtn);
    top.append(searchInput,actions);
    panel.append(top);

    const checklist=el('div','pc-proj-checklist');
    checklist.id='pc-proj-checklist';

    function updateChecklistDisplay(){
      const items=checklist.querySelectorAll('.pc-proj-item');
      for(const item of items){
        const hay=item.dataset.hay||'';
        item.hidden=projSearchQuery?!hay.includes(projSearchQuery):false;
      }
    }

    known.forEach(p=>{
      const item=el('label','pc-proj-item');
      const hay=[p.name,p.id,p.status,p.reason||''].join(' ').toLowerCase();
      item.dataset.hay=hay;

      const chk=document.createElement('input');
      chk.type='checkbox';
      chk.id=`pc-chk-${p.id}`;
      chk.checked=isProjectVisible(p.id);
      chk.onchange=()=>{
        let set=getVisibleProjectIds();
        if(set===null){
          set=new Set(known.map(k=>k.id));
        }
        if(chk.checked){
          set.add(p.id);
        }else{
          set.delete(p.id);
        }
        setVisibleProjectIds(set);
      };

      const info=el('div','pc-proj-info');
      const head=el('div','pc-proj-head');
      head.append(el('strong',null,p.name));
      if(p.name!==p.id){
        head.append(el('small','pc-muted',`(${p.id})`));
      }
      if(p.is_current){
        head.append(el('span','pc-status pc-current',officeI18n.t('Current')));
      }
      if(p.status==='ok'){
        head.append(el('span','pc-status pc-working',officeI18n.t('Roster valid')));
      }else if(p.status==='no_teams'){
        head.append(el('span','pc-status',officeI18n.t('No teams')));
      }else if(p.status==='source_unavailable'){
        head.append(el('span','pc-status pc-blocked',officeI18n.t('Source unavailable')));
      }else if(p.status==='roster_error'){
        head.append(el('span','pc-status pc-blocked',officeI18n.t('Roster error')));
      }else if(p.status==='unknown'){
        head.append(el('span','pc-status',officeI18n.t('Unknown')));
      }
      info.append(head);
      if(p.reason){
        info.append(el('small','pc-reason',p.reason));
      }
      item.append(chk,info);
      checklist.append(item);
    });

    panel.append(checklist);
    updateChecklistDisplay();
    return panel;
  }

  function render(){const v=document.getElementById('projects-view');v.replaceChildren();const body=el('div','pc-body');const warn=warning();if(warn)body.append(warn);
    if(view==='dashboard'){
      v.append(header(officeI18n.t('Dashboard'),'',true));
      const layout=el('div','foundation-dashboard'),main=el('section'),attention=el('aside','foundation-attention');
      main.append(el('h2',null,officeI18n.t('Projects in your dashboard')));
      const controls=renderVisibilityControls();if(controls){const picker=el('details','dashboard-project-picker');picker.append(el('summary',null,officeI18n.t('Choose projects')),controls);main.append(picker);}
      const grid=el('div','pc-grid');projects().forEach(p=>grid.append(card(p)));
      if(grid.children.length)main.append(grid);else main.append(empty(officeI18n.t(!data?officeI18n.t('Loading projects…'):!data.available?officeI18n.t('Status unavailable'):officeI18n.t('No projects in the snapshot.'))));
      attention.id='owner-dashboard-attention';paintInboxPreview(attention);
      const handle=el('div','pane-handle right-pane-handle');handle.tabIndex=0;handle.setAttribute('role','separator');handle.setAttribute('aria-orientation','vertical');handle.setAttribute('aria-label',officeI18n.t('Right pane width'));
      layout.append(main,handle,attention);window.officeShell?.divider(handle,'right');body.append(layout);v.append(body);return;
    }
    if(view==='inbox'){v.append(header(officeI18n.t('Waiting for me'),''),body);renderInbox(body);return;}
    if(view==='overview'){
      const known=allKnownProjects();

      if(!data){v.append(header(officeI18n.t('Projects'),'',true),body);body.append(empty(officeI18n.t('Loading projects…')));return;}
      if(!data.available){
        v.append(header(officeI18n.t('Projects'),known.length?officeI18n.t('{count} known projects · status unavailable',{count:known.length}):officeI18n.t('No project data'),true),body);
        body.append(empty(data.error||officeI18n.t('Project status is unavailable.')));
        const grid=el('div','pc-grid');allProjects().forEach(p=>grid.append(card(p)));if(grid.children.length)body.append(grid);
        return;
      }
      const ps=projects();
      v.append(header(officeI18n.t('Projects'),ps.length+' projects · updated '+when(data.snapshot.generated_at),true));
      const grid=el('div','pc-grid');
      [...ps].sort((a,b)=>pending(b).length-pending(a).length).forEach(p=>grid.append(card(p)));
      if(ps.length){body.append(grid);}else{const emptyMsg=el('p','empty');emptyMsg.textContent=allProjects().length?officeI18n.t('All projects are hidden by filter. Click "Restore all" above to show all projects.'):officeI18n.t('No projects in the snapshot.');body.append(emptyMsg);}
      v.append(body);
      return;
    }
    if(!data){body.append(empty(officeI18n.t('Loading projects…')));v.append(header(officeI18n.t('Projects'),''),body);return;}
    if(!data.available&&!allProjects().some(p=>p.id===view)){v.append(header(officeI18n.t('Projects'),officeI18n.t('No project data')),body);body.append(empty(data.error||officeI18n.t('Project status is unavailable.')));return;}
    const ps=projects();
    {const p=ps.find(x=>x.id===view);if(!p){v.append(header(officeI18n.t('Project'),officeI18n.t('Not in snapshot')));body.append(empty(officeI18n.t('This project is no longer in the snapshot.')));}else{v.append(header(p.name,officeI18n.t(STATUS[p.status])+(p.manager?' · '+p.manager:'')));
      const split=el('div','pm-split-layout');
      split.id='pm-split-layout';
      const left=el('div','pm-left-col');
      left.id='pm-left-col-box';
      const divider=el('div','pm-split-divider');
      divider.id='pm-split-divider';
      divider.setAttribute('role','separator');
      divider.setAttribute('tabindex','0');
      divider.setAttribute('aria-label',officeI18n.t('Resize layout'));
      divider.setAttribute('aria-orientation','vertical');
      const right=buildChatPane(p.id);
      split.append(left,divider,right);
      body.append(split);
      v.append(body);
      initSplitDivider(split,left,right,divider);
      updateProjectHeaderActivity(p.id);
      updateProjectLeftCol(p.id);
      updateChatPane(p.id);
      renderComposerPreviews(p.id);
      loadProjectActivity(p.id);
      return;
    }}
    v.append(body);
  }

  function renderList(){const list=document.getElementById('project-list');if(!list)return;list.replaceChildren();const n=document.getElementById('projects-notice');
    if(!data?.available){n.textContent=data?data.error:officeI18n.t('Loading…');return;}n.textContent=problem?'Update failed; showing last valid data':'';
    const row=(label,meta,key,cls)=>{const b=el('button','personal-row'+(cls?' '+cls:''));b.type='button';b.setAttribute('aria-pressed',String(!document.getElementById('projects-view').hidden&&view===key));const copy=el('span','session-copy');copy.append(el('strong',null,label),el('small',null,meta));b.append(copy);b.onclick=()=>open(key);list.append(b);};
    const visiblePs=projects();const countLabel=getVisibleProjectIds()!==null?`${visiblePs.length}/${allProjects().length} projects`:`${visiblePs.length} projects`;row(officeI18n.t('Overview'),countLabel,'overview');visiblePs.forEach(p=>row(p.name,(officeI18n.t(STATUS[p.status]||p.status))+(pending(p).length?' · '+pending(p).length+' waiting':''),p.id));}
  function open(key,itemId=null){if(window.officeShell&&!window.officeShell.canNavigate())return;inboxSelected=key==='inbox'?itemId:null;window.officeShell?.activate(key==='dashboard'?'dashboard':key==='inbox'?(itemId?'owner-decision':'waiting'):key==='overview'?'projects':'project',itemId?inboxData?.items.find(item=>item.id===itemId)?.title:!['dashboard','inbox','overview'].includes(key)?allProjects().find(p=>p.id===key)?.name:undefined,()=>open(key,itemId));view=key;officeMedia.stop();officeSpeech.stop();officeView='projects';selectMenu('projects');for(const id of ['room-view','personal-view','session-detail'])document.getElementById(id).hidden=true;document.getElementById('projects-view').hidden=false;
    const title=key==='overview'?officeI18n.t('Projects'):key==='inbox'?officeI18n.t('Waiting for you'):(projects().find(p=>p.id===key)?.name||officeI18n.t('Project'));if(!['dashboard','overview','inbox'].includes(key))addConversation('project',key,title);render();renderList();load();if(key==='dashboard'||key==='inbox')loadInbox();if(key!=='overview'&&key!=='inbox'&&key!=='dashboard'){loadProjectChat(key);loadProjectActivity(key);}}
  window.officeProjects={open,load,loadInbox,refresh:render,hide(){document.getElementById('projects-view').hidden=true;renderList();},isProjectVisible,getVisibleProjectIds,setVisibleProjectIds,allKnownProjects};
  window.addEventListener('office-language-changed',()=>{renderList();if(!document.getElementById('projects-view').hidden){if(view!=='overview'&&view!=='inbox'&&view!=='dashboard'){updateProjectLeftCol(view);updateChatPane(view);}else render();}});
  setInterval(()=>{
    if(document.getElementById('projects-view').hidden) return;
    load();if(view==='dashboard'||view==='inbox')loadInbox();
  }, 10000);
})();
