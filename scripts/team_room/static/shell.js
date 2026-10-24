/* Atelier navigation reuses the existing runtime views and authenticated modules. */
(()=>{
  const {t,read,save}=officeI18n;
  const views=['room-view','personal-view','session-detail','updates-view','agents-view','projects-view','directory-view','settings-dialog','agent-detail','new-project-view','room-editor-view'];
  let route='dashboard',previous='dashboard';
  let restoreCurrent,restorePrevious;
  let collapsed=read('office_nav_collapsed',false)===true;
  let navWidth=Number(read('office_nav_width',200))||200;
  let rightWidth=Number(read('office_right_width',260))||260;
  let mode=read('office_appearance','light');if(!['light','dark'].includes(mode))mode='light';
  function layout(){
    navWidth=Math.max(160,Math.min(300,navWidth));rightWidth=Math.max(190,Math.min(460,rightWidth));
    document.documentElement.style.setProperty('--nav-width',navWidth+'px');
    document.documentElement.style.setProperty('--right-width',Math.min(rightWidth,Math.max(190,innerWidth-(collapsed?64:navWidth)-350))+'px');
    document.body.dataset.nav=collapsed?'collapsed':'expanded';document.body.dataset.mode=mode;
    $('nav-collapse').setAttribute('aria-expanded',String(!collapsed));
    $('nav-collapse').setAttribute('aria-label',t(collapsed?'Expand navigation':'Collapse navigation'));
    for(const b of document.querySelectorAll('[data-appearance]'))b.setAttribute('aria-pressed',String(b.dataset.appearance===mode));
    $('nav-divider').setAttribute('aria-valuenow',String(navWidth));
    const labels={dashboard:'Dashboard',waiting:'Waiting for me',projects:'Projects',agents:'Agents',rooms:'Rooms'};
    for(const b of document.querySelectorAll('[data-route]')){b.title=t(labels[b.dataset.route]);b.setAttribute('aria-label',b.title);}
  }
  function activate(next,title,restore){
    if(route!==next){previous=route;restorePrevious=restoreCurrent;route=next;}
    restoreCurrent=restore||(()=>go(next));
    for(const id of views)if($(id))$(id).hidden=true;
    for(const b of document.querySelectorAll('[data-route]')){
      const active=b.dataset.route===next||(next==='project'||next==='new-project')&&b.dataset.route==='projects'||next==='personal'&&b.dataset.route==='agents'||(next==='room'||next==='room-editor')&&b.dataset.route==='rooms'||next==='owner-decision'&&b.dataset.route==='waiting';
      b.setAttribute('aria-current',active?'page':'false');b.tabIndex=0;
    }
    $('office-settings').setAttribute('aria-current',next==='settings'?'page':'false');
    const labels={dashboard:'Dashboard',waiting:'Waiting for me',projects:'Projects',agents:'Agents',rooms:'Rooms',settings:'Settings',teams:'Teams',updates:'Updates','new-project':'New project','personal-directory':'Personal chats'};
    $('page-title').textContent=title||t(labels[next]||next);
    $('page-back').hidden=['dashboard','waiting','projects','agents','rooms'].includes(next);
    document.body.dataset.page=next;
    // Only known internal routes enter browser history; no user text or project paths.
    history.replaceState(null,'','#'+(['dashboard','waiting','projects','agents','rooms','settings'].includes(next)?next:next==='room'?'rooms':next==='personal'?'agents':next==='owner-decision'?'waiting':'projects'));
  }
  function go(next){
    if(route==='settings'&&next!=='settings'&&!window.officeSettings.canLeave())return;
    officeMedia.stop();officeSpeech.stop();
    if(next==='settings'){window.officeSettings.open();return;}
    if(['dashboard','waiting','projects'].includes(next)){officeProjects.open(next==='dashboard'?'dashboard':next==='waiting'?'inbox':'overview');return;}
    if(next==='agents'){officeAgents.open();return;}
    if(next==='updates'){officeUpdates.open();return;}
    if(next==='personal-directory'){activate(next);officeView='directory';selectMenu('bots');$('directory-view').hidden=false;loadBots();return;}
    if(!['rooms','teams'].includes(next)){go('dashboard');return;}
    activate(next);officeView='directory';selectMenu(next==='teams'?'teams':'rooms');$('directory-view').hidden=false;
    if(next==='teams')loadTeams();else loadRooms();
  }
  function divider(handle,kind){
    const value=()=>kind==='nav'?navWidth:rightWidth;
    const change=n=>{if(kind==='nav')navWidth=n;else rightWidth=n;layout();handle.setAttribute('aria-valuenow',String(value()));};
    const persist=()=>save(kind==='nav'?'office_nav_width':'office_right_width',value());
    handle.title=t('Resize with arrow keys; Home resets');handle.setAttribute('aria-valuemin',kind==='nav'?'160':'190');handle.setAttribute('aria-valuemax',kind==='nav'?'300':'460');
    handle.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home'].includes(e.key))return;e.preventDefault();change(e.key==='Home'?(kind==='nav'?200:260):value()+(e.key==='ArrowRight'?1:-1)*(kind==='nav'?1:-1)*(e.shiftKey?40:10));persist();});
    handle.addEventListener('dblclick',()=>{change(kind==='nav'?200:260);persist();});
    handle.addEventListener('pointerdown',e=>{
      if(e.button!==0)return;e.preventDefault();handle.setPointerCapture(e.pointerId);const start=e.clientX,initial=value();
      const move=event=>change(initial+(event.clientX-start)*(kind==='nav'?1:-1));
      const finish=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',finish);handle.removeEventListener('pointercancel',finish);persist();};
      handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);
    });
  }
  const canNavigate=()=>route!=='settings'||window.officeSettings.canLeave();
  window.officeShell={activate,go,divider,canNavigate,get route(){return route;}};
  for(const b of document.querySelectorAll('[data-route]'))b.onclick=()=>go(b.dataset.route);
  $('office-settings').onclick=()=>go('settings');$('page-back').onclick=()=>{
    if(!canNavigate())return;
    if(restorePrevious&&previous!=='settings')restorePrevious();else go('dashboard');
  };
  $('settings-teams').onclick=()=>go('teams');$('settings-updates').onclick=()=>go('updates');
  $('nav-collapse').onclick=()=>{collapsed=!collapsed;save('office_nav_collapsed',collapsed);layout();};
  for(const b of document.querySelectorAll('[data-appearance]'))b.onclick=()=>{mode=b.dataset.appearance;save('office_appearance',mode);layout();};
  $('ui-language').value=officeI18n.language;$('ui-language').onchange=e=>officeI18n.setLanguage(e.target.value);
  divider($('nav-divider'),'nav');window.addEventListener('resize',layout);
  window.addEventListener('office-language-changed',()=>{
    layout();$('ui-language').value=officeI18n.language;
    const labels={dashboard:'Dashboard',waiting:'Waiting for me',projects:'Projects',agents:'Agents',rooms:'Rooms',settings:'Settings',teams:'Teams',updates:'Updates','new-project':'New project'};
    if(labels[route])$('page-title').textContent=t(labels[route]);
  });
  window.addEventListener('DOMContentLoaded',()=>{officeI18n.apply();layout();const requested=location.hash.slice(1);go(['dashboard','waiting','projects','agents','rooms','settings'].includes(requested)?requested:'dashboard');});
})();
