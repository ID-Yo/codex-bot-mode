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
  const when=t=>{const d=new Date(t);return Number.isNaN(d.valueOf())?'unknown time':d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
  const stale=p=>!p.source.as_of||Date.now()-new Date(p.source.as_of)>(p.stale_after_hours||48)*3600e3;
  const pending=p=>(p.decisions||[]).filter(d=>d.state==='pending');
  let registryData=null;
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
      list.push({id:p.id,name:p.name||p.id,root:'',status:'ok',reason:null,is_current:false,applicable:true});
    }
    return list;
  }
  const allProjects=()=>data?.snapshot?.projects||[];
  const projects=()=>allProjects().filter(p=>isProjectVisible(p.id));

  async function load(){
    if(loading)return;loading=true;
    try{
      const [rProjects,rOverview]=await Promise.allSettled([
        fetch('/api/projects',{headers:{'X-Room-Token':token}}),
        fetch('/api/registry/overview',{headers:{'X-Room-Token':token}})
      ]);
      if(rProjects.status==='fulfilled'){
        const j=await rProjects.value.json();
        if(!rProjects.value.ok)throw Error(j.error||'Projects unavailable');
        if(j.available){data=j;problem=null;}
        else if(data?.available)problem=j.error||'Project status is unavailable';
        else{data=j;problem=null;}
      }
      if(rOverview.status==='fulfilled'&&rOverview.value.ok){
        registryData=await rOverview.value.json();
      }
    }catch(e){
      if(data?.available)problem=e.message;
      else{data={available:false,error:e.message};problem=null;}
    }finally{loading=false;}
    renderList();
    if(!document.getElementById('projects-view').hidden){
      if(view!=='overview'&&view!=='inbox'&&document.getElementById('pm-chat-container')){
        updateProjectLeftCol(view);
      }else{
        render();
      }
    }
    if(view!=='overview'&&view!=='inbox'){
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
      let label='Active'+(act.active_bots?.length?' ('+act.active_bots.join(', ')+')':'');
      if(act.partial_failure) label+=' (partial)';
      const pill=el('span','pc-activity pc-act-active',label);
      let titleText='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
      if(act.partial_failure) titleText+=' · partial coverage: some bots unknown';
      pill.title=titleText;
      container.append(pill);
      if(act.partial_failure){
        container.append(el('span','pc-flag pc-warn','Partial coverage'));
      }
      if(p.status==='paused'){
        container.append(el('span','pc-flag pc-warn','Contradiction: active bot'));
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
    sub.append(el('span',null,STATUS[p.status]+(p.manager?' · '+p.manager:'')));
    const act=projectActivities.get(pid);
    if(act){
      if(act.status==='active'&&!act.stale){
        let label='Active'+(act.active_bots?.length?' ('+act.active_bots.join(', ')+')':'');
        if(act.partial_failure) label+=' (partial)';
        const pill=el('span','pc-activity pc-act-active',label);
        let titleText='Checked '+when(act.fetched_at)+' via '+(act.source||'relay');
        if(act.partial_failure) titleText+=' · partial coverage: some bots unknown';
        pill.title=titleText;
        sub.append(pill);
        if(act.partial_failure){
          sub.append(el('span','pc-flag pc-warn','Partial coverage'));
          sub.append(el('span','pc-provenance','checked '+when(act.fetched_at)+' via '+(act.source||'relay')+' (partial: some bots unknown)'));
        }
        if(p.status==='paused'){
          const contra=el('span','pc-contradiction-flag','Paused · Active bot contradiction: '+(act.active_bots?.join(', ')||'bot')+' active in Codex');
          sub.append(contra);
        }
      }else if(act.status==='inactive'&&!act.stale){
        const pill=el('span','pc-activity pc-act-inactive','Inactive (all bots idle)');
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

  function freshness(p){const box=el('div','pc-source');const s=p.source;box.append(link(s.url,s.label));box.append(el('span','pc-muted',' · '+(s.as_of?'as of '+when(s.as_of):'no timestamp')));if(!s.available)box.append(el('span','pc-flag pc-warn','Source unavailable'+(s.error?': '+s.error:'')));else if(s.error)box.append(el('span','pc-flag pc-warn',s.error));else if(stale(p))box.append(el('span','pc-flag pc-warn','Stale'));return box;}
  function badge(p){
    return el('span','pc-status pc-'+p.status,STATUS[p.status]||p.status);
  }
  function decisionItem(d,p){
    const li=el('li','pc-decision'+(d.urgency==='urgent'?' pc-urgent':''));
    li.append(el('strong',null,d.question));
    if(p)li.append(el('span','pc-tag',p.name));
    if(d.context)li.append(el('p',null,d.context));
    const meta=el('div','pc-muted',(d.urgency==='urgent'?'Urgent · ':'')+'Asked by '+d.requested_by+' · '+when(d.requested_at)+(d.due?' · due '+when(d.due):'')+' · ');
    meta.append(link(d.source_url,'source'));
    li.append(meta);

    if(d.suggestion){
      const sug=el('div','pc-suggestion');
      const sugHead=el('div','pc-suggestion-head');
      sugHead.append(el('strong',null,'Proposed recommendation'));
      if(d.suggestion.by)sugHead.append(el('span','pc-muted',' · by '+d.suggestion.by));
      if(d.suggestion.at)sugHead.append(el('span','pc-muted',' · '+when(d.suggestion.at)));
      if(d.suggestion.source_url)sugHead.append(el('span','pc-muted',' · '),link(d.suggestion.source_url,'source'));
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
          if(isRec)optLi.append(el('span','pc-tag','Recommended'));
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
      const pLabel=el('span','pc-label','Waiting for you \u00b7 '+ds.length);
      pBox.append(pLabel);
      const ul=el('ul','pc-pending-list');
      ds.slice(0, 2).forEach(d=>{
        const li=el('li','pc-pending-item',d.question);
        li.title=d.question;
        li.setAttribute('aria-label',d.question);
        ul.append(li);
      });
      if(ds.length > 2){
        ul.append(el('li','pc-pending-more','+ '+(ds.length-2)+' more'));
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
    const facts=el('div','pc-muted',(p.manager?p.manager+' \u00b7 ':'')+(p.open_tasks==null?'tasks unknown':p.open_tasks+' open tasks'));
    c.append(facts);
    const rs=[...(p.reports||[])].sort((a,b)=>new Date(b.at)-new Date(a.at));
    const r=rs[0];
    if(r){
      const latestText='Latest: '+r.text+(r.at?' \u00b7 '+when(r.at):'');
      const latestP=el('p','pc-latest',latestText);
      latestP.title=latestText;
      latestP.setAttribute('aria-label',latestText);
      c.append(latestP);
    }
    c.append(freshness(p));
    return c;
  }
  function warning(){if(!problem||!data?.available)return null;const w=el('p','pc-banner','Update failed: '+problem+'. Showing the last valid data from '+when(data.snapshot.generated_at)+'; it may be out of date.');w.setAttribute('role','alert');return w;}
  function header(title,sub){
    const h=el('header','pc-header');
    const t=el('div');
    const subSpan=el('span','room-description',sub);
    subSpan.id='pm-header-subtitle';
    t.append(el('h1',null,title),subSpan);
    h.append(t);
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
      if(e.key==='Enter'){
        if(e.shiftKey)return;
        if(e.isComposing||e.keyCode===229)return;
        e.preventDefault();
        submitForm();
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
    const sendBtn=el('button',null,'Send');
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
      sendBtn.textContent='Image Unsupported';
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
    const eligible=Boolean(st && st.available && !st.hold && (st.status !== 'working in Codex') && att.mgrId && mgrId && att.mgrId === mgrId && !att.inflight);
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
    if(notice) { notice.textContent='Sending...'; notice.className='pm-inline-notice'; }
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
    if(p.tracking){
      const t=el('p','pc-muted','Task authority: '+p.tracking.authority+' ');
      if(p.tracking.url)t.append(link(p.tracking.url,'open tracker'));
      left.append(t);
    }
    const ds=p.decisions||[];
    left.append(el('h2','pc-h2','Decisions'));
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
    }else left.append(empty('No owner decisions recorded.'));
    left.append(el('h2','pc-h2','Tasks'));
    const ts=p.tasks||[];
    if(ts.length){
      const ul=el('ul','pc-list');
      ts.forEach(t=>{
        const li=el('li');
        li.append(link(t.url,t.id+' '+t.title),el('span','pc-tag',t.state.replace('_',' ')));
        ul.append(li);
      });
      left.append(ul);
    }else left.append(empty('No tasks listed in the snapshot.'));
    left.append(el('h2','pc-h2','Reports'));
    const rs=[...(p.reports||[])].sort((a,b)=>new Date(b.at)-new Date(a.at));
    if(rs.length){
      const ul=el('ul','pc-list pc-reports');
      rs.forEach(r=>{
        const li=el('li');
        li.append(el('strong',null,r.from+' · '+when(r.at)),el('p',null,r.text),link(r.source_url,'source'));
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
      heading.append(el('h2','pc-h2','Manager Chat · '+mgrName));
      if(st){
        const bar=el('div','pm-chat-status-bar');
        const statusTxt = st.hold ? 'Held' : (st.status || (st.available ? 'Idle' : 'Unavailable'));
        bar.append(el('span','pc-tag',statusTxt));
        if(st.hold){
          const rBtn=el('button','pm-resume-btn','Resume');
          rBtn.type='button';
          rBtn.onclick=async()=>{
            rBtn.disabled=true;
            try{
              await api('/api/projects/'+encodeURIComponent(pid)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
              await loadProjectChat(pid);
            }catch(err){ alert('Resume failed: '+err.message); }finally{ rBtn.disabled=false; }
          };
          bar.append(rBtn);
        }else if(st.available && !st.hold && (st.status==='working in Codex'||(st.deliveries||[]).some(d=>['sending','accepted','ambiguous'].includes(d.state)))){
          const sBtn=el('button','pm-stop-btn','Stop');
          sBtn.type='button';
          sBtn.onclick=async()=>{
            sBtn.disabled=true;
            try{
              await api('/api/projects/'+encodeURIComponent(pid)+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
              await loadProjectChat(pid);
            }catch(err){ alert('Stop failed: '+err.message); }finally{ sBtn.disabled=false; }
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
    if(ta) ta.placeholder='Message '+mgrName+' directly…';

    if(!st){
      if(conv) conv.replaceChildren(empty('Loading Manager conversation…'));
      if(ta) ta.disabled=true;
      if(sendBtn){
        sendBtn.disabled=true;
        sendBtn.textContent='Loading…';
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
        box.append(el('strong',null,'Chat Unavailable (Read-only)'));
        box.append(el('p','pc-muted',st.error||'Manager chat is not accessible on Desktop.'));
        conv.replaceChildren(box);
      }
      if(ta) ta.disabled=true;
      if(sendBtn){
        sendBtn.disabled=true;
        sendBtn.textContent='Unavailable';
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
          const author=el('span','author',m.sender==='owner'?'You':mgrName);
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
              thumb.alt='Attached image';
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
        sendBtn.textContent='Sending...';
      }else if(st.status==='working in Codex'){
        sendBtn.disabled=true;
        sendBtn.textContent='Busy';
      }else if(st.hold){
        sendBtn.disabled=true;
        sendBtn.textContent='Held';
        if(notice&&!notice.textContent){ notice.textContent='Chat is held. Resume before sending new messages.'; notice.className='pm-inline-notice held'; }
      }else if(hasUnresolved){
        sendBtn.disabled=true;
        sendBtn.textContent='Busy';
        if(notice&&!notice.textContent){ notice.textContent='Pending delivery in progress.'; notice.className='pm-inline-notice held'; }
      }else{
        const curImgs=projectDraftImages.get(pid)||[];
        if(curImgs.length>0){
          sendBtn.disabled=true;
          sendBtn.textContent='Image Unsupported';
        }else{
          sendBtn.disabled=false;
          sendBtn.textContent='Send';
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
    searchInput.placeholder='Search known projects…';
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
    countMeta.textContent=isFiltered?`Showing ${visibleCount} of ${allCount} (${allCount-visibleCount} hidden)`:`All ${allCount} projects visible`;

    const selectApplicableBtn=el('button','pc-vis-btn','Select all applicable');
    selectApplicableBtn.type='button';
    selectApplicableBtn.id='pc-select-applicable-btn';
    selectApplicableBtn.onclick=()=>{
      const applicableIds=known.filter(p=>p.applicable).map(p=>p.id);
      setVisibleProjectIds(applicableIds);
    };

    const restoreAllBtn=el('button','pc-vis-btn','Restore all');
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
        head.append(el('span','pc-status pc-current','Current'));
      }
      if(p.status==='ok'){
        head.append(el('span','pc-status pc-working','Roster valid'));
      }else if(p.status==='no_teams'){
        head.append(el('span','pc-status','No teams'));
      }else if(p.status==='source_unavailable'){
        head.append(el('span','pc-status pc-blocked','Source unavailable'));
      }else if(p.status==='roster_error'){
        head.append(el('span','pc-status pc-blocked','Roster error'));
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
    if(!data){body.append(empty('Loading projects…'));v.append(header('Projects',''),body);return;}
    if(!data.available){v.append(header('Projects','No project data'),body);body.append(empty(data.error||'Project status is unavailable.'));return;}
    const ps=projects();
    if(view==='overview'){v.append(header('Projects',ps.length+' projects · updated '+when(data.snapshot.generated_at)));const controls=renderVisibilityControls();if(controls)body.append(controls);const grid=el('div','pc-grid');[...ps].sort((a,b)=>pending(b).length-pending(a).length).forEach(p=>grid.append(card(p)));if(ps.length){body.append(grid);}else{const emptyMsg=el('p','empty');emptyMsg.textContent=allProjects().length?'All projects are hidden by filter. Click "Restore all" above to show all projects.':'No projects in the snapshot.';body.append(emptyMsg);}}
    else if(view==='inbox'){const rank=d=>[d.urgency==='urgent'?0:1,d.due||'9999',d.requested_at].join('|');const all=ps.flatMap(p=>pending(p).map(d=>[d,p])).sort(([a],[b])=>rank(a)<rank(b)?-1:rank(a)>rank(b)?1:0);v.append(header('Waiting for you',all.length+' explicit owner decisions'));const ul=el('ul','pc-decisions pc-inbox');all.forEach(([d,p])=>ul.append(decisionItem(d,p)));body.append(all.length?ul:empty('Nothing is waiting for you.'));}
    else{const p=ps.find(x=>x.id===view);if(!p){v.append(header('Project','Not in snapshot'));body.append(empty('This project is no longer in the snapshot.'));}else{v.append(header(p.name,STATUS[p.status]+(p.manager?' · '+p.manager:'')));
      const split=el('div','pm-split-layout');
      split.id='pm-split-layout';
      const left=el('div','pm-left-col');
      left.id='pm-left-col-box';
      const divider=el('div','pm-split-divider');
      divider.id='pm-split-divider';
      divider.setAttribute('role','separator');
      divider.setAttribute('tabindex','0');
      divider.setAttribute('aria-label','Resize layout');
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
    if(!data?.available){n.textContent=data?data.error:'Loading…';return;}n.textContent=problem?'Update failed; showing last valid data':'';const total=projects().reduce((s,p)=>s+pending(p).length,0);
    const row=(label,meta,key,cls)=>{const b=el('button','personal-row'+(cls?' '+cls:''));b.type='button';b.setAttribute('aria-pressed',String(!document.getElementById('projects-view').hidden&&view===key));const copy=el('span','session-copy');copy.append(el('strong',null,label),el('small',null,meta));b.append(copy);b.onclick=()=>open(key);list.append(b);};
    const visiblePs=projects();const countLabel=getVisibleProjectIds()!==null?`${visiblePs.length}/${allProjects().length} projects`:`${visiblePs.length} projects`;row('Overview',countLabel,'overview');row('Waiting for you',total+' decisions','inbox',total?'pc-attention':'');visiblePs.forEach(p=>row(p.name,(STATUS[p.status]||p.status)+(pending(p).length?' · '+pending(p).length+' waiting':''),p.id));}
  function open(key){view=key;officeMedia.stop();officeSpeech.stop();officeView='projects';selectMenu('projects');for(const id of ['room-view','personal-view','session-detail'])document.getElementById(id).hidden=true;document.getElementById('projects-view').hidden=false;
    const title=key==='overview'?'Projects':key==='inbox'?'Waiting for you':(projects().find(p=>p.id===key)?.name||'Project');addConversation('project',key,title);render();renderList();load();if(key!=='overview'&&key!=='inbox'){loadProjectChat(key);loadProjectActivity(key);}}
  window.officeProjects={open,load,hide(){document.getElementById('projects-view').hidden=true;renderList();},isProjectVisible,getVisibleProjectIds,setVisibleProjectIds,allKnownProjects};
  setInterval(()=>{
    if(document.getElementById('projects-view').hidden) return;
    load();
  }, 10000);
})();
