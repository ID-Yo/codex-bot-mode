(()=>{
  const view=document.getElementById('updates-view');
  let confirmed=null, preview=null, busy=false, errorText='', inventory=null;
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  const request=(action,data)=>api('/api/updates/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function button(label,action){const b=el('button',label);b.type='button';b.disabled=busy;b.onclick=()=>run(action);return b;}
  async function run(action){if(busy)return;busy=true;errorText='';render();try{await action();}catch(error){errorText=error.message+' Last confirmed state retained.';}finally{busy=false;render();}}
  async function refresh(){confirmed=await api('/api/updates/status');render();}
  function render(){
    const previous={};for(const input of view.querySelectorAll('[data-field]'))previous[input.dataset.field]=input.value;
    view.replaceChildren();const header=el('header');header.append(el('p',officeI18n.t('CODEX OFFICE / UPDATES')),el('h1',officeI18n.t('Your installed components')),el('p',officeI18n.t('Choose a project and inspect the exact changes before approval.')));
    header.append(button(officeI18n.t('Refresh status'),refresh));view.append(header);
    const status=el('p',errorText||confirmed?.reason||officeI18n.t('Checks never apply updates automatically.'));status.id='update-status';status.setAttribute('role','status');view.append(status);
    if(!confirmed){view.append(el('p',officeI18n.t('Update status has not been checked.')));return;}
    view.append(el('p','Discovery: '+(confirmed.discovery_freshness||officeI18n.t('Unknown'))));
    const history=el('section');history.append(el('h2',officeI18n.t('Confirmed activity')));
    for(const item of confirmed.history||[])history.append(el('p',[item.project_id,item.release_id||(item.components||[]).join(', '),item.state].filter(Boolean).join(' · ')));
    if(!(confirmed.history||[]).length)history.append(el('p',confirmed.history_coverage==='complete'?officeI18n.t('No recorded transactions.'):officeI18n.t('Transaction history is incomplete; inspect the recovery journal.')));
    view.append(history);
    if(confirmed.history_coverage==='partial')view.append(el('p',officeI18n.t('Some transaction evidence is unavailable. Inspect journals before retrying.')));
    if(confirmed.components)view.append(el('pre',JSON.stringify(confirmed.components,null,2)));
    if(confirmed.configured===false)return;
    view.append(button(officeI18n.t('Check for updates'),async()=>{await request('check',{});await refresh();}));
    const available=confirmed.discovery?.last_check;
    if(available?.release_id)view.append(button(officeI18n.t('Download and verify {release}',{release:available.release_id}),async()=>{await request('stage',{release_id:available.release_id});await refresh();}));
    const form=el('section');form.append(el('h2',officeI18n.t('Select a scoped update')));
    function field(name,label,placeholder){const wrap=el('label',label);const input=el('input');input.dataset.field=name;input.value=previous[name]||'';input.placeholder=placeholder||'';wrap.append(input);form.append(wrap);return input;}
    const project=field('project',officeI18n.t('Configured project ID'),(confirmed.projects||[]).join(', '));const release=field('release',officeI18n.t('Verified release ID'),(confirmed.releases||[]).map(r=>r.release_id).join(', '));
    const component=field('component',officeI18n.t('One component ID'));const target=field('target',officeI18n.t('Installed component ID'));
    form.append(button(officeI18n.t('Read inventory'),async()=>{inventory=await request('inventory',{project_id:project.value});}));
    form.append(button(officeI18n.t('Preview selected update'),async()=>{preview=null;render();const result=await request('preview',{project_id:project.value,release_id:release.value,components:[component.value],targets:{schema:1,targets:{[component.value]:target.value}}});preview=result;render();}));view.append(form);
    if(inventory)view.append(el('pre',JSON.stringify(inventory,null,2)));
    if(preview){const section=el('section');section.append(el('h2',officeI18n.t('Exact preview')),el('p','Project: '+preview.project_id+' · Release: '+preview.release_id));
      section.append(el('pre',JSON.stringify(preview.plan.selected_targets||[],null,2)));
      for(const op of preview.plan.operations||[]){const details=el('details');details.append(el('summary',op.action+' '+op.path),el('pre',op.diff||officeI18n.t('No text diff available')));section.append(details);}
      const conflicts=preview.plan.conflicts||[];for(const conflict of conflicts)section.append(el('p','Conflict: '+conflict.path+' — '+conflict.reason));
      section.append(el('p','Plan SHA-256: '+preview.plan.plan_sha256));
      if(!conflicts.length)section.append(button(officeI18n.t('Approve and apply this exact plan'),async()=>{await request('apply',{handle:preview.handle,approved_digest:preview.plan.plan_sha256});preview=null;await refresh();}));
      else section.append(el('p',officeI18n.t('Resolve conflicts using the CLI and create a fresh preview.')));
      view.append(section);
    }
    view.append(el('p',officeI18n.t('Applied files remain pending activation until the agent confirms loading them. Recovery and rollback use the transaction CLI when Office is unavailable.')));
  }
  function open(){if(window.officeShell&&!window.officeShell.canNavigate())return;window.officeShell?.activate('updates');officeMedia.stop();officeSpeech.stop();officeProjects.hide();officeView='updates';selectMenu('updates');for(const id of ['room-view','personal-view','session-detail','agents-view'])document.getElementById(id).hidden=true;view.hidden=false;render();run(refresh);}
  window.officeUpdates={open};
  window.addEventListener('office-language-changed',()=>{if(!view.hidden)render();});
})();
