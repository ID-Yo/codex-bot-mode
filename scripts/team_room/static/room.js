const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="room-token"]').content;
const names={owner:officeI18n.t('You'),manager:officeI18n.t('Manager'),developer:officeI18n.t('Developer'),verifier:officeI18n.t('Verifier')},roleOf={};let leadBot='manager';
let state, cursor = 0, pendingRequest = null, refreshing = false, actionError = '';
let currentRoomId='development',rooms=[{id:'development',name:'Development',bots:['manager','developer','verifier']}];
const roomDrafts=new Map(),roomStates=new Map(),roomPending=new Map(),roomInflight=new Set();
function roomPath(path,id=currentRoomId){return id==='development'?path:'/api/rooms/'+encodeURIComponent(id)+path.slice(4);}
function roomScope(id=currentRoomId){return id==='development'?'room':'room:'+id;}
function visibleRoomError(error){
  return error==='Restart requires reconciliation and explicit Resume'?'':error||'';
}
function render(s) {
  const conversation=$('room-view').querySelector('.conversation'),scrollTop=conversation.scrollTop;
  const follow=conversation.scrollHeight-conversation.clientHeight-scrollTop<40;
  state = s; cursor = Math.max(cursor, s.event_cursor);
  $('status').textContent = officeI18n.state(s.status);
  $('budget').textContent = `${s.turns} / 4 turns`;
  $('elapsed').textContent = s.remaining_seconds === null ? '120s available' : `${Math.ceil(s.remaining_seconds)}s remaining`;
  $('error').textContent = actionError || visibleRoomError(s.error);
  const available=new Set(s.participants.map(p=>p.bot));
  document.querySelectorAll('[data-recipient]').forEach(button=>{button.hidden=!available.has(button.dataset.recipient);button.disabled=!available.has(button.dataset.recipient);});
  $('room-member-count').textContent=officeI18n.t('{count} participants',{count:available.size});window.officeCrossRooms?.afterRender(currentRoomId);
  selectRecipient();
  $('journal').replaceChildren();
  if (!s.journal.length) { const p=document.createElement('p'); p.className='empty'; p.textContent=officeI18n.t('Start a discussion in this room.'); $('journal').append(p); }
  for (const m of s.journal) {
    const article=document.createElement('article'); article.className=`message ${m.sender}`; article.dataset.messageId=m.id;
    const head=document.createElement('div');head.className='message-head';
    if(m.sender!=='owner'){const avatar=document.createElement('span');avatar.className=`avatar ${m.sender}`;avatar.textContent=names[m.sender][0];avatar.setAttribute('aria-hidden','true');head.append(avatar);}
    const author=document.createElement('span'); author.className='author'; author.textContent=m.sender==='owner'?officeI18n.t('You'):names[m.sender];
    const timestamp=document.createElement('time');timestamp.className='timestamp';timestamp.dateTime=new Date(m.created*1000).toISOString();timestamp.textContent=new Date(m.created*1000).toLocaleTimeString(officeI18n.language,{hour:'2-digit',minute:'2-digit'});
    head.append(author,timestamp);
    if(m.sender!=='owner'&&m.text)head.append(officeSpeech.button('room:'+currentRoomId+':'+m.id,m.text));
    if(m.sender==='owner'&&['room','team'].includes(m.recipient)){const route=document.createElement('small');route.className='message-route';route.textContent=m.recipient==='room'?(s.deliveries.some(d=>d.message_id===m.id&&d.mode==='intent')?officeI18n.t('Manager'):'General · optional replies'):'Everyone · requested replies';head.append(route);}
    const hashLine=m.recipient==='work'?m.text.match(/^SHA-256: [a-f0-9]{64}$/m):null;
    const text=officeMedia.message({...m,text:hashLine?m.text.replace(hashLine[0]+'\n',''):m.text});article.append(head,text);
    if(hashLine){const details=document.createElement('details'),summary=document.createElement('summary'),hash=document.createElement('p');summary.textContent=officeI18n.t('Evidence');hash.textContent=hashLine[0];details.append(summary,hash);article.append(details);}
    $('journal').append(article);
  }
  $('provisional').replaceChildren();
  for (const [bot,text] of Object.entries(s.provisional)) {
    const p=document.createElement('div'); p.className='provisional'; p.textContent=`${bot} · provisional\n${text}`; $('provisional').append(p);
  }
  $('deliveries').replaceChildren();
  const latestMessage=s.deliveries.at(-1)?.message_id;
  for (const d of s.deliveries.filter(d=>!['completed','cancelled'].includes(d.state)||(d.decision==='abstain'&&d.message_id===latestMessage))) {
    const p=document.createElement('p'); p.className='delivery'; p.textContent=`${d.bot}: ${d.decision==='abstain'?'abstained':d.mode==='voluntary'&&d.state==='accepted'?'considering':d.state}${d.error ? ' вЂ” '+d.error : ''}`; $('deliveries').append(p);
  }
  $('resume').disabled=!s.hold||Boolean(s.active); $('stop').disabled=s.hold&&!s.active;
  $('send').textContent=officeI18n.t('Send');
  renderWork(s.work);
  $('send').disabled=roomInflight.has(currentRoomId)||Boolean(s.work?.tasks?.some(task=>!['completed','failed','review_required'].includes(task.state)));
  const intentBox=$('intent-status');intentBox.replaceChildren();
  const waiting=s.intents?.find(i=>['clarification','expired','failed'].includes(i.state));
  if(waiting){
    const p=document.createElement('p');p.textContent=waiting.state==='clarification'?'Manager is waiting for your clarification.':waiting.error||'This request needs review.';
    const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Dismiss request';
    cancel.onclick=async()=>{try{await mutate('/api/intent/cancel',{intent_id:waiting.id});}catch(e){actionError=e.message;await refresh();}};
    intentBox.append(p,cancel);
  }
  conversation.scrollTop=follow?conversation.scrollHeight:scrollTop;
  if(officeView==='room')officeSpeech.observe('room:'+currentRoomId,s.journal);
}
function renderWork(work){
  const box=$('work-status');box.replaceChildren();
  const unfinished=work?.tasks?.find(t=>!['completed','failed','review_required'].includes(t.state));
  $('work-availability').textContent=work?.enabled?'Task execution available.':work?.reason||'Task execution unavailable.';
  if(unfinished)$('send').disabled=true;
  else if(!pendingRequest)$('send').disabled=false;
  const recent=work?.tasks?.at(-1);
  if(!recent)return;
  box.className='work-card';
  const title=document.createElement('strong');title.textContent=`Task: ${recent.specification.title} · ${recent.state}${recent.hold?' · held':''}`;box.append(title);
  const scope=document.createElement('p');scope.textContent=recent.specification.path;box.append(scope);
  if(recent.issue_url){const link=document.createElement('a');link.textContent=officeI18n.t('Open GitHub issue');link.href=recent.issue_url;link.target='_blank';link.rel='noopener';box.append(link);}
  const active=recent.steps.find(step=>step.state!=='completed');
  if(active){const p=document.createElement('p');p.textContent=`${names[active.role]}: ${active.state}`;box.append(p);}
  if(recent.state==='completed'){const p=document.createElement('p');p.textContent='File published and independently verified.';box.append(p);}
  const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent=officeI18n.t('Evidence');details.append(summary);
  for(const step of recent.steps){const p=document.createElement('p');p.textContent=`${names[step.role]}: ${step.state}${step.turn_id?' · '+step.turn_id:''}`;details.append(p);}
  if(recent.artifact_sha){const p=document.createElement('p');p.textContent='SHA-256: '+recent.artifact_sha;details.append(p);}
  const limit=document.createElement('p');limit.textContent='Existing Desktop workers · 2 turns / 15 minutes';details.append(limit);box.append(details);
  if(recent.error){const p=document.createElement('p');p.className='task-error';p.textContent=recent.error;box.append(p);}
  if(unfinished){
    const note=document.createElement('p');note.textContent='Stop scheduling holds future dispatch and artifact publication. An accepted Desktop turn continues.';box.append(note);
    for(const [action,label] of [['stop','Stop scheduling'],['resume','Resume work'],['reconcile','Reconcile original outcome']]){
      const button=document.createElement('button');button.type='button';button.className='secondary';button.id='work-'+action;button.textContent=label;
      button.disabled=action==='stop'?Boolean(recent.hold):!recent.hold;
      button.onclick=async()=>{button.disabled=true;try{await mutate('/api/work/'+action,{});}catch(e){actionError=e.message;$('error').textContent=actionError;await refresh();}};
      box.append(button);
    }
  }
}
async function refresh() {
  if (refreshing) return; refreshing=true;
  const id=currentRoomId;
  try {const r=await fetch(roomPath('/api/state',id),{headers:{'X-Room-Token':token}}); if(!r.ok)throw Error(officeI18n.t('State unavailable'));const data=await r.json();roomStates.set(id,data);if(id!==currentRoomId)return;render(data);if(Date.now()-(roomQueueLoadedAt.get(id)||0)>3000)loadQueue(id);if(roomInflight.has(id))$('send').disabled=true;$('connection').textContent=officeI18n.t('Connected');}
  catch(e){if(id===currentRoomId){$('connection').textContent='Connection lost';$('error').textContent=officeI18n.t(e.message);}}
  finally{refreshing=false;}
}
async function mutate(path,data,id=currentRoomId) {
  const r=await fetch(path.startsWith('/api/sessions/')?path:roomPath(path,id),{method:'POST',headers:{'Content-Type':'application/json','X-Room-Token':token},body:JSON.stringify(data)});
  const result=await r.json(); if(!r.ok)throw Error(result.error); actionError=''; await refresh(); return result;
}
async function submit() {
  if($('send').disabled)return;
  officeMedia.stop();
  const roomId=currentRoomId,scope=roomScope(roomId);
  let images;try{images=officeMedia.snapshot(scope);}catch(error){$('error').textContent=officeI18n.t(error.message);return;}
  const draft=$('topic').value,text=draft.trim(); if(!text&&!images.length){$('topic').reportValidity();return;}
  const key=JSON.stringify([text,images.map(item=>item.id)]);
  if(!pendingRequest||pendingRequest.key!==key)pendingRequest={key,id:crypto.randomUUID()};roomPending.set(roomId,pendingRequest);
  const request=pendingRequest;
  roomDrafts.set(roomId,draft);roomInflight.add(roomId);$('send').disabled=true;
  try{
    if(state?.hold){
      await mutate('/api/resume',resumeOptions(),roomId);
    }
    await mutate('/api/messages',{text,images:images.map(item=>item.id),client_message_id:request.id},roomId);
    officeMedia.clear(scope,images);roomPending.delete(roomId);
    if(currentRoomId===roomId&&pendingRequest===request)pendingRequest=null;
    if(roomDrafts.get(roomId)===draft)roomDrafts.set(roomId,'');
    if(currentRoomId===roomId&&$('topic').value===draft){$('topic').value='';closeMentions();selectRecipient();}
  }
  catch(e){if(currentRoomId===roomId){actionError=e.message;$('error').textContent=actionError;}}
  finally{roomInflight.delete(roomId);if(currentRoomId===roomId)$('send').disabled=false;}
}
$('composer').addEventListener('submit',e=>{e.preventDefault();submit();});
// Enter sends, Ctrl/Cmd+Enter waits, Shift+Enter is a newline; IME composition, Alt and held-key repeats never submit.
function composerIntent(e){
  if(e.key!=='Enter'||e.isComposing||e.keyCode===229||e.altKey||e.shiftKey)return null;
  if(e.repeat)return 'ignore';
  return e.ctrlKey||e.metaKey?'queue':'send';
}
const roomQueues=new Map(),roomQueueRequests=new Map(),roomQueueAsked=new Map(),roomQueueBusy=new Set(),roomQueueSeq=new Map(),roomQueueLoadedAt=new Map();
async function queueCall(id,suffix,data){
  const response=await fetch(roomPath('/api/queue'+suffix,id),{method:data?'POST':'GET',headers:{'X-Room-Token':token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});
  const result=await response.json().catch(()=>({}));if(!response.ok)throw Error(result.error||'Request failed');return result;
}
function roomBusy(id){const s=roomStates.get(id);return roomInflight.has(id)||Boolean(s?.active)||Boolean(s?.deliveries?.some(d=>['sending','accepted'].includes(d.state)))||Boolean(s?.work?.tasks?.some(task=>!['completed','failed','review_required'].includes(task.state)));}
async function loadQueue(id=currentRoomId){
  const seq=(roomQueueSeq.get(id)||0)+1;roomQueueSeq.set(id,seq);roomQueueLoadedAt.set(id,Date.now());
  let entry;
  try{const data=await queueCall(id,'');entry={items:data.items.filter(item=>item.state==='waiting'),error:''};}
  catch{entry={items:roomQueues.get(id)?.items||[],error:'Waiting messages could not be refreshed'};}
  if(roomQueueSeq.get(id)!==seq)return;
  roomQueues.set(id,entry);if(id===currentRoomId)renderQueue();
}
function renderQueue(){
  const box=$('room-queue'),entry=roomQueues.get(currentRoomId),items=entry?.items||[],id=currentRoomId;
  box.replaceChildren();
  if(!items.length&&!entry?.error){box.hidden=true;return;}
  box.hidden=false;
  const head=document.createElement('h2');head.textContent=officeI18n.t('Waiting messages')+' · '+items.length;
  const note=document.createElement('p');note.className='room-queue-note';note.textContent=officeI18n.t('Waiting messages are not sent until you choose Send now.');
  box.append(head,note);
  if(entry.error){const warning=document.createElement('p');warning.className='room-queue-error';warning.setAttribute('role','alert');warning.textContent=officeI18n.t(entry.error);box.append(warning);}
  const list=document.createElement('ul');
  for(const item of items){
    const row=document.createElement('li');row.dataset.queueId=item.id;
    const copy=document.createElement('div');copy.className='room-queue-copy';
    const text=document.createElement('span');text.className='room-queue-text';text.textContent=item.text;text.title=item.text;
    const when=document.createElement('time');when.className='pc-muted';when.dateTime=new Date(item.created*1000).toISOString();when.textContent=officeI18n.t('Waiting since {time}',{time:new Date(item.created*1000).toLocaleTimeString(officeI18n.language,{hour:'2-digit',minute:'2-digit'})});
    copy.append(text,when);
    const actions=document.createElement('div');actions.className='room-queue-actions';
    const busy=roomQueueBusy.has(item.id);
    if(roomQueueAsked.get(id)===item.id){
      const ask=document.createElement('span');ask.className='room-queue-ask';ask.textContent=officeI18n.t('Send this waiting message to the room now?');
      const confirm=document.createElement('button');confirm.type='button';confirm.className='room-queue-confirm';confirm.textContent=officeI18n.t('Send now');confirm.disabled=busy||roomBusy(id);confirm.onclick=()=>releaseQueued(id,item);
      const keep=document.createElement('button');keep.type='button';keep.className='secondary room-queue-keep';keep.textContent=officeI18n.t('Keep waiting');keep.disabled=busy;keep.onclick=()=>{roomQueueAsked.delete(id);renderQueue();};
      actions.append(ask,confirm,keep);
    }else{
      const send=document.createElement('button');send.type='button';send.className='secondary room-queue-send';send.textContent=officeI18n.t('Send now');send.disabled=busy||roomBusy(id);send.title=send.disabled?officeI18n.t('The room is busy; this message keeps waiting.'):'';send.onclick=()=>{roomQueueAsked.set(id,item.id);renderQueue();};
      const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary room-queue-cancel';cancel.textContent=officeI18n.t('Cancel');cancel.disabled=busy;cancel.onclick=()=>cancelQueued(id,item);
      actions.append(send,cancel);
    }
    row.append(copy,actions);list.append(row);
  }
  box.append(list);
}
async function enqueue(){
  const roomId=currentRoomId,scope=roomScope(roomId),lock='enqueue:'+roomId;
  if(roomQueueBusy.has(lock))return;
  officeMedia.stop();
  let images;try{images=officeMedia.snapshot(scope);}catch(error){$('error').textContent=officeI18n.t(error.message);return;}
  if(images.length){$('error').textContent=officeI18n.t('Images cannot wait. Remove them or press Enter to send.');return;}
  const draft=$('topic').value,text=draft.trim();if(!text){$('topic').reportValidity();return;}
  let request=roomQueueRequests.get(roomId);if(!request||request.key!==text){request={key:text,id:crypto.randomUUID()};roomQueueRequests.set(roomId,request);}
  roomQueueBusy.add(lock);
  try{
    await queueCall(roomId,'',{text,recipient:null,client_message_id:request.id});
    roomQueueRequests.delete(roomId);
    if(roomDrafts.get(roomId)===draft)roomDrafts.set(roomId,'');
    if(currentRoomId===roomId){if($('topic').value===draft){$('topic').value='';closeMentions();selectRecipient();}$('error').textContent='';$('queue-status').textContent=officeI18n.t('Message is waiting. Nothing was sent.');}
  }catch(e){if(currentRoomId===roomId)$('error').textContent=officeI18n.t(e.message);}
  finally{roomQueueBusy.delete(lock);await loadQueue(roomId);}
}
async function releaseQueued(id,item){
  if(roomQueueBusy.has(item.id)||roomBusy(id))return;
  roomQueueBusy.add(item.id);roomInflight.add(id);if(currentRoomId===id)$('send').disabled=true;renderQueue();
  try{
    if(currentRoomId===id&&state?.hold)await mutate('/api/resume',resumeOptions(),id);
    await queueCall(id,'/release',{id:item.id,confirmed:true});
    roomQueueAsked.delete(id);if(currentRoomId===id)$('error').textContent='';
  }catch(e){if(currentRoomId===id){actionError=e.message;$('error').textContent=officeI18n.t(e.message);}}
  finally{roomQueueBusy.delete(item.id);roomInflight.delete(id);await loadQueue(id);if(currentRoomId===id)await refresh();}
}
async function cancelQueued(id,item){
  if(roomQueueBusy.has(item.id))return;
  roomQueueBusy.add(item.id);renderQueue();
  try{await queueCall(id,'/cancel',{id:item.id});if(currentRoomId===id)$('error').textContent='';}
  catch(e){if(currentRoomId===id)$('error').textContent=officeI18n.t(e.message);}
  finally{roomQueueBusy.delete(item.id);await loadQueue(id);}
}
window.addEventListener('office-language-changed',()=>renderQueue());
let handles=[{handle:'manager',detail:'Planning and coordination'},{handle:'developer',detail:'Implementation perspective'},{handle:'verifier',detail:'Independent perspective'},{handle:'everyone',detail:'All room participants'}];
let mentionMatch=null,mentionOptions=[],mentionIndex=0;
function closeMentions(){$('mentions').hidden=true;$('topic').setAttribute('aria-expanded','false');$('topic').removeAttribute('aria-activedescendant');mentionMatch=null;}
function showMentions(){
  const before=$('topic').value.slice(0,$('topic').selectionStart);const match=before.match(/(?:^|\s)@([a-z]*)$/i);
  if(!match){closeMentions();return;}
  const members=rooms.find(room=>room.id===currentRoomId)?.bots||[];
  mentionMatch={start:before.lastIndexOf('@'),end:$('topic').selectionStart};mentionOptions=handles.filter(h=>h.handle.startsWith(match[1].toLowerCase())&&(h.handle==='everyone'||members.includes(h.handle)));mentionIndex=0;
  if(!mentionOptions.length){closeMentions();return;}paintMentions();
}
function paintMentions(){
  $('mentions').replaceChildren();$('mentions').hidden=false;$('topic').setAttribute('aria-expanded','true');
  mentionOptions.forEach((h,i)=>{const option=document.createElement('li');option.id='mention-'+h.handle;option.role='option';option.setAttribute('aria-selected',String(i===mentionIndex));option.textContent='@'+h.handle;const detail=document.createElement('small');detail.textContent=h.detail;option.append(detail);option.onmousedown=e=>{e.preventDefault();chooseMention(i);};$('mentions').append(option);});
  $('topic').setAttribute('aria-activedescendant','mention-'+mentionOptions[mentionIndex].handle);
}
function chooseMention(index){
  const input=$('topic'),handle=mentionOptions[index].handle,insert='@'+handle+' ';
  input.setRangeText(insert,mentionMatch.start,mentionMatch.end,'end');closeMentions();input.focus();
  selectRecipient();
}
$('topic').addEventListener('input',()=>{showMentions();selectRecipient();});$('topic').addEventListener('click',showMentions);
$('topic').addEventListener('keydown',e=>{
  if(e.isComposing||e.keyCode===229)return;
  if(!$('mentions').hidden){
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();mentionIndex=(mentionIndex+(e.key==='ArrowDown'?1:-1)+mentionOptions.length)%mentionOptions.length;paintMentions();return;}
    if((e.key==='Enter'||e.key==='Tab')&&!e.shiftKey){e.preventDefault();chooseMention(mentionIndex);return;}
    if(e.key==='Escape'){e.preventDefault();closeMentions();return;}
  }
  const intent=composerIntent(e);if(intent){e.preventDefault();closeMentions();if(intent==='send')submit();else if(intent==='queue')enqueue();}
});
$('topic').addEventListener('blur',closeMentions);
function selectRecipient(){const selected=new Set([...$('topic').value.matchAll(/(?<![\w@])@([\w-]+)/g)].map(m=>m[1].toLowerCase()));document.querySelectorAll('[data-recipient]').forEach(button=>button.setAttribute('aria-pressed',String(selected.has('everyone')||selected.has(button.dataset.recipient))));}
document.querySelectorAll('[data-recipient]').forEach(button=>button.onclick=()=>{
  const input=$('topic'),handle='@'+button.dataset.recipient;
  input.value=/(?<![\w@])@[\w-]+/.test(input.value)?input.value.replace(/(?<![\w@])@[\w-]+/,handle):handle+' '+input.value;
  selectRecipient();closeMentions();input.focus();
});
function resumeOptions(){const unresolved=state?.deliveries.some(d=>!['completed','cancelled','failed','interrupted'].includes(d.state))||state?.intents?.some(i=>['pending','ready','clarification'].includes(i.state));return {fresh_conversation:!unresolved};}
for(const action of ['stop','resume'])$(action).onclick=async()=>{try{await mutate('/api/'+action,action==='resume'?resumeOptions():{});}catch(e){actionError=e.message;$('error').textContent=actionError;}};
async function connect(){
  await refresh();
}

