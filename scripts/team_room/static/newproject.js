/* Full-page New project: prepare a concrete plan, review it, then confirm registration in Office. */
(()=>{
  const t=(key,values)=>officeI18n.t(key,values),$=id=>document.getElementById(id);
  const TEAMS=[['manager','Manager only'],['development','Development team'],['marketing','Marketing team'],['development_marketing','Development and Marketing teams']];
  const KINDS=[['existing','Existing Codex project'],['new','New project (Office creates the folder)']];
  const STEPS={create_folder:'Project folder created',init_roster:'Empty team roster created',register:'Registered in Office',appoint_team:'Team appointed in Codex'};
  const label=(list,value)=>t(list.find(item=>item[0]===value)?.[1]||value);
  const fresh=()=>({name:'',folder:'',repository:'',kind:'existing',initial_team:'manager',show_dashboard:true});
  let draft=fresh(),stage='form',plan=null,result=null,error='',busy=false,copied=false;
  function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
  function configuration(){
    const config={name:draft.name.trim(),folder:draft.folder.trim(),kind:draft.kind,initial_team:draft.initial_team,show_dashboard:draft.show_dashboard};
    if(draft.repository.trim())config.repository=draft.repository.trim();
    return config;
  }
  const UNCERTAIN='Result uncertain: the connection reply was lost, so Office may already have registered this project. Use Check again to see the actual state.';
  const UNCERTAIN_CREATE='Result uncertain: the reply was lost, so Office may already have created the folder and registered this project. Use Check again to see the actual state.';
  async function call(path,body,mayHaveChanged){
    let response;
    try{response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Room-Token':document.querySelector('meta[name="room-token"]').content},body:JSON.stringify(body)});}
    catch{throw Error(mayHaveChanged||'Office is unavailable. Nothing was changed by this step; check again.');}
    const data=await response.json().catch(()=>null);
    if(data===null&&mayHaveChanged)throw Error(mayHaveChanged);
    if(!response.ok)throw Error((data&&data.error)||'Request failed');
    return data||{};
  }
  async function prepare(){
    if(busy)return;busy=true;error='';paint();
    try{plan=await call('/api/projects/prepare',{configuration:configuration()});stage='plan';}
    catch(e){error=e.message;}
    finally{busy=false;paint();}
  }
  async function confirm(){
    if(busy||!plan||!['ready_to_connect','ready_to_create'].includes(plan.state))return;busy=true;error='';paint();
    try{
      result=await call('/api/projects/confirm',{configuration:plan.configuration,version:plan.version,confirmed:true},plan.state==='ready_to_create'?UNCERTAIN_CREATE:UNCERTAIN);
      applyVisibility(result.project_id,plan.configuration.show_dashboard);
      stage='result';
      if(window.officeProjects)window.officeProjects.load();
    }catch(e){error=e.message;}
    finally{busy=false;paint();}
  }
  function applyVisibility(id,show){
    const projects=window.officeProjects,current=projects.getVisibleProjectIds();
    if(show){if(current===null)return;current.add(id);projects.setVisibleProjectIds(current);}
    else{const ids=current||new Set(projects.allKnownProjects().map(project=>project.id));ids.delete(id);projects.setVisibleProjectIds(ids);}
  }
  function field(labelText,input,hint){
    const wrap=el('label','np-field');wrap.append(el('span','np-label',t(labelText)),input);
    if(hint)wrap.append(el('small','np-hint',t(hint)));return wrap;
  }
  function textInput(id,key,attrs){
    const input=el('input');input.id=id;input.type='text';input.value=draft[key];Object.assign(input,attrs||{});
    input.autocomplete='off';input.spellcheck=false;input.oninput=()=>{draft[key]=input.value;};return input;
  }
  function select(id,key,list){
    const input=el('select');input.id=id;
    for(const [value,text] of list){const option=el('option',null,t(text));option.value=value;input.append(option);}
    input.value=draft[key];input.onchange=()=>{draft[key]=input.value;};return input;
  }
  function errorLine(){
    const line=el('p','np-error',error?t(error):'');line.id='np-error';line.setAttribute('role','alert');line.hidden=!error;return line;
  }
  function renderForm(page){
    page.append(el('p','np-lead',t('Choose a project folder, repository and starting team. Nothing is created or registered until you review the plan and confirm.')));
    const form=el('form','np-form');form.id='np-form';
    const name=textInput('np-name','name',{required:true,maxLength:100,placeholder:t('For example: Client portal')});
    const folder=textInput('np-folder','folder',{required:true,maxLength:512,placeholder:'D:\\Projects\\client-portal'});
    const repository=textInput('np-repository','repository',{maxLength:160,placeholder:'owner/repository'});
    const show=el('input');show.type='checkbox';show.id='np-dashboard';show.checked=draft.show_dashboard;show.onchange=()=>{draft.show_dashboard=show.checked;};
    const showLabel=el('label','np-check');showLabel.append(show,el('span',null,t('Show on the Dashboard after it is added')));
    const actions=el('div','np-actions');
    const next=el('button',null,t(busy?'Preparing…':'Prepare plan'));next.type='submit';next.id='np-prepare';next.disabled=busy;
    const cancel=el('button','secondary',t('Cancel'));cancel.type='button';cancel.id='np-cancel';cancel.onclick=()=>$('page-back').click();
    actions.append(next,cancel);
    form.append(field('Project name',name),field('Project folder',folder,'Absolute path on this computer.'),field('GitHub repository (optional)',repository,'Only the owner/repository name is stored; no repository is created.'),
      field('How is it added?',select('np-kind','kind',KINDS)),field('Starting team',select('np-team','initial_team',TEAMS),'This records the request. No agent is appointed without your approval in Codex.'),showLabel,errorLine(),actions);
    form.onsubmit=event=>{event.preventDefault();if(!form.reportValidity())return;prepare();};
    page.append(form);
  }
  function handoffText(config){
    const lines=[config.kind==='existing'?t('Open the existing project “{name}” in folder {folder}.',{name:config.name,folder:config.folder}):t('Create a new project “{name}” in folder {folder}.',{name:config.name,folder:config.folder})];
    if(config.repository)lines.push(t('Tracking repository: {repository}.',{repository:config.repository}));
    lines.push(t('Review the folder and its existing configuration; do not overwrite existing work.'));
    lines.push(t('Prepare this CBM team: {team}. Appointments need my explicit approval.',{team:label(TEAMS,config.initial_team)}));
    lines.push(t('When a Manager is appointed, tell me so I can confirm the connection in Office.'));
    lines.push(t('Record every stop caused by a blocker or required decision in a linked issue.'));
    return lines.join('\n');
  }
  function summaryRow(list,term,value){const row=el('div');row.append(el('dt',null,t(term)),el('dd',null,value));list.append(row);}
  function progress(list,done,text,pendingText){
    const item=el('li','np-step '+(done?'done':'pending'));
    item.append(el('span','np-mark',done?'✓':'○'),el('span',null,t(text)),el('small','pc-muted',t(done?'Done':pendingText||'Pending')));list.append(item);
  }
  function handoffBox(page,text){
    const prompt=el('textarea','np-prompt');prompt.id='np-handoff';prompt.readOnly=true;prompt.rows=4;prompt.value=text;
    const copy=el('button','secondary',t(copied?'Copied':'Copy instruction'));copy.type='button';copy.id='np-copy';
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(prompt.value);}catch{prompt.select();}copied=true;copy.textContent=t('Copied');};
    page.append(el('label','np-label',t('Instruction for Codex')),prompt,copy);
  }
  function renderPlan(page){
    const config=plan.configuration,create=plan.state==='ready_to_create',ready=plan.state==='ready_to_connect'||create;
    const banner=el('div','np-banner '+(ready?'ready':'setup'));banner.id='np-state';
    banner.append(el('strong',null,t(create?'Ready to create':ready?'Ready to connect':'Codex setup is needed first')),
      el('p',null,t(create?'Confirming creates the project folder and an empty team roster and registers the project in Office. No repository is created and no team is appointed; that step needs Codex afterwards.':ready?'This existing project has an appointed Manager. Confirming adds its folder to Office\'s project registry only; no repository, agent or work is created.':'Office cannot create a Codex project, repository or team, and has created nothing. Complete the steps below in Codex, then check again.')));
    page.append(banner);
    const summary=el('dl','np-summary');
    summaryRow(summary,'Project name',config.name);summaryRow(summary,'Project folder',config.folder);
    summaryRow(summary,'GitHub repository',config.repository||t('None'));summaryRow(summary,'How is it added?',label(KINDS,config.kind));
    summaryRow(summary,'Starting team',label(TEAMS,config.initial_team));summaryRow(summary,'Dashboard',t(config.show_dashboard?'Shown after it is added':'Hidden after it is added'));
    summaryRow(summary,'Project ID',plan.project_id);summaryRow(summary,'Appointed Managers found',String(plan.manager_count));
    if(plan.already_connected)summaryRow(summary,'Office registry',t('Already registered; confirming keeps one registration and applies only the Dashboard choice.'));
    page.append(el('h2',null,t('Plan')),summary);
    const steps=el('ol','np-progress');steps.id='np-progress';
    if(create){
      progress(steps,true,'Folder and repository reviewed');
      for(const step of plan.setup_steps||[])progress(steps,step.done,STEPS[step.id]||step.id,step.supported?'Waits for your confirmation':'Needs Codex');
    }else{
      progress(steps,true,'Folder and repository reviewed');
      progress(steps,plan.manager_count>0&&config.kind==='existing','Project open in Codex with an appointed Manager','Needs Codex');
      progress(steps,plan.already_connected,'Registered in Office','Waits for your confirmation');
    }
    page.append(el('h2',null,t('Progress')),steps);
    if(create){
      const team=(plan.setup_steps||[]).find(step=>step.id==='appoint_team');
      page.append(el('h2',null,t('Next step in Codex, after the project is created')));
      if(team&&team.handoff)handoffBox(page,team.handoff);
    }
    if(!ready){
      const todo=el('ol','np-steps');for(const step of plan.steps||[])todo.append(el('li',null,t(step)));
      const prompt=el('textarea','np-prompt');prompt.id='np-handoff';prompt.readOnly=true;prompt.rows=8;prompt.value=handoffText(config);
      const copy=el('button','secondary',t(copied?'Copied':'Copy instruction'));copy.type='button';copy.id='np-copy';
      copy.onclick=async()=>{try{await navigator.clipboard.writeText(prompt.value);}catch{prompt.select();}copied=true;copy.textContent=t('Copied');};
      page.append(el('h2',null,t('Next steps in Codex')),todo,el('label','np-label',t('Instruction for Codex')),prompt,copy);
    }
    page.append(errorLine());
    const actions=el('div','np-actions');
    if(ready){const go=el('button',null,t(create?(busy?'Creating…':'Create project'):(busy?'Connecting…':'Connect to Office')));go.type='button';go.id='np-confirm';go.disabled=busy;go.onclick=confirm;actions.append(go);}
    const again=el('button','secondary',t('Check again'));again.type='button';again.id='np-recheck';again.disabled=busy;again.onclick=prepare;
    const edit=el('button','secondary',t('Edit'));edit.type='button';edit.id='np-edit';edit.disabled=busy;edit.onclick=()=>{stage='form';error='';paint();};
    actions.append(again,edit);page.append(actions);
  }
  function renderCreated(page){
    const banner=el('div','np-banner setup');banner.id='np-state';
    banner.append(el('strong',null,t('Project created — team not appointed yet')),el('p',null,t('Office created the folder and an empty team roster and registered the project. No team, Manager or Codex chat exists yet.')));
    const summary=el('dl','np-summary');summaryRow(summary,'Project ID',result.project_id);summaryRow(summary,'Dashboard',t(plan.configuration.show_dashboard?'Shown on the Dashboard':'Hidden from the Dashboard; open it from Projects'));
    page.append(banner,summary);
    const done=el('ol','np-progress');done.id='np-completed';
    for(const id of result.completed||[])progress(done,true,STEPS[id]||id);
    for(const step of result.remaining||[])progress(done,false,STEPS[step.id]||step.id,'Needs Codex');
    page.append(el('h2',null,t('Progress')),done);
    const blocker=result.blocker;
    if(blocker){
      const box=el('div','np-banner setup');box.id='np-blocker';box.dataset.issueRequired=String(blocker.issue_required===true);
      box.append(el('strong',null,t(blocker.issue_required===true?'Team not appointed yet — blocker without issue':'Team not appointed yet')),el('p',null,t(blocker.reason||'')),
        el('p',null,t('Record this stop in a linked issue. Office does not create it.')));
      page.append(box);
    }
    const next=(result.remaining||[]).find(step=>step.handoff);
    if(next){page.append(el('h2',null,t('Next step in Codex')));handoffBox(page,next.handoff);}
    page.append(el('p','pc-muted',t('After a Manager is appointed in Codex, add the project again as an existing project to connect it.')));
    const actions=el('div','np-actions');
    const dash=el('button',null,t('Open Dashboard'));dash.type='button';dash.id='np-dashboard-open';dash.onclick=()=>window.officeShell.go('dashboard');
    const another=el('button','secondary',t('Add another project'));another.type='button';another.id='np-another';another.onclick=()=>{draft=fresh();plan=null;result=null;error='';copied=false;stage='form';paint();};
    actions.append(dash,another);page.append(actions);
  }
  function renderResult(page){
    if(result.state==='created_pending_team')return renderCreated(page);
    const banner=el('div','np-banner ready');banner.id='np-state';
    banner.append(el('strong',null,t('Project connected')),el('p',null,t(result.already_connected?'This project was already registered; Office kept one registration.':'Office registered the project folder.')));
    const summary=el('dl','np-summary');summaryRow(summary,'Project ID',result.project_id);summaryRow(summary,'Dashboard',t(plan.configuration.show_dashboard?'Shown on the Dashboard':'Hidden from the Dashboard; open it from Projects'));
    page.append(banner,summary);
    const actions=el('div','np-actions');
    const dash=el('button',null,t('Open Dashboard'));dash.type='button';dash.id='np-dashboard-open';dash.onclick=()=>window.officeShell.go('dashboard');
    const another=el('button','secondary',t('Add another project'));another.type='button';another.id='np-another';another.onclick=()=>{draft=fresh();plan=null;result=null;error='';copied=false;stage='form';paint();};
    actions.append(dash,another);page.append(actions);
  }
  function paint(){
    const view=$('new-project-view');if(!view)return;
    const focused=document.activeElement?.id,page=el('div','np-page');
    const head=el('header','pc-header');const title=el('div');title.append(el('h1',null,t('New project')),el('span','room-description',t(stage==='form'?'Step 1 of 2 · Details':stage==='plan'?'Step 2 of 2 · Review':'Done')));head.append(title);
    page.append(head);
    if(stage==='form')renderForm(page);else if(stage==='plan')renderPlan(page);else renderResult(page);
    view.replaceChildren(page);
    if(focused&&$(focused)&&!$(focused).disabled)$(focused).focus();
  }
  function open(startFresh){
    if(window.officeShell&&!window.officeShell.canNavigate())return;
    if(startFresh===true&&stage==='result'){draft=fresh();plan=null;result=null;error='';copied=false;stage='form';}
    window.officeShell?.activate('new-project',t('New project'),open);
    officeMedia.stop();officeSpeech.stop();officeView='new-project';selectMenu('projects');
    for(const id of ['room-view','personal-view','session-detail','projects-view'])$(id).hidden=true;
    $('new-project-view').hidden=false;paint();
  }
  window.officeNewProject={open};
  window.addEventListener('office-language-changed',()=>{if(!$('new-project-view').hidden)paint();});
})();
