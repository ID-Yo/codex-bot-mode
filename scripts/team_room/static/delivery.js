(()=>{
  // Delivery panel (#424): the supervisor's status file through /api/outbox-health, and (S4-E) Retry / Skip for reports that wait for
  // a decision. A decision is one POST to /api/outbox-resolve after an inline confirmation; the answer of the server decides what is shown.
  // Office never calls Desktop or the relay for it. Unknown is shown as Unknown, never as healthy, zero or done.
  let data=null,failed=false,loading=false;
  const reportsByProject=new Map();  // project name -> {ok,rows,cut,limit} read from /api/outbox-rows
  const notices=new Map();  // project name -> the result of the last decision, kept as a label key so a language change translates it
  let confirming=null,acting=false;  // the decision waiting for its confirmation / one being sent
  const ID_PATTERN=/^[0-9a-f]{32}$/;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
  const T=(key,values)=>officeI18n.t(key,values);
  const STATE={ok:'Healthy',problems:'Problems',unknown:'Unknown'};
  const NOTE={again:'Report {id} is queued to be sent again.',skipped:'Report {id} was skipped.',refused:'Not done: {reason}',
    unknown:'Unknown outcome. The list was read again; check it before deciding again.'};
  const ageText=seconds=>{
    if(!Number.isFinite(seconds)||seconds<0)return T('unknown time');
    if(seconds<120)return T('{n} s',{n:Math.round(seconds)});
    if(seconds<7200)return T('{n} min',{n:Math.round(seconds/60)});
    if(seconds<172800)return T('{n} h',{n:Math.round(seconds/3600)});
    return T('{n} d',{n:Math.round(seconds/86400)});
  };
  const when=stamp=>{const d=new Date(stamp);return Number.isNaN(d.valueOf())?T('unknown time'):d.toLocaleString(officeI18n.language,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
  const valid=d=>Boolean(d)&&d.schema===1&&Object.keys(STATE).includes(d.state)&&Array.isArray(d.projects)&&Array.isArray(d.incidents)&&Array.isArray(d.problems);
  const tag=text=>el('span','pc-tag',text);
  const waitingCount=project=>(Number.isFinite(project.ambiguous)?project.ambiguous:0)+(Number.isFinite(project.needs_attention)?project.needs_attention:0);
  const shortId=id=>id.slice(0,8);
  const button=(cls,label,onClick,aria)=>{const node=el('button',cls,label);node.type='button';node.disabled=acting;if(aria)node.setAttribute('aria-label',aria);node.addEventListener('click',onClick);return node;};
  const validReport=row=>Boolean(row)&&typeof row.id==='string'&&ID_PATTERN.test(row.id)&&['ambiguous','needs-attention'].includes(row.state);

  async function loadReports(name){
    try{
      const reply=await api('/api/outbox-rows?project='+encodeURIComponent(name));
      if(!reply||!Array.isArray(reply.rows))throw Error('Unexpected report list');
      reportsByProject.set(name,{ok:true,rows:reply.rows.filter(validReport),cut:reply.cut===true,limit:Number.isFinite(reply.limit)?reply.limit:20});
    }catch(error){reportsByProject.set(name,{ok:false});}
  }
  function ask(name,row,action){confirming={project:name,id:row.id,action};repaint();}
  function cancel(){confirming=null;repaint();}
  async function decide(name,id,action){
    if(acting||!confirming)return;
    acting=true;repaint();
    let notice={kind:'unknown',name:'unknown'};
    try{
      const result=await api('/api/outbox-resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:name,id,action,confirm:true})});
      if(result&&result.id===id&&result.action===action&&result.state===(action==='retry'?'pending':'skipped'))
        notice={kind:'done',name:action==='retry'?'again':'skipped',values:{id:shortId(id)},journal:result.journal_written!==false};
    }catch(error){
      if([400,404,409].includes(error.status))notice={kind:'refused',name:'refused',values:{reason:String(error.message||'')}};
    }
    confirming=null;acting=false;notices.set(name,notice);
    await loadReports(name);
    repaint();
  }

  function restartLine(restart){
    if(!restart)return T('No restart recorded.');
    return T('Last restart')+': '+[restart.reason,when(restart.at),T(restart.ok===true?'worked':restart.ok===false?'failed':'Unknown')].filter(Boolean).join(' · ');
  }
  function projectRow(project){
    const row=el('li','delivery-project');row.dataset.project=project.name;
    row.append(el('strong',null,project.name));
    const meta=el('div','delivery-meta');
    const waiting=Number.isFinite(project.pending)?project.pending:null;
    meta.append(el('span',null,waiting===null?T('Unknown'):waiting===0?T('Nothing waiting'):T('{n} waiting',{n:waiting})+(Number.isFinite(project.oldest_pending_seconds)?' · '+T('oldest {age}',{age:ageText(project.oldest_pending_seconds)}):'')));
    meta.append(el('span',null,Number.isFinite(project.last_delivery_age_seconds)?T('Last delivery {age} ago',{age:ageText(project.last_delivery_age_seconds)}):T('No delivery recorded')));
    meta.append(tag(project.pump_alive===true?T('Pump running'):project.pump_alive===false?T('Pump stopped'):T('Unknown')));
    const canary=project.canary;
    const goodCanary=Boolean(canary)&&['active','idle','notLoaded'].includes(canary.last_chat_status)&&Number.isFinite(canary.last_latency_seconds)&&Number.isFinite(canary.last_ok_age_seconds);
    meta.append(tag(!canary?T('Canary unknown'):Number.isFinite(canary.pending_seconds)?T('Canary waiting {age}',{age:ageText(canary.pending_seconds)}):goodCanary?T('Canary {latency} s, {age} ago',{latency:Math.round(canary.last_latency_seconds*10)/10,age:ageText(canary.last_ok_age_seconds)}):T('Canary unknown')));
    if(Number.isFinite(project.needs_attention)&&project.needs_attention>0)meta.append(tag(T('{n} need attention',{n:project.needs_attention})));
    if(Number.isFinite(project.ambiguous)&&project.ambiguous>0)meta.append(tag(T('{n} unconfirmed',{n:project.ambiguous})));
    row.append(meta);
    if(waitingCount(project)>0)row.append(reportsBlock(project));
    return row;
  }
  function reportRow(project,report){
    const item=el('li','delivery-report');item.dataset.row=report.id;
    const head=el('div','delivery-report-head');
    head.append(el('strong',null,shortId(report.id)),tag(report.state==='needs-attention'?T('Needs attention'):T('Unconfirmed')),
      el('span',null,String(report.sender||'')+' → '+String(report.target||'')),el('span',null,ageText(report.age_seconds)));
    item.append(head);
    const mine=confirming&&confirming.project===project.name&&confirming.id===report.id?confirming:null;
    if(!mine){
      const actions=el('div','delivery-actions');
      actions.append(button('delivery-retry',T('Retry'),()=>ask(project.name,report,'retry'),T('Retry report {id}',{id:shortId(report.id)})),
        button('delivery-skip',T('Skip'),()=>ask(project.name,report,'skip'),T('Skip report {id}',{id:shortId(report.id)})));
      item.append(actions);
      return item;
    }
    const box=el('div','delivery-confirm');box.setAttribute('role','group');
    box.append(el('p','delivery-warning',mine.action==='retry'
      ?T('The report may already have arrived. Sending it again can show the Manager the same report twice (same marker line). It keeps its place, so it may also wait behind other unconfirmed reports for the same recipient.')
      :T('This report will never be sent.')));
    const choices=el('div','delivery-actions');
    choices.append(button('delivery-confirm-yes',acting?T('Working…'):mine.action==='retry'?T('Confirm retry'):T('Confirm skip'),()=>decide(project.name,report.id,mine.action)),
      button('delivery-confirm-no',T('Cancel'),cancel));
    box.append(choices);item.append(box);
    return item;
  }
  function reportsBlock(project){
    const box=el('div','delivery-reports');box.dataset.project=project.name;
    const notice=notices.get(project.name);
    if(notice){
      const line=el('p','delivery-notice',T(NOTE[notice.name],notice.values)+(notice.journal===false?' '+T('The journal could not be written.'):''));
      line.dataset.kind=notice.kind;line.setAttribute('role','status');box.append(line);
    }
    const reports=reportsByProject.get(project.name);
    if(!reports){box.append(el('p','delivery-note',T('Loading reports…')));return box;}
    if(!reports.ok){box.append(el('p','delivery-error',T('The reports waiting for a decision cannot be read. Unknown is not empty.')));return box;}
    box.append(el('h4',null,T('Waiting for a decision')));
    if(!reports.rows.length)box.append(el('p','delivery-note',T('No report is waiting for a decision. The counts above update with the next supervisor check.')));
    else{const list=el('ul','delivery-reports-list');for(const report of reports.rows)list.append(reportRow(project,report));box.append(list);}
    if(reports.cut){
      const hidden=waitingCount(project)-reports.rows.length;
      box.append(el('p','delivery-note',T('Only the newest {n} reports of each state are shown. Older ones cannot be decided from Office.',{n:reports.limit})+(hidden>0?' '+T('{n} older reports are not shown.',{n:hidden}):'')));
    }
    return box;
  }
  function incidentRow(incident){
    const row=el('li','delivery-incident');row.dataset.incident=incident.key;
    row.append(el('strong',null,incident.key),el('span',null,' · '+(incident.severity||'')+' · '+T('open since')+' '+when(incident.opened)));
    if(incident.text)row.append(el('p','delivery-text',incident.text));
    if(incident.remedy)row.append(el('p','delivery-text',T('Remedy')+': '+incident.remedy));
    return row;
  }
  function build(){
    const section=el('section','delivery-panel');section.setAttribute('aria-label',T('Delivery'));
    section.append(el('h2',null,T('Delivery')));
    const status=el('p','delivery-state');status.setAttribute('role','status');section.append(status);
    if(failed){section.dataset.state='unknown';status.textContent=T('Delivery status unavailable. Unknown is not healthy.');return section;}
    if(!data){section.dataset.state='loading';status.textContent=T('Loading delivery status…');return section;}
    section.dataset.state=data.state;
    status.textContent=T(STATE[data.state])+(Number.isFinite(data.age_seconds)?' · '+T('Checked {age} ago',{age:ageText(data.age_seconds)}):'');
    if(data.state==='unknown'){section.append(el('p','delivery-reason',data.reason||''),el('p','delivery-note',T('Nothing is known about delivery until the supervisor reports again.')));return section;}
    const relay=el('p','delivery-relay');
    relay.textContent=T(data.relay?.ok?'Relay up':'Relay down')+(data.desktop_running===false?' · '+T('Codex Desktop is closed'):'')+' · '+restartLine(data.relay?.last_restart);
    section.append(relay);
    if(data.last_pump_action)section.append(el('p','delivery-relay',T('Last pump action')+': '+[data.last_pump_action.action,when(data.last_pump_action.at)].filter(Boolean).join(' · ')));
    if(data.problems.length){const list=el('ul','delivery-problems');for(const problem of data.problems)list.append(el('li',null,problem));section.append(list);}
    if(typeof data.incidents_error==='string'&&data.incidents_error)section.append(el('p','delivery-error',T('Incident bookkeeping failed')+': '+data.incidents_error));
    if(data.incidents.length){section.append(el('h3',null,T('Open incidents')));const list=el('ul','delivery-incidents');for(const incident of data.incidents)list.append(incidentRow(incident));section.append(list);}
    const list=el('ul','delivery-projects');
    for(const project of data.projects)list.append(projectRow(project));
    if(!data.projects.length)section.append(el('p','delivery-note',T('No project outbox is registered with the supervisor.')));
    else section.append(list);
    return section;
  }
  function repaint(){for(const old of document.querySelectorAll('.delivery-panel'))old.replaceWith(build());}
  async function refresh(){
    if(loading)return;loading=true;
    try{
      const next=await api('/api/outbox-health');
      if(!valid(next))throw Error('Unexpected delivery status');
      data=next;failed=false;
    }catch(error){failed=true;data=null;}
    repaint();
    try{
      if(data){
        const names=data.projects.filter(project=>waitingCount(project)>0).map(project=>project.name);
        for(const name of [...reportsByProject.keys()])if(!names.includes(name))reportsByProject.delete(name);
        if(!acting)await Promise.all(names.map(loadReports));
        if(confirming&&!(reportsByProject.get(confirming.project)?.rows||[]).some(report=>report.id===confirming.id))confirming=null;
      }
    }finally{loading=false;}
    repaint();
  }
  function paint(parent){
    parent.append(build());
    if(!data&&!failed)refresh();
  }
  setInterval(()=>{if(document.querySelector('.delivery-panel'))refresh();},30000);
  window.addEventListener('office-language-changed',repaint);
  window.officeDelivery={paint,build,refresh,ageText};
})();