let sessions=[], selectedSession=null, sessionsLoading=false;
let officeView='room', personalBots=[], selectedPrivateBot=null, privateLoading=false;
const privateStates=new Map(), privateDrafts=new Map(), privatePending=new Map(), privateActionErrors=new Map(), privateInflight=new Set();

function botName(bot){return names[bot]||bot;}
function botClass(bot){const role=(roleOf[bot]||bot).toLowerCase();return role.includes('verif')||role.includes('review')?'verifier':role.includes('manager')||role.includes('director')?'manager':'developer';}
function applyBots(list,lead){if(lead)leadBot=lead;for(const item of list){if(item.name)names[item.bot]=item.name;if(item.role)roleOf[item.bot]=item.role;}
  handles=[...list.map(item=>({handle:item.bot,detail:item.role||''})),{handle:'everyone',detail:'All room participants'}];
  const box=document.querySelector('#new-room-form fieldset');if(box){const legend=box.querySelector('legend');box.replaceChildren(legend);
    for(const item of list){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=true;
      if(item.bot===leadBot)input.disabled=true;else{input.name='room-bot';input.value=item.bot;}label.append(input,' '+botName(item.bot));box.append(label);}}}
async function api(path, options={}){
  const response=await fetch(path,{...options,headers:{'X-Room-Token':token,...(options.headers||{})}});
  const result=await response.json();if(!response.ok)throw Error(result.error||'Request failed');return result;
}
let directoryBots = [];
let directoryTeams = [];
function renderPrivateList(){
  const list=$('personal-list'); if(!list) return;
  list.replaceChildren();
  const query = ($('bot-search')?.value || '').trim().toLowerCase();
  const botsToDisplay = directoryBots.length ? directoryBots : personalBots.map(b=>({bot_key: b.bot, bot_id: b.bot, name: b.name, role: b.role, project_name: officeI18n.t('Current'), team_name: '', thread_id: b.personal_thread_id, is_current: true}));
  const filtered = botsToDisplay.filter(b => {
    if(!query) return true;
    const hay = [b.name, b.role, b.project_name, b.team_name, b.bot_id].filter(Boolean).join(' ').toLowerCase();
    return hay.includes(query);
  });
  for(const item of filtered){
    const button=document.createElement('button'); button.type='button'; button.className='personal-row';
    const isCurrentBot = item.is_current && selectedPrivateBot === item.bot_id;
    button.setAttribute('aria-pressed', String(isCurrentBot));
    const avatar=document.createElement('span'); avatar.className='avatar ' + botClass(item.bot_id); avatar.textContent=(item.name || item.bot_id)[0]; avatar.setAttribute('aria-hidden','true');
    const copy=document.createElement('span'); copy.className='session-copy';
    const title=document.createElement('strong'); title.textContent=item.name || item.role || item.bot_id;
    const meta=document.createElement('small');
    meta.textContent = (item.project_name ? item.project_name + ' · ' : '') + (item.role || '') + (item.team_name ? ' (' + item.team_name + ')' : '');
    copy.append(title, meta); button.append(avatar, copy);
    if(!item.is_current && !item.thread_id){
      button.disabled = true;
      button.style.opacity = '0.6';
      button.title = 'No active chat thread appointed';
    } else {
      button.onclick = async () => {
        if(item.is_current){
          openPrivate(item.bot_id);
        } else if(item.thread_id){
          try {
            await mutate('/api/registry/open', {bot_key: item.bot_key});
            $('personal-notice').textContent = 'Navigated to ' + item.name + ' in Codex.';
          } catch(err) {
            $('personal-notice').textContent = err.message;
          }
        }
      };
    }
    list.append(button);
  }
  if(!filtered.length){
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No matching bots.';
    list.append(empty);
  }
}
async function loadBots(){
  $('personal-notice').textContent='Loading appointed bots…';
  let dirError = null;
  let localData = {bots:[]};
  let dirData = {bots:[],teams:[]};
  try {
    const results = await Promise.allSettled([
      api('/api/bots'),
      api('/api/registry/overview')
    ]);
    if(results[0].status === 'fulfilled') localData = results[0].value;
    if(results[1].status === 'fulfilled') {
      dirData = results[1].value;
      if(dirData.status === 'invalid_config' || dirData.status === 'roster_error') {
        dirError = dirData.error || 'Registry error';
      } else if(dirData.teams && dirData.teams.some(t=>t.status==='source_unavailable'||t.status==='roster_error')) {
        const unavail = dirData.teams.filter(t=>t.status==='source_unavailable'||t.status==='roster_error');
        dirError = unavail.length + ' project source(s) unavailable';
      }
    } else {
      dirError = results[1].reason?.message || 'Directory unavailable';
    }
    personalBots = localData.bots || [];
    applyBots(personalBots, localData.lead);
    if(dirData && dirData.bots && dirData.bots.length){
      directoryBots = dirData.bots;
    } else {
      directoryBots = personalBots.map(b=>({bot_key: b.bot, bot_id: b.bot, name: b.name, role: b.role, project_name: officeI18n.t('Current'), team_name: localData.team, thread_id: b.personal_thread_id, is_current: true}));
    }
    renderPrivateList();
    if(results[0].status === 'rejected' && (!dirData.bots || !dirData.bots.length)){
      const localErr = results[0].reason?.message || 'Local bots unavailable';
      $('personal-notice').textContent = 'Error: ' + localErr + (dirError ? '; Directory: ' + dirError : '');
    } else if(dirError){
      $('personal-notice').textContent = 'Directory warning: ' + dirError + ' (showing available bots)';
    } else {
      $('personal-notice').textContent = directoryBots.length ? '' : 'No appointed bots found.';
    }
  } catch(error){
    $('personal-notice').textContent = error.message;
  }
}
async function loadTeams(){
  if(!document.getElementById('team-dir-list')) return;
  $('teams-notice').textContent='Loading teams…';
  try{
    const dirData = await api('/api/registry/overview');
    directoryTeams = dirData.teams || [];
    renderTeamsDirectory();
    if(dirData.status === 'invalid_config' || dirData.status === 'roster_error'){
      $('teams-notice').textContent = 'Registry warning: ' + (dirData.error || 'Invalid configuration');
    } else if(dirData.teams && dirData.teams.some(t=>t.status==='source_unavailable'||t.status==='roster_error')){
      const unavail = dirData.teams.filter(t=>t.status==='source_unavailable'||t.status==='roster_error');
      $('teams-notice').textContent = 'Notice: ' + unavail.length + ' project source(s) unavailable or in error.';
    } else {
      $('teams-notice').textContent = directoryTeams.length ? '' : 'No teams found.';
    }
  } catch(error){
    $('teams-notice').textContent = error.message;
  }
}
function renderTeamsDirectory(){
  window.refreshOfficeDirectoryVisibility = () => { renderPrivateList(); renderTeamsDirectory(); };
  const list = $('team-dir-list'); if(!list) return;
  list.replaceChildren();
  const byProject = new Map();
  for(const t of directoryTeams){
    const key = String(t.project_id || t.project_name);
    if(!byProject.has(key)) byProject.set(key, {name: t.project_name, teams: []});
    byProject.get(key).teams.push(t);
  }
  for(const p of byProject.values()){
    const pheader = document.createElement('div');
    pheader.className = 'section-label';
    pheader.style.marginTop = '12px';
    pheader.textContent = p.name.toUpperCase();
    list.append(pheader);
    for(const item of p.teams){
      const row = document.createElement('div');
      row.className = 'personal-row';
      row.style.flexDirection = 'column';
      row.style.alignItems = 'flex-start';
      row.style.cursor = 'default';
      const title = document.createElement('strong');
      title.textContent = item.team || (item.status === 'source_unavailable' ? officeI18n.t('Source unavailable') : officeI18n.t('No teams'));
      const meta = document.createElement('small');
      if(item.status === 'source_unavailable'){
        meta.textContent = 'Project source unavailable';
      } else if(item.status === 'roster_error'){
        meta.textContent = officeI18n.t('Roster error');
      } else if(!item.members.length){
        meta.textContent = 'No active members';
      } else {
        meta.textContent = item.members.map(m=>m.display_name ? m.display_name + ' (' + m.role + ')' : m.role).join(', ');
      }
      row.append(title, meta);
      list.append(row);
    }
  }
}
function renderPrivate(state, bot){
  if(!state||bot!==selectedPrivateBot)return;
  const conversation=$('personal-conversation'),scrollTop=conversation.scrollTop,follow=conversation.scrollHeight-conversation.clientHeight-scrollTop<40;
  $('personal-status').textContent=officeI18n.state(state.status);$('personal-error').textContent=privateActionErrors.get(bot)||visibleRoomError(state.error);
  const meta=state.personal_thread_id||personalBots.find(item=>item.bot===bot)?.personal_thread_id;
  if(meta){const item=personalBots.find(entry=>entry.bot===bot);if(item&&item.personal_thread_id!==meta){item.personal_thread_id=meta;renderPrivateList();}}
  $('personal-thread-meta').textContent=meta?`${state.backend==='desktop'?officeI18n.t("Shared with Codex Desktop"):officeI18n.t("Office-only conversation")} · ${meta}`:officeI18n.t("Office-only conversation");
  $('personal-open-codex').hidden=state.backend!=='desktop';
  $('personal-help').textContent=state.backend==='desktop'?'This is the same personal conversation as in Codex Desktop. Messages wait while the bot is busy. Pause sending holds only OfficeвЂ™s queue; it does not interrupt Codex. Recent conversation text is shown; tools and reasoning are omitted.':officeI18n.t("This Office-only private conversation stays separate from the Development room and native Codex chats.");
  $('personal-stop').textContent=state.backend==='desktop'?officeI18n.t("Pause sending"):officeI18n.t('Stop');
  const journal=$('personal-journal');journal.replaceChildren();
  if(!state.journal?.length){const empty=document.createElement('p');empty.className='empty';empty.textContent=officeI18n.t('Start a private conversation with {name}.',{name:botName(bot)});journal.append(empty);}
  for(const message of state.journal||[]){
    const article=document.createElement('article');article.className=`message ${message.sender==='owner'?'owner':'bot'}`;article.dataset.messageId=message.id;
    const head=document.createElement('div');head.className='message-head';
    if(message.sender!=='owner'){const avatar=document.createElement('span');avatar.className=`avatar ${botClass(bot)}`;avatar.textContent=botName(bot)[0];avatar.setAttribute('aria-hidden','true');head.append(avatar);}
    const author=document.createElement('span');author.className='author';author.textContent=message.sender==='owner'?officeI18n.t('You'):botName(bot);
    const timestamp=document.createElement('time');timestamp.className='timestamp';const date=new Date((message.created||Date.now()/1000)*1000);if(!Number.isNaN(date.valueOf())){timestamp.dateTime=date.toISOString();timestamp.textContent=date.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});}head.append(author,timestamp);
    if(message.sender!=='owner'&&message.text)head.append(officeSpeech.button('personal:'+bot+':'+message.id,message.text));
    const text=officeMedia.message(message);article.append(head,text);journal.append(article);
  }
  const deliveries=$('personal-deliveries');deliveries.replaceChildren();
  for(const delivery of (state.deliveries||[]).filter(item=>!['completed','cancelled'].includes(item.state))){const line=document.createElement('p');line.className='delivery';line.textContent=`${delivery.state}${delivery.error?' вЂ” '+delivery.error:''}`;deliveries.append(line);}
  $('personal-send').textContent=officeI18n.t('Send');
  const busy=privateInflight.has(bot);
  $('personal-resume').disabled=busy||!state.hold||Boolean(state.active);$('personal-stop').disabled=busy||Boolean(state.hold&&!state.active);
  $('personal-send').disabled=busy||Boolean((state.work?.tasks||[]).some(task=>!['completed','failed','review_required'].includes(task.state)));
  conversation.scrollTop=follow?conversation.scrollHeight:scrollTop;
  if(officeView==='personal'&&bot===selectedPrivateBot)officeSpeech.observe('personal:'+bot,state.journal||[],!state.active);
}
async function refreshPrivate(bot=selectedPrivateBot){
  if(!bot||bot!==selectedPrivateBot||privateLoading)return;privateLoading=true;
  try{const data=await api('/api/private/'+encodeURIComponent(bot));if(bot!==selectedPrivateBot)return;privateStates.set(bot,data);renderPrivate(data,bot);$('personal-connection').textContent=officeI18n.t('Connected');}
  catch(error){if(bot===selectedPrivateBot){$('personal-connection').textContent='Connection lost';$('personal-error').textContent=officeI18n.t(error.message);}}
  finally{privateLoading=false;}
}
async function openPrivate(bot){
  officeMedia.stop();officeSpeech.enter('personal:'+bot);addConversation('personal',bot,'Personal · '+botName(bot));
  if(selectedPrivateBot&&selectedPrivateBot!==bot)privateDrafts.set(selectedPrivateBot,$('personal-topic').value);
  if(window.officeShell&&!window.officeShell.canNavigate())return;window.officeShell?.activate('personal',botName(bot),()=>openPrivate(bot));
  selectedPrivateBot=bot;officeView='personal';
  selectMenu('bots');document.getElementById('agents-view').hidden=true;officeProjects.hide();$('room-view').hidden=true;$('session-detail').hidden=true;$('personal-view').hidden=false;
  $('personal-topic').value=privateDrafts.get(bot)||'';
  officeMedia.refresh();
  $('personal-title').textContent=officeI18n.t('Personal · {name}',{name:botName(bot)});$('personal-avatar').className=`avatar ${botClass(bot)}`;$('personal-avatar').textContent=botName(bot)[0];$('personal-topic').placeholder=officeI18n.t('Message {name} privately…',{name:botName(bot)});
  $('personal-status').textContent=officeI18n.t('Loading');$('personal-connection').textContent=officeI18n.t('Connecting');$('personal-send').disabled=true;$('personal-stop').disabled=true;$('personal-resume').disabled=true;renderPrivateList();
  if(!privateStates.has(bot)){$('personal-journal').replaceChildren(Object.assign(document.createElement('p'),{className:'empty',textContent:officeI18n.t('Opening private Codex chat…')}));}
  const cached=privateStates.get(bot);if(cached)renderPrivate(cached,bot);
  officeSpeech.enter('personal:'+bot);
  await refreshPrivate(bot);
}
function privateResumeOptions(state){const unresolved=state?.deliveries?.some(item=>!['completed','cancelled','failed','interrupted'].includes(item.state))||state?.intents?.some(item=>['pending','ready','clarification'].includes(item.state));return {fresh_conversation:!unresolved};}
async function submitPrivate(){
  const bot=selectedPrivateBot;if(!bot||privateInflight.has(bot)||$('personal-send').disabled)return;
  officeMedia.stop();
  let images;try{images=officeMedia.snapshot(bot);}catch(error){$('personal-error').textContent=officeI18n.t(error.message);return;}
  const draft=$('personal-topic').value,text=draft.trim();if(!text&&!images.length){$('personal-topic').reportValidity();return;}
  privateDrafts.set(bot,draft);
  const key=JSON.stringify([text,images.map(item=>item.id)]);
  let pending=privatePending.get(bot);if(!pending||pending.key!==key){pending={text,key,id:crypto.randomUUID()};privatePending.set(bot,pending);}
  privateInflight.add(bot);$('personal-send').disabled=true;$('personal-error').textContent='';
  try{
    const current=privateStates.get(bot)||{};
    if(current.hold){const resumed=await api('/api/private/'+encodeURIComponent(bot)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(privateResumeOptions(current))});privateStates.set(bot,{...current,...resumed,hold:false});}
    await api('/api/private/'+encodeURIComponent(bot)+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,images:images.map(item=>item.id),client_message_id:pending.id})});
    officeMedia.clear(bot,images);
    privateActionErrors.delete(bot);
    if(privatePending.get(bot)===pending)privatePending.delete(bot);
    if(privateDrafts.get(bot)===draft){privateDrafts.set(bot,'');if(selectedPrivateBot===bot&&$('personal-topic').value===draft)$('personal-topic').value='';}
    await refreshPrivate(bot);
  }catch(error){privateActionErrors.set(bot,error.message);if(selectedPrivateBot===bot)$('personal-error').textContent=officeI18n.t(error.message);}
  finally{privateInflight.delete(bot);if(selectedPrivateBot===bot){$('personal-send').disabled=false;const state=privateStates.get(bot);if(state)renderPrivate(state,bot);}}
}
$('personal-composer').addEventListener('submit',event=>{event.preventDefault();submitPrivate();});
$('personal-open-codex').onclick=async()=>{try{await api('/api/private/'+selectedPrivateBot+'/open',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){$('personal-error').textContent=officeI18n.t(error.message);}};
$('personal-topic').addEventListener('input',()=>{if(selectedPrivateBot)privateDrafts.set(selectedPrivateBot,$('personal-topic').value);});
$('personal-topic').addEventListener('keydown',event=>{const intent=composerIntent(event);if(!intent)return;event.preventDefault();if(intent==='send')submitPrivate();else if(intent==='queue')$('personal-error').textContent=officeI18n.t('Waiting is available in rooms only. Press Enter to send.');});
for(const action of ['stop','resume'])$('personal-'+action).onclick=async()=>{
  const bot=selectedPrivateBot;if(!bot||privateInflight.has(bot))return;$('personal-error').textContent='';privateInflight.add(bot);renderPrivate(privateStates.get(bot),bot);
  try{const state=privateStates.get(bot)||{};const result=await api('/api/private/'+encodeURIComponent(bot)+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(action==='resume'?privateResumeOptions(state):{})});if(action==='resume')privateStates.set(bot,{...state,...result,hold:false});privateActionErrors.delete(bot);await refreshPrivate(bot);}
  catch(error){privateActionErrors.set(bot,error.message);if(selectedPrivateBot===bot)$('personal-error').textContent=officeI18n.t(error.message);}
  finally{privateInflight.delete(bot);if(selectedPrivateBot===bot){const state=privateStates.get(bot);if(state)renderPrivate(state,bot);}}
};
function officeTab(tab){
  selectMenu(tab);if(tab==='sessions')loadSessions();if(tab==='rooms')loadRooms();if(tab==='bots'){loadBots();officeAgents.open();}if(tab==='teams')loadTeams();if(tab==='projects')officeProjects.open('overview');if(tab==='updates')officeUpdates.open();
}
function selectMenu(tab){const updates=document.getElementById('updates-view');if(updates&&tab!=='updates')updates.hidden=true;const agents=document.getElementById('agents-view');if(agents&&tab!=='bots')agents.hidden=true;for(const name of ['sessions','bots','teams','rooms','projects','updates']){const selected=name===tab;const btn=$(name+'-tab');if(btn){btn.setAttribute('aria-selected',String(selected));btn.tabIndex=selected?0:-1;}const pane=$(name+'-pane');if(pane)pane.hidden=!selected;}}
function renderSessions(){
  const query=$('session-search').value.toLocaleLowerCase();$('session-list').replaceChildren();
  for(const s of sessions.filter(s=>(s.title+' '+s.project+' '+(s.kind==='office-private'?`Personal ${botName(s.bot)}`:'')).toLocaleLowerCase().includes(query))){
    const button=document.createElement('button');button.type='button';button.className='session-row';button.dataset.sessionId=s.id;button.dataset.status=s.status;button.setAttribute('aria-pressed',String(selectedSession?.id===s.id));button.title=s.title;
    const dot=document.createElement('span');dot.className='active-dot';dot.setAttribute('aria-hidden','true');
    const copy=document.createElement('span');copy.className='session-copy';const title=document.createElement('strong');title.textContent=s.kind==='office-private'?`Personal · ${botName(s.bot)}`:s.title;const meta=document.createElement('small');meta.textContent=s.kind==='office-private'?'Office private chat · '+(s.status||'ready'):(s.pinned?'Pinned · ':'')+s.project+' · '+s.status+(s.unread?' · unread':'');copy.append(title,meta);button.append(dot,copy);
    if(typeof s.updated_at==='number'){const time=document.createElement('time');const date=new Date(s.updated_at<1e12?s.updated_at*1000:s.updated_at);if(!Number.isNaN(date.valueOf())){time.dateTime=date.toISOString();time.textContent=date.toLocaleDateString(undefined,{month:'short',day:'numeric'});button.append(time);}}
    button.onclick=()=>{if(s.kind==='office-private'){openPrivate(s.bot);return;}openSession(s);};$('session-list').append(button);
  }
  if(!$('session-list').children.length){const empty=document.createElement('p');empty.textContent=sessions.length?'No matching sessions.':'No Codex sessions found.';empty.className='empty';$('session-list').append(empty);}
}
async function loadSessions(){
  if(sessionsLoading)return;sessionsLoading=true;$('sessions-refresh').disabled=true;$('sessions-notice').textContent='Loading Codex sessions…';
  try{const response=await fetch('/api/sessions',{headers:{'X-Room-Token':token}});const data=await response.json();if(!response.ok)throw Error(data.error);sessions=data.sessions;renderSessions();$('sessions-notice').textContent=data.unavailable_hosts?'Some hosts are unavailable.':sessions.length+' sessions';}
  catch(e){$('sessions-notice').textContent=officeI18n.t(e.message);}
  finally{sessionsLoading=false;$('sessions-refresh').disabled=false;}
}
for(const tab of ['projects','bots','teams','rooms','updates'])$(tab+'-tab').onclick=()=>officeTab(tab);$('sessions-refresh').onclick=loadSessions;
if($('bot-search')) $('bot-search').oninput = renderPrivateList;
if($('bots-refresh')) $('bots-refresh').onclick = loadBots;
$('session-search').oninput=renderSessions;
$('session-open').onclick=async()=>{if(!selectedSession)return;$('session-open').disabled=true;$('session-open-status').textContent='Opening…';try{await mutate('/api/sessions/open',{thread_id:selectedSession.id});$('session-open-status').textContent='Opened in Codex.';}catch(e){$('session-open-status').textContent=officeI18n.t(e.message);}finally{$('session-open').disabled=false;}};
setInterval(()=>{if(!$('sessions-pane').hidden)loadSessions();},30000);
window.addEventListener('DOMContentLoaded',()=>{
  officeSpeech.initialize();connect();loadRooms();loadBots();loadTeams();
  // The shell selects the initial page after modules initialize.
  setInterval(()=>{if(officeView==='room')refresh();},1000);
  setInterval(()=>{if(officeView==='personal'&&selectedPrivateBot)refreshPrivate(selectedPrivateBot);},1000);
});

