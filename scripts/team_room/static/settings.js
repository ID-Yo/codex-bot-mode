/* Edit only the canonical profile files and existing room settings. */
(()=>{
  const dialog=$('settings-dialog'),kind={value:'office'},target=$('settings-target'),content=$('settings-content'),name=$('settings-name'),status=$('settings-status');
  const categoryTabs=[...document.querySelectorAll('[data-settings-kind]')];
  function paintCategories(){for(const tab of categoryTabs){const selected=tab.dataset.settingsKind===kind.value;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;tab.disabled=busy;if(selected)$('settings-panel').setAttribute('aria-labelledby',tab.id);}}
  const tabs=[...document.querySelectorAll('[data-settings-file]')];let file='SOUL.md';
  function paintTabs(){for(const tab of tabs){tab.setAttribute('aria-selected',String(tab.dataset.settingsFile===file));tab.tabIndex=tab.dataset.settingsFile===file?0:-1;tab.disabled=busy;}}
  let loaded=null,request=0,busy=false;
  function dirty(){return loaded&&(loaded.kind!=='office'&&content.value!==loaded.content||loaded.kind!=='bots'&&name.value!==loaded.name);}
  function discard(){return !dirty()||window.confirm('Discard unsaved settings changes?');}
  function path(){return '/api/settings/'+kind.value+'/'+encodeURIComponent(target.value);}
  function applyOffice(row){$('office-name').textContent=row.name;document.title=row.name;}
  api('/api/settings/office/general').then(applyOffice).catch(()=>{});
  async function load(){
    paintCategories();const id=++request;loaded=null;status.textContent='Loading…';$('settings-save').disabled=true;content.disabled=true;
    try{
      const row=await api(path()+(kind.value==='bots'?'?file='+encodeURIComponent(file):''));if(id!==request)return;
      loaded={...row,kind:kind.value,content:kind.value==='office'?'':kind.value==='bots'?row.content:row.instructions};content.value=loaded.content;name.value=row.name||'';content.disabled=false;
      target.parentElement.hidden=kind.value==='office';content.hidden=kind.value==='office';$('settings-content-label').hidden=kind.value==='office';$('settings-name-label').firstChild.textContent=kind.value==='office'?'Office name':'Room name';
      $('settings-file-tabs').hidden=kind.value!=='bots';$('settings-name-label').hidden=kind.value==='bots';$('settings-members').hidden=kind.value!=='rooms';content.readOnly=Boolean(row.readonly);$('settings-save').hidden=Boolean(row.readonly);
      $('settings-path').textContent=row.path||'Saved room settings';$('settings-members').textContent='Participants: '+(row.bots||[]).map(botName).join(', ');
      $('settings-help').textContent=row.readonly?'Actual project-scoped learned memory, freshly validated through the same helper used by Office. Stale lessons whose sources changed are excluded. This view is read-only; it does not show native Codex-generated memory.':'Shared profile for this bot’s Office conversations. Office reloads it on the next turn. Active Codex Desktop chats may need to reload their profile. Documentation stays in English.';
      if(kind.value==='rooms')$('settings-help').textContent='Room instructions apply to subsequent discussion turns. Existing history and participant sessions are preserved. Participant membership is selected when creating the room.';
      if(kind.value==='office'){$('settings-path').textContent='Office identity';$('settings-help').textContent='The Office name is saved locally on the Office host and shared by its browser tabs.';}
      status.textContent='';$('settings-save').disabled=false;
    }catch(error){if(id===request)status.textContent=error.message;}
  }
  function botList(){return personalBots.length?personalBots.map(item=>item.bot):['manager','developer','verifier'];}
  function targets(){target.replaceChildren(...(kind.value==='office'?[new Option('General','general')]:kind.value==='bots'?botList().map(bot=>new Option(botName(bot),bot)):rooms.map(room=>new Option(room.name,room.id))));target.value=kind.value==='office'?'general':kind.value==='bots'?(selectedPrivateBot||leadBot):currentRoomId;}
  let previous={kind:'bots',target:leadBot,file:'SOUL.md'};
  function remember(){previous={kind:kind.value,target:target.value,file};}
  target.onchange=()=>{
    if(busy||!discard()){target.value=previous.target;return;}
    remember();load();
  };
  for(const tab of categoryTabs){
    tab.onclick=()=>{if(busy||tab.dataset.settingsKind===kind.value||!discard())return;kind.value=tab.dataset.settingsKind;targets();remember();load();};
    tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();let index=categoryTabs.indexOf(tab);index=event.key==='Home'?0:event.key==='End'?categoryTabs.length-1:(index+(event.key==='ArrowRight'?1:categoryTabs.length-1))%categoryTabs.length;categoryTabs[index].click();if(categoryTabs[index].getAttribute('aria-selected')==='true')categoryTabs[index].focus();};
  }
  for(const tab of tabs){tab.onclick=()=>{if(busy||tab.dataset.settingsFile===file||!discard())return;file=tab.dataset.settingsFile;paintTabs();remember();load();};tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();let index=tabs.indexOf(tab);index=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length;tabs[index].click();tabs[index].focus();};}
  $('office-settings').onclick=async()=>{if(dialog.open)return;kind.value='office';await loadRooms();targets();remember();dialog.showModal();load();};
  function close(){if(!busy&&discard()){++request;dialog.close();}}
  $('settings-close').onclick=close;dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  $('settings-reload').onclick=()=>{if(!busy&&discard())load();};
  $('settings-form').onsubmit=async event=>{
    event.preventDefault();if(!loaded||loaded.readonly||busy)return;busy=true;paintTabs();paintCategories();$('settings-save').disabled=true;target.disabled=true;
    try{
      const data=kind.value==='office'?{name:name.value,version:loaded.version}:kind.value==='bots'?{file,content:content.value,version:loaded.version}:{name:name.value,instructions:content.value,version:loaded.version};
      const row=await api(path(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      loaded={...row,kind:kind.value,content:kind.value==='office'?'':kind.value==='bots'?row.content:row.instructions};status.textContent='Saved. Applies to subsequent Office turns.';
      if(kind.value==='office'){applyOffice(row);status.textContent='Office name saved.';}
      if(kind.value==='rooms'){
        await loadRooms();const room=rooms.find(room=>room.id===currentRoomId);if(room){$('room-title').textContent=room.name;$('room-avatar').textContent=room.name[0];}
      }
    }catch(error){status.textContent=error.message;}
    finally{busy=false;paintTabs();paintCategories();$('settings-save').disabled=false;target.disabled=false;}
  };
})();
