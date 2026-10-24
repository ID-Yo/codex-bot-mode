(()=>{
  const details=$('new-team-details'),form=$('new-team-form'),select=$('team-template'),name=$('team-name'),box=$('team-questions'),out=$('team-plan-output'),go=$('team-launch'),notice=$('rooms-notice');
  let templates=[],planned=null,clientId=null;
  const mode=()=>form.querySelector('input[name=team-mode]:checked').value;
  const current=()=>templates.find(t=>t.name===select.value);
  function renderQuestions(){
    box.replaceChildren();box.hidden=false;const t=current();if(!t)return;
    for(const q of t.team.filter(q=>q.name!=='team_name')){const label=document.createElement('label');label.textContent=q.question;const input=document.createElement('select');input.dataset.team=q.name;input.append(new Option('Choose...', ''),...q.choices.map(v=>new Option(v,v)));label.append(input);box.append(label);}
    if(mode()!=='questions')return;
    for(const role of t.roles){const group=document.createElement('fieldset');const legend=document.createElement('legend');legend.textContent=role;group.append(legend);
      for(const q of t.agents[role]){const label=document.createElement('label');label.textContent=q.question;const input=document.createElement('input');input.dataset.role=role;input.dataset.name=q.name;input.placeholder=q.default??'';input.maxLength=200;label.append(input);group.append(label);}
      box.append(group);}
  }
  function answers(){const out={};if(mode()!=='questions')return out;for(const input of box.querySelectorAll('input')){if(input.value.trim())(out[input.dataset.role]??={})[input.dataset.name]=input.value.trim();}return out;}
  function teamAnswers(){return Object.fromEntries([...box.querySelectorAll('select[data-team]')].filter(i=>i.value).map(i=>[i.dataset.team,i.value]));}
  function reset(){planned=null;go.hidden=true;out.hidden=true;}
  details.addEventListener('toggle',async()=>{if(!details.open||templates.length)return;
    try{const data=await api('/api/teams');templates=data.templates;select.replaceChildren(...templates.map(t=>new Option(t.name[0].toUpperCase()+t.name.slice(1)+' · '+t.roles.length+' roles',t.name)));
      name.placeholder=current()?.team[0]?.default||'';renderQuestions();}catch(error){notice.textContent=officeI18n.t(error.message);}});
  select.addEventListener('change',()=>{name.placeholder=current()?.team[0]?.default||'';renderQuestions();reset();});
  form.addEventListener('change',event=>{if(event.target.name==='team-mode')renderQuestions();reset();});
  form.addEventListener('submit',async event=>{event.preventDefault();reset();
    try{const plan=await api('/api/teams/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template:select.value,team_name:name.value,answers:answers(),team_answers:teamAnswers()})});
      planned={template:select.value,team_name:name.value,answers:answers(),team_answers:teamAnswers()};clientId=crypto.randomUUID().replaceAll('-','');
      out.textContent=['Team: '+plan.team,'Sidebar section: '+plan.section,'Room: '+plan.room,...plan.agents.map(a=>'• '+a.display_name+' ('+a.role+') → '+a.workspace),...(plan.director_proposal?['','Project Director proposed: '+plan.director_proposal]:[])].join('\n');
      out.textContent+='\nTracking: '+(plan.work_tracking.mode||'Choose a mode')+'\nStatus authority: '+(plan.work_tracking.authority||'Choose an authority for both');
      out.hidden=false;go.hidden=!plan.work_tracking.mode||!plan.work_tracking.authority;notice.textContent='';}catch(error){notice.textContent=officeI18n.t(error.message);}});
  go.addEventListener('click',async()=>{if(!planned)return;go.disabled=true;
    try{const result=await api('/api/teams/launch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...planned,client_message_id:clientId})});
      notice.textContent=result.state==='sent'?'Sent to '+botName(result.manager)+' ('+result.request+'). The chats appear in Codex; restart Office to see the new room.':result.state==='uncertain'?'Delivery unconfirmed ('+result.request+'); check the Manager chat before trying again.':'Already requested ('+result.request+').';
      reset();details.open=false;}catch(error){notice.textContent=officeI18n.t(error.message);}finally{go.disabled=false;}});
})();
