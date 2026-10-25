/* Cross-project rooms: owner discussion with the participants' Managers (#264).
   A message is delivered through each chosen Manager's own Desktop chat; replies come back from the server.
   Discussion never starts work; work only comes from approved results. Delivery labels never claim more than the server reported. */
(()=>{
  const t=(key,values)=>officeI18n.t(key,values),$=id=>document.getElementById(id),enc=encodeURIComponent;
  const STATE={queued:'Queued',sending:'Sending',running:'Running',completed:'Completed',failed:'Failed',blocked:'Blocked'};
  const HINT={queued:'Waiting for the Manager chat to be free.',sending:'Being handed to the Manager chat.',running:'The Manager is working on the answer.',completed:'The Manager finished this turn.',failed:'Delivery failed; it is not resent automatically.',blocked:'Nothing was sent, or the outcome is unclear.'};
  const ERRORS={invalid_message:'Write a message of 1 to 4000 characters.',invalid_recipients:'Choose at least one participant of this room.',invalid_client_id:'The message could not be identified. Try again.',
    idempotency_conflict:'This message ID was already used for a different message. Send it again.',journal_full:'The room journal is full.',unknown_room:'This room no longer exists.',
    discussion_unavailable:'The room discussion is temporarily unavailable. Nothing new was sent.'};
  const stores=new Map(),drafts=new Map();
  let roomId=null,timer=0,loading=0,signature='';
  function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
  const room=()=>(typeof rooms!=='undefined'?rooms:[]).find(item=>item.id===roomId);
  const people=()=>room()?.participants||[];
  // Only Manager chats can receive room messages today; everyone else would be reported as blocked.
  const defaultRecipients=()=>{const list=people(),managers=list.filter(p=>p.role==='Manager');return new Set((managers.length?managers:list).map(p=>p.bot_key));};
  const draft=id=>{if(!drafts.has(id))drafts.set(id,{text:'',recipients:null,request:null,busy:false,error:''});return drafts.get(id);};
  const when=v=>{const ms=Date.parse(v);return ms?new Date(ms).toLocaleString(officeI18n.language,{dateStyle:'medium',timeStyle:'short'}):'';};
  async function call(path,options){
    let response;
    try{response=await fetch(path,{...options,headers:{'X-Room-Token':document.querySelector('meta[name="room-token"]').content,...(options?.body?{'Content-Type':'application/json'}:{})}});}
    catch{const error=Error('unavailable');error.code='unavailable';throw error;}
    const data=await response.json().catch(()=>({}));
    if(!response.ok){const error=Error(data.error||'Request failed');error.code=data.code;error.status=response.status;throw error;}
    return data;
  }
  const message=e=>t(ERRORS[e.code]||(e.code==='unavailable'?'Office is unavailable. Check the journal before sending again.':'')||'')||e.message;

  function ensure(){
    let box=$('room-discussion');if(box)return box;
    const view=$('room-view'),conversation=view.querySelector('.conversation'),panel=view.querySelector('.composer-panel');
    box=el('div','rd');box.id='room-discussion';box.hidden=true;
    box.append(el('p','rd-note',t('Discussion only. Messages here never start work; ask for a result in the Results panel.')));
    const list=el('ul','rd-list');list.id='rd-list';box.append(list);conversation.append(box);
    const form=el('form','rd-form');form.id='rd-form';form.hidden=true;
    const who=el('fieldset','rd-who');who.id='rd-who';who.append(el('legend',null,t('Send to')));
    const label=el('label','sr-only',t('Message'));label.htmlFor='rd-text';
    const text=el('textarea');text.id='rd-text';text.rows=2;text.maxLength=4000;text.required=true;text.placeholder=t('Message the participants');
    const row=el('div','rd-actions');const send=el('button',null,t('Send'));send.type='submit';send.id='rd-send';
    const error=el('p','rd-error');error.id='rd-error';error.setAttribute('role','alert');
    row.append(send);form.append(who,label,text,row,error);panel.append(form);
    form.addEventListener('submit',event=>{event.preventDefault();if(roomId)submit();});
    text.addEventListener('input',()=>{if(!roomId)return;const d=draft(roomId);d.text=text.value;d.error='';paintError();});
    text.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();form.requestSubmit();}});
    return box;
  }
  function paintStatic(){
    $('room-discussion').querySelector('.rd-note').textContent=t('Discussion only. Messages here never start work; ask for a result in the Results panel.');
    $('rd-form').querySelector('label.sr-only').textContent=t('Message');$('rd-text').placeholder=t('Message the participants');$('rd-send').textContent=t('Send');
  }
  function paintWho(){
    const d=draft(roomId),box=$('rd-who');if(!d.recipients)d.recipients=defaultRecipients();
    box.replaceChildren(el('legend',null,t('Send to')));
    for(const p of people()){
      const label=el('label','rd-recipient'),input=el('input');input.type='checkbox';input.value=p.bot_key;input.checked=d.recipients.has(p.bot_key);input.disabled=d.busy;
      input.onchange=()=>{if(input.checked)d.recipients.add(p.bot_key);else d.recipients.delete(p.bot_key);d.request=null;d.error='';paintError();};
      label.append(input,el('span',null,p.name+' · '+p.project_name+(p.role==='Manager'?'':' · '+t('not a Manager chat'))));box.append(label);
    }
  }
  function paintError(){const d=draft(roomId);const line=$('rd-error');line.textContent=d.error||(stores.get(roomId)?.error?t(stores.get(roomId).error):'');}
  function paintForm(){
    const d=draft(roomId),text=$('rd-text');if(text.value!==d.text)text.value=d.text;
    text.disabled=d.busy;$('rd-send').disabled=d.busy;paintWho();paintError();
  }
  function deliveryRow(entry){
    const row=el('li','rd-delivery');row.dataset.state=entry.state;
    const chip=el('span','rd-state',t(STATE[entry.state]||entry.state));chip.title=t(HINT[entry.state]||'');
    row.append(el('span',null,entry.name),chip);
    if(entry.reason&&entry.state!=='queued')row.append(el('small','rd-reason',t(entry.reason)));
    return row;
  }
  function paintList(force){
    const rows=stores.get(roomId)?.messages||[];
    const next=JSON.stringify([roomId,rows,officeI18n.language,people().length]);
    if(!force&&next===signature)return;signature=next;
    const list=$('rd-list');list.replaceChildren();
    if(!rows.length){list.append(el('li','rd-empty',t('No messages yet. Write to the participants below.')));return;}
    for(const item of rows){
      const li=el('li','rd-item');li.dataset.kind=item.kind;li.dataset.message=item.id;
      const head=el('div','rd-head');
      head.append(el('strong',null,item.kind==='owner'?t('You'):(item.name||item.author)),item.kind==='reply'&&item.project_id?el('small',null,item.project_id):document.createTextNode(''));
      const time=el('time','rd-time',when(item.at));head.append(time);
      li.append(head,el('p','rd-text',item.text));
      if(item.kind==='owner'&&item.deliveries?.length){const ul=el('ul','rd-deliveries');for(const entry of item.deliveries)ul.append(deliveryRow(entry));li.append(ul);}
      list.append(li);
    }
    const box=list.closest('.conversation');if(box)box.scrollTop=box.scrollHeight;
  }
  async function load(){
    const id=roomId;if(!id)return;const mine=++loading;
    try{const data=await call('/api/rooms/'+enc(id)+'/discussion');if(id!==roomId||mine!==loading)return;stores.set(id,{messages:data.messages||[],error:''});}
    catch{if(id!==roomId||mine!==loading)return;stores.set(id,{messages:stores.get(id)?.messages||[],error:'The journal could not be refreshed'});}
    paintList();paintError();
  }
  async function submit(){
    const id=roomId,d=draft(id),text=d.text.trim();
    if(!text){$('rd-text').reportValidity();return;}
    const recipients=people().map(p=>p.bot_key).filter(key=>d.recipients.has(key));
    if(!recipients.length){d.error=t(ERRORS.invalid_recipients);paintError();return;}
    const key=JSON.stringify([text,recipients]);if(!d.request||d.request.key!==key)d.request={key,id:crypto.randomUUID()};
    d.busy=true;d.error='';paintForm();
    try{
      const data=await call('/api/rooms/'+enc(id)+'/discussion',{method:'POST',body:JSON.stringify({text,client_message_id:d.request.id,recipients})});
      stores.set(id,{messages:data.messages||[],error:''});d.text='';d.request=null;
    }catch(e){
      if(e.code==='idempotency_conflict')d.request=null;
      d.error=e.code==='unavailable'?t('Office is unavailable. Check the journal before sending again.'):message(e);
    }
    d.busy=false;if(id===roomId){paintForm();paintList(true);await load();}
  }
  function sync(id){
    const view=$('room-view');if(!view)return;ensure();
    const cross=(typeof rooms!=='undefined'?rooms:[]).find(item=>item.id===id)?.kind==='cross_project';
    view.dataset.crossDiscussion=cross?'true':'false';
    $('room-discussion').hidden=!cross;$('rd-form').hidden=!cross;
    if(!cross){roomId=null;signature='';return;}
    if(id!==roomId){roomId=id;signature='';paintForm();paintList(true);load();}
    if(!timer)timer=setInterval(()=>{if(roomId&&!document.hidden&&typeof officeView!=='undefined'&&officeView==='room')load();},4000);
  }
  const base=window.officeCrossRooms?.afterRender;
  if(base)window.officeCrossRooms.afterRender=id=>{base(id);sync(id);};
  window.addEventListener('office-language-changed',()=>{if(roomId){signature='';paintStatic();paintForm();paintList(true);}});
  window.officeDiscussion={sync};
})();