const conversations=new Map();let activeConversation='room:development';
function addConversation(kind,id,title,data){const key=kind+':'+id;conversations.set(key,{kind,id,title,data});activeConversation=key;renderConversations();}
function activateConversation(item){if(item.kind==='room')openRoom(item.id);else if(item.kind==='personal')openPrivate(item.id);else if(item.kind==='project')officeProjects.open(item.id);else openSession(item.data);}
function renderConversations(){
  const nav=$('conversation-tabs');nav.replaceChildren();
  for(const [key,item] of conversations){
    const wrap=document.createElement('div');wrap.className='conversation-tab';const button=document.createElement('button');button.type='button';button.role='tab';button.setAttribute('aria-selected',String(key===activeConversation));button.tabIndex=key===activeConversation?0:-1;button.textContent=item.title;button.onclick=()=>activateConversation(item);
    const close=document.createElement('button');close.type='button';close.className='close-tab';const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');icon.setAttribute('viewBox','0 0 16 16');icon.setAttribute('aria-hidden','true');const strokes=document.createElementNS('http://www.w3.org/2000/svg','path');strokes.setAttribute('d','M4 4l8 8M12 4l-8 8');icon.append(strokes);close.append(icon);close.setAttribute('aria-label','Close '+item.title+' tab');close.onclick=()=>{conversations.delete(key);if(activeConversation===key){const next=[...conversations.values()].at(-1);if(next)activateConversation(next);else openRoom('development');}else renderConversations();};
    button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const items=[...conversations.values()],index=items.indexOf(item),next=items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowRight'?1:items.length-1))%items.length];activateConversation(next);nav.querySelector('[aria-selected=true]')?.focus();};
    wrap.append(button,close);nav.append(wrap);
  }
}
function openSession(session){if(window.officeShell&&!window.officeShell.canNavigate())return;window.officeShell?.activate('session',session.title,()=>openSession(session));officeMedia.stop();officeSpeech.stop();selectedSession=session;officeView='sessions';selectMenu('sessions');officeProjects.hide();$('room-view').hidden=true;$('personal-view').hidden=true;$('session-detail').hidden=false;$('session-title').textContent=session.title;$('session-meta').textContent=session.project+' · '+session.status;$('session-open').hidden=false;$('session-open-status').textContent='';addConversation('session',session.id,session.title,session);renderSessions();}
async function loadRooms(){try{const data=await api('/api/rooms');rooms=data.rooms;renderRooms();for(const room of rooms){const tab=conversations.get('room:'+room.id);if(tab)tab.title=room.name;}renderConversations();const current=rooms.find(room=>room.id===currentRoomId);if(current){$('room-title').textContent=current.name;$('room-avatar').textContent=current.name[0];}}catch(error){$('rooms-notice').textContent=officeI18n.t(error.message);}}
function renderRooms(){
  const list=$('room-list');list.replaceChildren();
  for(const room of rooms){const button=document.createElement('button');button.type='button';button.className='room-link';button.setAttribute('aria-pressed',String(officeView==='room'&&room.id===currentRoomId));const avatar=document.createElement('span');avatar.className='avatar room-avatar';avatar.textContent=room.name[0];const copy=document.createElement('span'),name=document.createElement('strong'),members=document.createElement('small');name.textContent=room.name;members.textContent=room.participants?room.participants.map(p=>p.name).join(' + '):room.bots.map(botName).join(' + ');copy.append(name,members);button.append(avatar,copy);button.onclick=()=>openRoom(room.id);list.append(button);}
}
async function openRoom(id){
  const room=rooms.find(item=>item.id===id);if(!room)return;if(window.officeShell&&!window.officeShell.canNavigate())return;window.officeShell?.activate('room',room.name,()=>openRoom(id));
  officeMedia.stop();officeSpeech.stop();roomDrafts.set(currentRoomId,$('topic').value);currentRoomId=id;officeView='room';cursor=0;pendingRequest=roomPending.get(id)||null;actionError='';state=null;
  selectMenu('rooms');officeProjects.hide();$('room-view').hidden=false;$('personal-view').hidden=true;$('session-detail').hidden=true;$('topic').value=roomDrafts.get(id)||'';officeMedia.refresh();closeMentions();
  $('room-title').textContent=room.name;$('room-avatar').textContent=room.name[0];$('room-description').textContent=room.participants?room.participants.map(p=>p.name).join(' + '):room.bots.map(botName).join(' + ');$('room-view').setAttribute('aria-label',room.name+' conversation');$('journal').replaceChildren();$('work-status').replaceChildren();$('intent-status').replaceChildren();$('deliveries').replaceChildren();$('provisional').replaceChildren();$('error').textContent='';$('status').textContent=officeI18n.t('Loading');$('connection').textContent=officeI18n.t('Connecting');$('send').disabled=true;$('stop').disabled=true;$('resume').disabled=true;
  addConversation('room',id,room.name);renderRooms();window.officeCrossRooms?.afterRender(id);officeSpeech.enter('room:'+id);$('queue-status').textContent='';const cached=roomStates.get(id);if(cached)render(cached);renderQueue();loadQueue(id);officeSpeech.enter('room:'+id);await refresh();
}
$('topic').addEventListener('input',()=>{roomDrafts.set(currentRoomId,$('topic').value);$('queue-status').textContent='';});
let pendingRoomCreation=null;
$('new-room-form').addEventListener('submit',async event=>{
  event.preventDefault();const name=$('room-name').value.trim(),bots=[leadBot,...[...document.querySelectorAll('input[name=room-bot]:checked')].map(input=>input.value)],key=JSON.stringify([name,bots]);
  if(!pendingRoomCreation||pendingRoomCreation.key!==key)pendingRoomCreation={key,id:crypto.randomUUID()};$('create-room').disabled=true;$('rooms-notice').textContent='Creating room…';
  try{const room=await api('/api/rooms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,bots,client_message_id:pendingRoomCreation.id})});pendingRoomCreation=null;await loadRooms();$('room-name').value='';$('new-room-details').open=false;$('rooms-notice').textContent='';await openRoom(room.id);}
  catch(error){$('rooms-notice').textContent=officeI18n.t(error.message);}finally{$('create-room').disabled=false;}
});

window.addEventListener('office-language-changed',()=>{
  if(state)render(state);
  if(selectedPrivateBot&&privateStates.get(selectedPrivateBot))renderPrivate(privateStates.get(selectedPrivateBot),selectedPrivateBot);
  renderRooms();renderPrivateList();renderConversations();
});
