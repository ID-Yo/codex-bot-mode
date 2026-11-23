// Isolated browser fixture for the dashboard Delivery panel (#424, S4-D). Never connects to Office or Codex.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../scripts/team_room/static');
const out=path.resolve(process.argv[2]||'output/cbm424-delivery-panel');fs.mkdirSync(out,{recursive:true});
const base='http://delivery-panel-fixture.test';
const now=new Date().toISOString();
const projects=['Alpha','Beta'].map((name,index)=>({id:name.toLowerCase(),name,status:'working',summary:'Fixture summary',manager:name+' Manager',open_tasks:1,
  source:{kind:'local',label:name+' Report',available:true,as_of:now,url:'https://github.com/example/project/issues/'+(index+1)},tasks:[],reports:[],decisions:[]}));
const project=(name,over={})=>({name,pending:2,oldest_pending_seconds:150,ambiguous:1,needs_attention:3,relay_reachable:true,pump_alive:true,last_delivery_age_seconds:300,problems:[],...over});
const model=(over={})=>({schema:1,observed_at:now,state:'ok',reason:'healthy',age_seconds:30,supervisor:{at:now,interval_seconds:120,pass_seconds:1,version:'abc'},desktop_running:true,office_ok:true,
  relay:{ok:true,manager_changed:false,last_restart:{at:now,action:null,reason:'relay down',detail:'started',ok:true}},last_pump_action:{at:now,action:'pump started',reason:'work was waiting',detail:null,ok:null},
  projects:[project('Alpha'),project('Beta',{pending:0,oldest_pending_seconds:null,ambiguous:0,needs_attention:0,pump_alive:false,last_delivery_age_seconds:null}),project('A-very-long-project-name-that-must-wrap-inside-the-narrow-right-column-without-overflowing',{pending:12,oldest_pending_seconds:86400*3})],
  problems:[],incidents:[],...over});
const scenarios={
  ok:model(),
  problems:model({state:'problems',problems:['alpha: 3 report(s) could not be confirmed and need a decision (they no longer hold anything): outbox.py list --state needs-attention'],
    incidents:[{key:'needs-attention:alpha',severity:'manager',opened:now,reopened:1,text:'alpha: 3 report(s) could not be confirmed',remedy:'Run outbox.py list --state needs-attention, then outbox.py resolve <id> --as delivered|skipped|retry.',clearing:false}]}),
  unknown:model({state:'unknown',reason:'the supervisor last ran 900 s ago, expected every 120 s; it may not be running',age_seconds:900,supervisor:null,relay:null,last_pump_action:null,projects:[],desktop_running:null,office_ok:null}),
  failed:'FAIL',
};
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const failures=[];
    for(const [scenario,reply] of Object.entries(scenarios))for(const mode of ['light','dark'])for(const lang of ['en','bg']){
      const page=await browser.newPage({viewport:{width:1280,height:720}}),errors=[],mutations=[],calls=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.addInitScript(([m,l])=>{localStorage.setItem('office_appearance',JSON.stringify(m));localStorage.setItem('office_language',JSON.stringify(l));window.EventSource=class {addEventListener(){}close(){}};},[mode,lang]);
      await page.route(base+'/**',async route=>{
        const req=route.request(),url=new URL(req.url());let data;
        if(req.method()!=='GET')mutations.push(req.method()+' '+url.pathname);
        if(url.pathname==='/api/outbox-health'){calls.push(url.pathname);if(reply==='FAIL')return route.fulfill({status:503,contentType:'application/json',body:'{"error":"x"}'});data=reply;}
        else if(url.pathname==='/api/projects')data={available:true,snapshot:{generated_at:now,projects}};
        else if(url.pathname==='/api/owner-inbox')data={schema:1,observed_at:now,items:[],complete:true,warnings:[],coverage:'Fixture'};
        else if(url.pathname==='/api/registry/overview')data={status:'ok',projects:projects.map(p=>({id:p.id,name:p.name,applicable:true})),bots:[],teams:[]};
        else if(url.pathname==='/api/registry/agents')data={agents:[]};
        else if(url.pathname==='/api/bots')data={bots:[],lead:'manager'};
        else if(url.pathname==='/api/rooms')data={rooms:[]};
        else if(url.pathname==='/api/state')data={status:'idle',hold:true,active:null,event_cursor:0,turns:0,remaining_seconds:null,participants:[],journal:[],deliveries:[],provisional:{},intents:[],work:{tasks:[]}};
        else if(url.pathname==='/api/settings/office/general')data={name:'Fixture Office',version:'1'};
        else if(url.pathname.startsWith('/api/'))data={};
        if(data!==undefined)return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
        const file=path.join(root,url.pathname==='/'?'index.html':url.pathname.slice(1));
        if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:''});
        return route.fulfill({contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)],body:fs.readFileSync(file,'utf8').replace('__ROOM_TOKEN__','fixture-only')});
      });
      await page.goto(base);await page.waitForSelector('.delivery-panel');
      await page.waitForFunction(()=>document.querySelector('.delivery-panel')?.dataset.state!=='loading');
      const tag=scenario+'-'+mode+'-'+lang;
      const result=await page.evaluate(()=>{
        const panel=document.querySelector('.delivery-panel'),aside=document.querySelector('#owner-dashboard-attention'),box=panel.getBoundingClientRect(),outer=aside.getBoundingClientRect(),problems=[];
        if(box.left<outer.left-1||box.right>outer.right+1)problems.push('panel sits outside its column');
        for(const el of panel.querySelectorAll('*')){if(el.scrollWidth>el.clientWidth+1&&getComputedStyle(el).display!=='inline')problems.push(el.tagName+' '+el.textContent.slice(0,30)+' overflows horizontally');}
        return {state:panel.dataset.state,text:panel.innerText,problems,inAside:aside.contains(panel),panels:document.querySelectorAll('.delivery-panel').length};
      });
      assert.equal(result.inAside,true);assert.equal(result.panels,1,'one panel, also after the inbox repaints');
      assert.deepEqual(result.problems,[],tag);
      const expectedState={ok:'ok',problems:'problems',unknown:'unknown',failed:'unknown'}[scenario];
      assert.equal(result.state,expectedState,tag);
      const heading=lang==='bg'?'\u0414\u043e\u0441\u0442\u0430\u0432\u043a\u0430':'Delivery';
      assert.ok(result.text.includes(heading),tag+' heading');
      if(scenario==='ok'&&lang==='en'){for(const s of ['Healthy','Relay up','2 waiting','Pump running','3 need attention','Last delivery 5 min ago'])assert.ok(result.text.includes(s),tag+' '+s);}
      if(scenario==='unknown'||scenario==='failed'){assert.ok(!/Healthy|\u0412 \u0440\u0435\u0434|Relay up|\u0420\u0435\u043b\u0435\u0442\u043e \u0440\u0430\u0431\u043e\u0442\u0438/.test(result.text),tag+' must not claim health');assert.ok(!/waiting|\u0447\u0430\u043a\u0430\u0442/.test(result.text),tag+' must not show counts');}
      assert.deepEqual(mutations,[],tag+' the panel must not write');
      assert.deepEqual(errors,[]);
      if(mode==='light'||scenario==='problems')await page.screenshot({path:path.join(out,'dashboard-'+tag+'.png')});
      await page.close();
    }
    console.log('CBM-424 delivery panel checks passed; screenshots in '+out);
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
