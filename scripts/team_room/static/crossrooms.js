/* Cross-project rooms: participant/lead page (#212). Eligibility, hierarchy and membership are decided by the server. */
(()=>{
  const t=(key,values)=>officeI18n.t(key,values),$=id=>document.getElementById(id);
  const REASONS={not_appointed:'Not an appointed agent',no_desktop_thread:'No Codex Desktop chat is bound',project_unavailable:'Project is unavailable'};
  const ERRORS={invalid_participants:'Select 2 to 24 different participants.',invalid_lead:'Choose the lead from the selected participants.',unknown_room:'This room no longer exists.',idempotency_conflict:'This request was already used with different content. Review it and save again.'};
  const ERRORS_REGISTRY='The agent registry is unavailable, so no agents can be listed. Check the registry and try again.';
  const BASIS={hierarchy:'By hierarchy',manual:'Manual choice',override:'Hierarchy overridden'};
  const empty=()=>({id:null,name:'',version:null,selected:new Map(),lead:'',override:false,query:''});
  let draft=empty(),candidates=[],loadState='idle',error='',notice='',busy=false,tie=null,seq=0,timer=0,creation=null,blocked=new Map();
  function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
  const roomOf=id=>(typeof rooms!=='undefined'?rooms:[]).find(room=>room.id===id);
  async function call(path,options={}){
    let response;
    try{response=await fetch(path,{...options,headers:{'X-Room-Token':document.querySelector('meta[name="room-token"]').content,...(options.body?{'Content-Type':'application/json'}:{})}});}
    catch{const failure=Error('Office is unavailable. Try again.');failure.code='unavailable';throw failure;}
    const data=await response.json().catch(()=>({}));
    if(!response.ok){const failure=Error(data.error||'Request failed');failure.code=data.code||'error';failure.data=data;throw failure;}
    return data;
  }
  async function search(){
    const mine=++seq,recovering=loadState==='failed';loadState='loading';paintList();
    try{const data=await call('/api/rooms/candidates?q='+encodeURIComponent(draft.query.trim()));if(mine!==seq)return;candidates=data.agents||[];loadState='ready';if(recovering)error='';}
    catch(e){if(mine!==seq)return;candidates=[];loadState='failed';error=e.code==='registry_unavailable'?ERRORS_REGISTRY:e.message;}
    paintList();paintStatus();
  }
  const rankOf=key=>draft.selected.get(key)?.rank??0;
  function hierarchy(){
    const members=[...draft.selected.values()];if(!members.length)return {top:[],rank:0};
    const rank=Math.max(...members.map(m=>m.rank||0));return {top:members.filter(m=>(m.rank||0)===rank),rank};
  }
  function leadText(){
    const {top}=hierarchy();
    if(!top.length)return t('Select participants to see who leads.');
    if(draft.lead){const lead=draft.selected.get(draft.lead);return lead?t('{name} will lead ({basis}).',{name:lead.name,basis:t(draft.override?BASIS.override:BASIS.manual)}):'';}
    if(top.length===1)return t('{name} leads by hierarchy ({role}).',{name:top[0].name,role:t(top[0].role)});
    return t('Several participants share the highest rank; choose the lead below.');
  }
  function overrideNeeded(){const {top,rank}=hierarchy();return Boolean(draft.lead&&draft.selected.has(draft.lead)&&rankOf(draft.lead)<rank&&top.length);}
  function toggle(agent,on){
    if(on)draft.selected.set(agent.bot_key,agent);else{draft.selected.delete(agent.bot_key);if(draft.lead===agent.bot_key)draft.lead='';}
    if(!overrideNeeded())draft.override=false;tie=null;error='';paint();
  }
  function paintList(){
    const list=$('re-candidates');if(!list)return;list.replaceChildren();
    if(loadState==='loading'){list.append(el('p','re-note',t('Searching…')));return;}
    if(loadState==='failed'){const retry=el('button','secondary',t('Try again'));retry.type='button';retry.id='re-retry';retry.onclick=search;list.append(el('p','re-note re-error',t(error||'Candidates could not be loaded.')),retry);return;}
    if(!candidates.length){list.append(el('p','re-note',t('No matching agents.')));return;}
    for(const agent of candidates){
      const row=el('label','re-row'+(agent.eligible&&!blocked.has(agent.bot_key)?'':' disabled'));row.dataset.botKey=agent.bot_key;
      const box=el('input');box.type='checkbox';box.checked=draft.selected.has(agent.bot_key);box.disabled=!agent.eligible||blocked.has(agent.bot_key);box.onchange=()=>toggle(agent,box.checked);
      const copy=el('span','re-copy');copy.append(el('strong',null,agent.name),el('small',null,t(agent.role)+' · '+agent.project_name+(agent.team_name?' · '+agent.team_name:'')));
      row.append(box,copy);
      const why=!agent.eligible?t(REASONS[agent.reason_code]||agent.reason||'Not available'):blocked.get(agent.bot_key);
      if(why)row.append(el('small','re-reason',why));
      list.append(row);
    }
  }
  function paintStatus(){const box=$('re-error');if(!box)return;box.textContent=error?t(error):'';box.hidden=!error;}
  function paintSelection(){
    const box=$('re-selected');if(!box)return;box.replaceChildren();
    if(!draft.selected.size){box.append(el('p','re-note',t('No participants selected.')));return;}
    for(const agent of draft.selected.values()){
      const row=el('div','re-chip');row.append(el('strong',null,agent.name),el('small',null,t(agent.role)+' · '+agent.project_name));
      const drop=el('button','secondary',t('Remove'));drop.type='button';drop.setAttribute('aria-label',t('Remove {name}',{name:agent.name}));drop.onclick=()=>toggle(agent,false);row.append(drop);box.append(row);
    }
  }
  function paint(){
    const view=$('room-editor-view');if(!view)return;
    const focused=document.activeElement?.id,page=el('div','re-page'),editing=Boolean(draft.id);
    const head=el('header','pc-header'),title=el('div');title.append(el('h1',null,t(editing?'Participants and lead':'New room')),el('span','room-description',t('Choose agents from any project. The server checks who may join.')));head.append(title);page.append(head);
    const form=el('form','re-form');form.id='re-form';
    const name=el('input');name.id='re-name';name.type='text';name.maxLength=80;name.required=!editing;name.value=draft.name;name.readOnly=editing;name.oninput=()=>{draft.name=name.value;};
    const nameLabel=el('label','re-field');nameLabel.append(el('span','re-label',t('Room name')),name);form.append(nameLabel);
    const search$=el('input');search$.id='re-search';search$.type='search';search$.value=draft.query;search$.placeholder=t('Search by name, role or project');search$.setAttribute('aria-label',t('Search agents'));
    search$.oninput=()=>{draft.query=search$.value;clearTimeout(timer);timer=setTimeout(search,250);};
    const searchLabel=el('label','re-field');searchLabel.append(el('span','re-label',t('Agents')),search$);form.append(searchLabel);
    const list=el('div','re-candidates');list.id='re-candidates';form.append(list);
    form.append(el('h2',null,t('Selected')));const selected=el('div','re-selected');selected.id='re-selected';form.append(selected);
    const lead=el('select');lead.id='re-lead';
    const auto=el('option',null,t('By hierarchy (automatic)'));auto.value='';lead.append(auto);
    for(const agent of draft.selected.values()){const option=el('option',null,agent.name+' · '+t(agent.role)+' · '+agent.project_name);option.value=agent.bot_key;lead.append(option);}
    lead.value=draft.selected.has(draft.lead)?draft.lead:'';lead.onchange=()=>{draft.lead=lead.value;if(!overrideNeeded())draft.override=false;tie=null;error='';paint();};
    const leadLabel=el('label','re-field');leadLabel.append(el('span','re-label',t('Lead')),lead);form.append(leadLabel);
    const preview=el('p','re-note');preview.id='re-lead-note';preview.textContent=leadText();form.append(preview);
    if(tie){const warn=el('p','re-note re-error');warn.id='re-tie';warn.textContent=t('Several participants share the highest rank. Choose one as lead: {names}.',{names:tie.map(key=>draft.selected.get(key)?.name||key).join(', ')});form.append(warn);}
    if(overrideNeeded()||draft.overrideAsked){
      const check=el('input');check.type='checkbox';check.id='re-override';check.checked=draft.override;check.onchange=()=>{draft.override=check.checked;};
      const row=el('label','re-check');row.append(check,el('span',null,t('Override the hierarchy: this lead ranks below the highest-ranked participant.')));form.append(row);
    }
    const status=el('p','re-note re-error');status.id='re-error';status.setAttribute('role','alert');status.hidden=!error;status.textContent=error?t(error):'';form.append(status);
    if(notice)form.append(el('p','re-note',t(notice)));
    const actions=el('div','re-actions');
    const save=el('button',null,t(busy?'Saving…':editing?'Save participants':'Create room'));save.type='submit';save.id='re-save';save.disabled=busy||!draft.selected.size||(!editing&&!draft.name.trim());
    const cancel=el('button','secondary',t('Cancel'));cancel.type='button';cancel.id='re-cancel';cancel.onclick=()=>$('page-back').click();
    actions.append(save,cancel);form.append(actions);form.onsubmit=event=>{event.preventDefault();submit();};
    page.append(form);view.replaceChildren(page);paintList();paintSelection();
    if(focused&&$(focused)&&!$(focused).disabled&&focused!=='re-save')$(focused).focus();
  }
  function payload(){
    const body={participants:[...draft.selected.keys()]};
    if(draft.lead)body.lead=draft.lead;if(draft.override&&draft.lead)body.lead_override=true;
    return body;
  }
  async function submit(){
    if(busy)return;
    if(draft.selected.size<2||draft.selected.size>24){error=ERRORS.invalid_participants;paint();return;}
    busy=true;error='';notice='';paint();
    try{
      let room;
      if(draft.id){room=await call('/api/rooms/'+encodeURIComponent(draft.id)+'/participants',{method:'POST',body:JSON.stringify({...payload(),version:draft.version})});}
      else{
        const key=JSON.stringify([draft.name.trim(),payload()]);if(!creation||creation.key!==key)creation={key,id:crypto.randomUUID()};
        room=await call('/api/rooms',{method:'POST',body:JSON.stringify({name:draft.name.trim(),...payload(),client_message_id:creation.id})});
      }
      creation=null;const id=room.id||draft.id;draft=empty();candidates=[];await loadRooms();busy=false;await openRoom(id);return;
    }catch(e){
      const data=e.data||{};
      if(e.code==='lead_required'){tie=data.candidates||[];error='Choose the lead before saving.';}
      else if(e.code==='override_required'){draft.overrideAsked=true;error='This lead ranks below the highest-ranked participant. Confirm the override to continue.';}
      else if(e.code==='not_allowed'){if(data.bot_key){blocked.set(data.bot_key,t('The server refused this agent.'));draft.selected.delete(data.bot_key);if(draft.lead===data.bot_key)draft.lead='';}error=REASONS[data.reason_code]||e.message;}
      else if(ERRORS[e.code])error=ERRORS[e.code];
      else if(e.code==='version_conflict'){error='The room changed elsewhere. Reload the room and try again.';}
      else error=e.message;
    }finally{busy=false;paint();}
  }
  function show(){
    if(window.officeShell&&!window.officeShell.canNavigate())return false;
    window.officeShell?.activate('room-editor',t(draft.id?'Participants and lead':'New room'),()=>show());
    officeMedia.stop();officeSpeech.stop();officeView='room-editor';selectMenu('rooms');
    for(const id of ['room-view','personal-view','session-detail','projects-view','new-project-view','directory-view'])if($(id))$(id).hidden=true;
    $('room-editor-view').hidden=false;paint();search();return true;
  }
  function openNew(){draft=empty();error='';notice='';tie=null;blocked=new Map();creation=null;show();}
  function openEdit(id){
    const room=roomOf(id);if(!room||room.kind!=='cross_project')return;
    draft=empty();draft.id=id;draft.name=room.name;draft.version=room.version;
    for(const p of room.participants||[])draft.selected.set(p.bot_key,p);
    if(room.lead&&room.lead.basis!=='hierarchy'){draft.lead=room.lead.bot_key;draft.override=room.lead.basis==='override';}
    error='';notice='';tie=null;blocked=new Map();show();
  }
  function afterRender(id){
    const room=roomOf(id),cross=room?.kind==='cross_project';window.officeOutcomes?.sync(id,cross);
    const edit=$('room-edit-members');if(edit)edit.hidden=!cross;
    const line=$('room-lead');if(!line)return;line.hidden=!cross;
    if(!cross){return;}
    const lead=room.lead;line.textContent=lead?t('{name} leads this room · {role} · {basis}',{name:lead.name,role:t(lead.role),basis:t(BASIS[lead.basis]||lead.basis)}):t('No lead is set.');
    $('room-member-count').textContent=t('{count} participants',{count:(room.participants||[]).length});
    const roster=document.querySelector('#room-view .room-members .roster');
    if(roster){roster.replaceChildren();for(const p of room.participants||[]){const row=el('div','participant-row');row.append(el('strong',null,p.name+(lead&&lead.bot_key===p.bot_key?' · '+t('Lead'):'')),el('small',null,t(p.role)+' · '+p.project_name));roster.append(row);}}
  }
  window.officeCrossRooms={openNew,openEdit,afterRender,call};
  document.addEventListener('DOMContentLoaded',()=>{$('room-edit-members').onclick=()=>openEdit(currentRoomId);$('new-room-page').onclick=openNew;});
  window.addEventListener('office-language-changed',()=>{if($('room-editor-view')&&!$('room-editor-view').hidden)paint();afterRender(currentRoomId);});
})();
