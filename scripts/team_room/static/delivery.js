(()=>{
  // Delivery panel (#424, S4-D): read-only view of the supervisor's status file through /api/outbox-health.
  // Office never calls Desktop or the relay for it. Unknown is shown as Unknown, never as healthy or zero.
  let data=null,failed=false,loading=false;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
  const T=(key,values)=>officeI18n.t(key,values);
  const STATE={ok:'Healthy',problems:'Problems',unknown:'Unknown'};
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
    if(Number.isFinite(project.needs_attention)&&project.needs_attention>0)meta.append(tag(T('{n} need attention',{n:project.needs_attention})));
    if(Number.isFinite(project.ambiguous)&&project.ambiguous>0)meta.append(tag(T('{n} unconfirmed',{n:project.ambiguous})));
    row.append(meta);
    return row;
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
    }catch(error){failed=true;data=null;}finally{loading=false;}
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
