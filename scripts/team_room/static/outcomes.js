/* Cross-project rooms: explicit result requests, approval and evidence-based status (#213).
   Discussion messages never start work; only this form does. Status labels never claim more than the server proved. */
(()=>{
  const t=(key,values)=>officeI18n.t(key,values),$=id=>document.getElementById(id),enc=encodeURIComponent;
  const MODES={plan_approval:'Plan for my approval',organize_start:'Organize and start now'};
  const OUTCOME={awaiting_approval:'Waiting for your approval',organizing:'Organizing',dispatched:'Dispatched',blocked:'Blocked',failed:'Failed',cancelled:'Cancelled'};
  const ASSIGNMENT={proposed:'Proposed',approved:'Approved',dispatching:'Sending, not accepted yet',accepted:'Accepted',task_linked:'Task linked',blocked:'Blocked',failed:'Failed'};
  const ERRORS={not_authorized:'Starting work needs your explicit confirmation.',version_conflict:'This result changed. The list was reloaded; check it and try again.',idempotency_conflict:'This request was already used with different text. Send it again.',
    not_awaiting_approval:'This result is no longer waiting for approval. The list was reloaded.',not_cancellable:'The work was already sent and cannot be recalled here.',
    invalid_issue_url:'Enter a GitHub issue URL such as https://github.com/owner/repo/issues/1.',no_projects:'No participating project can receive work.',
    too_many_outcomes:'This room has too many results. Review the existing ones first.',invalid_text:'Describe the result you want (1 to 4000 characters).',invalid_mode:'Choose how the result should run.',
    outcome_state_unsaved:'Work may already be delivered but its state could not be saved. Check the list before acting.',storage_unavailable:'Result storage is unavailable; nothing was sent.',
    unknown_outcome:'This result no longer exists. The list was reloaded.',unknown_assignment:'This assignment no longer exists. The list was reloaded.'};
  const STOPPED=new Set(['blocked','failed']),PROVEN=new Set(['accepted','task_linked']),CANCELLABLE=new Set(['awaiting_approval']),LINKABLE=new Set(['accepted','blocked','failed']);
  const ISSUE_URL=/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/issues\/\d+$/,issueDrafts=new Map();
  const stores=new Map(),forms=new Map(),confirming=new Set(),busy=new Set();
  let roomId=null,timer=0,loading=0,signature='';
  function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
  const form=id=>{if(!forms.has(id))forms.set(id,{text:'',mode:'plan_approval',confirm:false,request:null,busy:false,error:''});return forms.get(id);};
  const stamp=v=>typeof v==='number'?(v<1e12?v*1000:v):(Date.parse(v)||0);
  const when=v=>{const ms=stamp(v);return ms?new Date(ms).toLocaleString(officeI18n.language,{dateStyle:'medium',timeStyle:'short'}):t('Unknown');};
  const safeUrl=value=>{try{const url=new URL(value);return url.protocol==='https:'||url.protocol==='http:'?url.href:null;}catch{return null;}};
  const room=()=>(typeof rooms!=='undefined'?rooms:[]).find(item=>item.id===roomId);
  const projectName=a=>(room()?.participants||[]).find(p=>p.project_id===a.project_id)?.project_name||a.project_id;
  const projects=list=>[...new Set(list.map(projectName))].join(', ');
  const person=key=>{const p=(room()?.participants||[]).find(item=>item.bot_key===key);return p?{name:p.name,project:p.project_name}:{name:key||'',project:''};};
  // Only a recorded delivery counts as proof that a Manager received the work; an issue link alone does not.
  const proof=a=>Boolean(a.delivery_id&&a.accepted_at);
  const assignmentLabel=a=>PROVEN.has(a.state)&&!proof(a)?'Waiting for proof of acceptance':ASSIGNMENT[a.state]||a.state;
  const confirmedDispatch=o=>Boolean(o.assignments?.length)&&o.assignments.every(a=>PROVEN.has(a.state)&&proof(a));
  const outcomeLabel=o=>o.state==='dispatched'&&!confirmedDispatch(o)?'Dispatch not confirmed':OUTCOME[o.state]||o.state;
  const message=e=>t(ERRORS[e.code]||(e.code==='unavailable'?'Office is unavailable. Try again.':'')||'')||e.message;
  const call=(path,options)=>window.officeCrossRooms.call(path,options);
  function link(url,label){const href=safeUrl(url);if(!href)return null;const a=el('a',null,label);a.href=href;a.target='_blank';a.rel='noopener noreferrer';return a;}

  function paintSummary(){
    const items=stores.get(roomId)?.items||[],waiting=items.filter(o=>o.state==='awaiting_approval').length;
    const summary=$('ro-summary');summary.textContent=t('Results')+(items.length?' · '+items.length:'')+(waiting?' · '+t('{count} waiting for your approval',{count:waiting}):'');
    $('room-outcomes').dataset.attention=waiting?'true':'false';
  }
  function paintError(){const f=form(roomId);$('ro-error').textContent=f.error||(stores.get(roomId)?.error?t(stores.get(roomId).error):'');}
  function paintForm(){
    const f=form(roomId);
    if($('ro-text').value!==f.text)$('ro-text').value=f.text;
    for(const radio of document.querySelectorAll('input[name="ro-mode"]'))radio.checked=radio.value===f.mode;
    $('ro-confirm').hidden=!f.confirm;$('ro-submit').hidden=f.confirm;
    for(const id of ['ro-submit','ro-confirm-yes','ro-confirm-no','ro-text'])$(id).disabled=f.busy;
    paintError();
  }
  function paintList(force){
    const entry=stores.get(roomId),items=[...(entry?.items||[])].sort((a,b)=>stamp(b.created)-stamp(a.created));
    const next=JSON.stringify([roomId,items,[...confirming],[...busy],officeI18n.language,room()?.participants]);
    if(!force&&next===signature)return;signature=next;
    const list=$('ro-list');list.replaceChildren();
    if(!items.length){list.append(el('li','ro-empty',t('No results have been requested in this room.')));return;}
    for(const o of items)list.append(paintItem(o,items));
  }
  function paintItem(o){
    const li=el('li','ro-item');li.dataset.outcome=o.id;li.dataset.state=o.state;
    const head=el('div','ro-head');head.append(el('strong',null,t(MODES[o.mode]||o.mode)),el('span','ro-state',t(outcomeLabel(o))));
    const time=el('time','ro-time',when(o.created));if(stamp(o.created))time.dateTime=new Date(stamp(o.created)).toISOString();head.append(time);
    li.append(head,el('p','ro-text',o.text));
    if(o.lead_bot_key)li.append(el('small','ro-lead',t('Lead: {name}',{name:person(o.lead_bot_key).name})));
    if(o.assignments?.length){
      const titles=new Map(o.assignments.map(a=>[a.id,a.title])),ul=el('ul','ro-assignments');
      for(const a of o.assignments){
        const row=el('li','ro-assignment');row.dataset.assignment=a.id;row.dataset.state=a.state;row.dataset.proven=String(proof(a));
        const who=person(a.manager_bot_key),top=el('div','ro-a-head');top.append(el('strong',null,a.title),el('span','ro-astate',t(assignmentLabel(a))));
        const meta=el('small','ro-meta',[who.name,who.project||a.project_id].filter(Boolean).join(' · '));
        row.append(top,meta);
        if(a.depends_on?.length)row.append(el('small','ro-meta',t('After: {names}',{names:a.depends_on.map(id=>titles.get(id)||id).join(', ')})));
        if((a.state==='blocked'||a.state==='failed')&&a.reason)row.append(el('small','ro-reason',a.reason));
        if(proof(a))row.append(el('small','ro-meta',t('Accepted {time}',{time:when(a.accepted_at)})));
        const issue=link(a.issue_url,t('Open task issue'));
        if(issue)row.append(issue);
        else if(LINKABLE.has(a.state))row.append(issueForm(o,a));
        ul.append(row);
      }
      li.append(ul);
    }
    if(o.blocker){
      const noIssue=o.blocker.issue_required===true||!safeUrl(o.blocker.issue_url);
      const box=el('div','ro-blocker');box.dataset.issueRequired=String(noIssue);box.append(el('span',null,o.blocker.reason||t('Work is blocked.')));
      const issue=link(o.blocker.issue_url,t('Open linked issue'));if(issue)box.append(issue);
      if(noIssue)box.append(el('strong','ro-issue-required',t('Blocker without issue')),el('small','ro-meta',t('Every stop needs a linked issue. Link the issue on the blocked assignment above to clear this.')));
      li.append(box);
    }else if(o.state==='blocked'||o.state==='failed'){const box=el('div','ro-blocker');box.append(el('span',null,t('Work is blocked.')));li.append(box);}
    const actions=el('div','ro-actions'),working=busy.has(o.id),stopped=(o.assignments||[]).filter(a=>STOPPED.has(a.state)),sendable=(o.assignments||[]).filter(a=>!STOPPED.has(a.state)),nothingToSend=Boolean(o.assignments?.length)&&!sendable.length;
    if(o.state==='awaiting_approval'){
      if(confirming.has(o.id)){
        const sends=stopped.length?t('Will be sent: {sent} ({names}). Blocked and will not be sent: {blocked} ({blockedNames}).',{sent:sendable.length,names:projects(sendable),blocked:stopped.length,blockedNames:projects(stopped)}):t('Approving sends these assignments to the project Managers now.');
        const warn=el('p','ro-warn',sends);warn.dataset.partial=String(stopped.length>0);actions.append(warn);
        const yes=el('button',null,t('Approve and start'));yes.type='button';yes.disabled=working;yes.dataset.action='approve-confirm';yes.onclick=()=>act(o,'approve');
        const back=el('button','secondary',t('Back'));back.type='button';back.disabled=working;back.onclick=()=>{confirming.delete(o.id);paintList();};actions.append(yes,back);
      }else{const approve=el('button',null,t('Approve'));approve.type='button';approve.dataset.action='approve';approve.disabled=working||nothingToSend;approve.onclick=()=>{confirming.add(o.id);paintList();};actions.append(approve);
        if(nothingToSend){const why=el('p','ro-warn',t('Every assignment is blocked, so there is nothing to send. Link the issues or fix the blockers first.'));why.dataset.nothingToSend='true';actions.append(why);}}
    }
    if(CANCELLABLE.has(o.state)){const cancel=el('button','secondary',t('Cancel result'));cancel.type='button';cancel.dataset.action='cancel';cancel.disabled=working;cancel.onclick=()=>act(o,'cancel');actions.append(cancel);}
    if(actions.children.length)li.append(actions);
    return li;
  }
  function issueForm(o,a){
    const key=o.id+'/'+a.id,wrap=el('div','ro-issue'),input=el('input');
    input.type='url';input.placeholder='https://github.com/owner/repo/issues/1';input.setAttribute('aria-label',t('GitHub issue URL'));input.value=issueDrafts.get(key)||'';input.dataset.issueFor=a.id;
    input.oninput=()=>issueDrafts.set(key,input.value);
    const go=el('button','secondary',t('Link issue'));go.type='button';go.dataset.action='link-issue';go.disabled=busy.has(o.id);
    go.onclick=()=>linkIssue(o,a,input.value.trim());
    wrap.append(input,go);return wrap;
  }
  async function linkIssue(o,a,url){
    const id=roomId,f=form(id);
    if(!ISSUE_URL.test(url)){f.error=t(ERRORS.invalid_issue_url);paintError();return;}
    busy.add(o.id);f.error='';paintList();
    try{await call('/api/rooms/'+enc(id)+'/outcomes/'+enc(o.id)+'/assignments/'+enc(a.id)+'/issue',{method:'POST',body:JSON.stringify({version:o.version,issue_url:url})});issueDrafts.delete(o.id+'/'+a.id);}
    catch(e){f.error=message(e);}
    busy.delete(o.id);if(id===roomId)paintError();await load();paintList();
  }
  async function load(){
    const id=roomId;if(!id)return;const mine=++loading;
    try{const data=await call('/api/rooms/'+enc(id)+'/outcomes');if(id!==roomId||mine!==loading)return;stores.set(id,{items:data.items||[],error:''});}
    catch{if(id!==roomId||mine!==loading)return;stores.set(id,{items:stores.get(id)?.items||[],error:'Results could not be refreshed'});}
    paintList();paintSummary();paintError();
  }
  async function act(o,kind){
    const id=roomId,f=form(id);busy.add(o.id);f.error='';paintList();
    try{await call('/api/rooms/'+enc(id)+'/outcomes/'+enc(o.id)+'/'+kind,{method:'POST',body:JSON.stringify(kind==='approve'?{version:o.version,confirmed:true}:{version:o.version})});confirming.delete(o.id);}
    catch(e){f.error=message(e);if(['version_conflict','not_awaiting_approval','not_cancellable'].includes(e.code))confirming.delete(o.id);}
    busy.delete(o.id);if(id===roomId)paintError();await load();paintList();
  }
  async function submit(confirmed){
    const id=roomId,f=form(id),text=f.text.trim();
    if(!text){$('ro-text').reportValidity();return;}
    if(f.mode==='organize_start'&&!confirmed){f.confirm=true;paintForm();$('ro-confirm-yes').focus();return;}
    const key=JSON.stringify([text,f.mode]);if(!f.request||f.request.key!==key)f.request={key,id:crypto.randomUUID()};
    f.busy=true;f.error='';paintForm();
    try{
      await call('/api/rooms/'+enc(id)+'/outcomes',{method:'POST',body:JSON.stringify({text,mode:f.mode,client_message_id:f.request.id,...(f.mode==='organize_start'?{confirmed:true}:{})})});
      f.text='';f.request=null;f.confirm=false;
    }catch(e){
      if(e.code==='idempotency_conflict')f.request=null;
      f.confirm=false;f.error=e.code==='unavailable'?t('Office is unavailable. Check the list before sending again.'):message(e);
    }
    f.busy=false;if(id===roomId){paintForm();await load();}
  }
  function sync(id,cross){
    const box=$('room-outcomes');if(!box)return;
    if(!cross){box.hidden=true;roomId=null;signature='';return;}
    box.hidden=false;
    if(id!==roomId){roomId=id;signature='';paintForm();paintList(true);paintSummary();load();}
    if(!timer)timer=setInterval(()=>{if(roomId&&!document.hidden&&typeof officeView!=='undefined'&&officeView==='room')load();},5000);
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('ro-form').addEventListener('submit',e=>{e.preventDefault();if(roomId)submit(false);});
    $('ro-text').addEventListener('input',()=>{if(!roomId)return;const f=form(roomId);f.text=$('ro-text').value;f.confirm=false;f.error='';paintForm();});
    for(const radio of document.querySelectorAll('input[name="ro-mode"]'))radio.addEventListener('change',()=>{if(!roomId)return;const f=form(roomId);f.mode=radio.value;f.confirm=false;paintForm();});
    $('ro-confirm-yes').onclick=()=>{if(roomId)submit(true);};
    $('ro-confirm-no').onclick=()=>{if(!roomId)return;form(roomId).confirm=false;paintForm();};
  });
  window.addEventListener('office-language-changed',()=>{if(roomId){signature='';paintList(true);paintSummary();}});
  window.officeOutcomes={sync};
})();
