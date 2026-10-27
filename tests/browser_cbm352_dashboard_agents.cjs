// #352: dashboard card header, bounded activity labels, concrete coverage wording, scoped dated summary,
// compact EN/BG agent cards with Working/Idle/Waiting. Isolated HTTP responses; never connects to Office or Codex.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(process.argv[3]||path.resolve(__dirname,'../scripts/team_room/static'));
const out=path.resolve(process.argv[2]||'output/cbm352');fs.mkdirSync(out,{recursive:true});
const base='http://cbm352-fixture.test';
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const now=new Date().toISOString(),old=new Date(Date.now()-5*24*3600e3).toISOString();
    const mk=(id,name,role,extra)=>({bot_key:'route:'+id,bot_id:id,name,role,project_id:'routeport',project_name:'RoutePort',team_name:'Delivery',thread_id:'00000000-0000-0000-0000-00000000000'+extra.n,is_current:false,
      runtime:extra.runtime,execution:extra.execution||{state:'unknown'},task:extra.task||null,dependencies:extra.dependencies||[],transitions:[],coverage:'Coverage explanation.',last_result:extra.last_result||null,action:extra.action||null});
    const fresh={status:'active',source_at:now,freshness:'fresh'},idleRt={status:'idle',source_at:now,freshness:'fresh'};
    const agents=[
      mk('manager','Manager','Manager',{n:1,runtime:fresh,execution:{state:'working',observed_at:now,source_at:now,source:'relay'},task:{id:'T1',title:'Release 0.2.0',updated_at:now,workflow:'in_progress'},action:'Reviewing the release plan'}),
      mk('developer','Developer','Developer',{n:2,runtime:idleRt,execution:{state:'unknown'}}),
      mk('verifier','Verifier','Verifier',{n:3,runtime:idleRt,execution:{state:'waiting'},task:{id:'T2',title:'QA of 0.2.0',updated_at:now,workflow:'in_progress'},dependencies:[{target:'Owner approval for the production deploy',freshness:'fresh',source_at:now}]}),
      mk('reviewer','Reviewer','Code Reviewer',{n:4,runtime:{status:'idle',source_at:old,freshness:'stale'},execution:{state:'unknown'}}),
      mk('writer','Writer','Content Writer',{n:5,runtime:null,execution:{state:'unknown'}}),
      mk('planner','Planner','Project Planner',{n:6,runtime:idleRt,execution:{state:'paused'}}),
      mk('auditor','Auditor','Auditor',{n:7,runtime:idleRt,execution:{state:'waiting'},task:{id:'T3',title:'Audit',updated_at:now,workflow:'in_progress'},dependencies:[{target:'Owner sign-off',freshness:'unknown',source_at:old}]}),
    ];
    const teams=[{project_id:'routeport',team:'Delivery',status:'ok',members:agents.map(a=>({bot_key:a.bot_key,bot_id:a.bot_id,display_name:a.name,role:a.role,thread_id:a.thread_id}))}];
    const longBots=['Project Manager Agent','Senior Developer Agent','Independent Code Reviewer','Verification Engineer','Release Coordinator'];
    const activity={project_id:'routeport',status:'active',active_bots:longBots,fetched_at:now,source:'relay',partial_failure:true,
      bots:[...longBots.map(n=>({name:n,status:'active'})),{name:'Writer',status:'unknown'},{name:'Designer',status:'unknown'},{name:'Analyst',status:'idle'}]};
    const summary='Next: confirm the 0.2.0 release candidate. In progress: handoff of the signed bundle to QA with the exact SHA and the review comments.';
    for(const width of [1280,900,390]){
      const page=await browser.newPage({viewport:{width,height:900}}),errors=[];let failAgents=false;
      page.on('pageerror',e=>errors.push(e.stack));
      await page.addInitScript(()=>{window.EventSource=class {addEventListener(){}close(){}};});
      await page.route(base+'/**',async route=>{
        const req=route.request(),url=new URL(req.url());let data;
        if(url.pathname==='/api/owner-inbox')data={schema:1,observed_at:now,items:[],complete:true,warnings:[],coverage:'Fixture'};
        else if(url.pathname==='/api/projects')data={available:true,snapshot:{generated_at:now,projects:[{id:'routeport',name:'RoutePort',status:'working',summary,manager:'RoutePort Manager',open_tasks:3,source:{kind:'local',label:'RoutePort Manager Report',available:true,as_of:old},reports:[],tasks:[],decisions:[]}]}};
        else if(url.pathname==='/api/projects/routeport/activity')data=width===900?{...activity,bots:undefined,error:'Metadata partially unavailable'}:activity;
        else if(url.pathname==='/api/registry/overview')data={status:'ok',projects:[{id:'routeport',name:'RoutePort',applicable:true}],bots:agents,teams};
        else if(url.pathname==='/api/registry/agents'){if(failAgents)return route.fulfill({status:503,contentType:'application/json',body:'{}'});data={agents,coverage:'Technical coverage note.'};}
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
      await page.goto(base);await page.click('#dashboard-tab');await page.waitForSelector('#pc-card-routeport');
      // Activity is fetched when a project is opened; the dashboard card then shows it.
      await page.click('#pc-card-routeport .pc-card-head');await page.waitForSelector('#pm-header-subtitle .pc-act-active');
      const sub=await page.locator('#pm-header-subtitle').evaluate(n=>({w:n.getBoundingClientRect().width,max:Math.max(...[...n.children].map(c=>c.getBoundingClientRect().right))-n.getBoundingClientRect().left}));
      assert.ok(sub.max<=sub.w+1,'project header badges stay inside their row');
      const team=async()=>{await page.waitForSelector('.project-agent-card');const o={};for(const c of await page.locator('.project-agent-card').all())o[await c.locator('strong').first().innerText()]=await c.innerText();return o;};
      let tc=await team();
      assert.match(tc.Verifier,/Waiting/);assert.match(tc.Verifier,/Waiting on: Owner approval for the production deploy/,'team card names who or what is awaited');
      assert.match(tc.Auditor,/Waiting on: Owner sign-off \(historical \/ unknown\)/,'a resolved or stale dependency stays marked historical');
      assert.doesNotMatch(tc.Manager,/Waiting on/);
      await page.click('#dashboard-tab');await page.waitForSelector('#pc-card-routeport');
      await page.waitForFunction(()=>document.querySelector('#pc-card-badges-routeport .pc-activity')?.textContent.startsWith('Active'));
      // No page title or section heading; picker and New project remain; no empty band above the cards.
      const view=await page.locator('#projects-view').innerText();
      assert.doesNotMatch(view,/Projects in your dashboard/);assert.equal(await page.locator('#projects-view h1').count(),0);
      assert.equal(await page.locator('#projects-view .dashboard-toolbar >> text=Choose projects').count(),1);
      assert.equal(await page.locator('#projects-view .dashboard-toolbar button.new-project-open').count(),1);
      const top=await page.evaluate(()=>{const v=document.querySelector('#projects-view').getBoundingClientRect(),c=document.querySelector('#pc-card-routeport').getBoundingClientRect();return Math.round(c.top-v.top);});
      assert.ok(top<=90,'cards start close to the top, got '+top);
      // Title stays readable: full width, one or two lines, never a one-letter column.
      const geo=await page.evaluate(()=>{const card=document.querySelector('#pc-card-routeport'),name=card.querySelector('.pc-card-head strong'),b=card.querySelector('#pc-card-badges-routeport');
        const r=name.getBoundingClientRect(),c=card.getBoundingClientRect(),lh=parseFloat(getComputedStyle(name).lineHeight)||16;
        return {nameW:r.width,cardW:c.width,lines:Math.round(r.height/lh),badgesW:b.getBoundingClientRect().width,overflow:[...b.children].some(n=>n.getBoundingClientRect().right>c.right+1)};});
      assert.ok(geo.nameW>=geo.cardW*0.6,'title width '+JSON.stringify(geo));assert.ok(geo.lines<=2,'title lines '+JSON.stringify(geo));assert.equal(geo.overflow,false,'badges stay inside the card');
      const text=await page.locator('#pc-card-routeport').innerText();
      assert.match(text,/Active \(Project Manager Agent, Senior Developer Agent \+3\)/,'bounded active label');
      if(width===900){assert.match(text,/Status check incomplete/,'no count is invented when the payload has none');assert.doesNotMatch(text,/\d+ of \d+ agents/);
        assert.match(await page.locator('#pc-card-badges-routeport .pc-act-active').getAttribute('title'),/Metadata partially unavailable/,'details stay in the tooltip');}
      else assert.match(text,/2 of 8 agents not checked/,'concrete failed-check wording');
      assert.doesNotMatch(text,/Partial coverage/);
      assert.match(await page.locator('#pc-card-badges-routeport .pc-act-active').getAttribute('title'),/Release Coordinator/,'full list in the tooltip');
      assert.match(text,/RoutePort Manager Report · as of/,'summary is scoped and dated');
      await page.screenshot({path:path.join(out,'dashboard-'+width+'.png'),fullPage:true});
      // Agent cards.
      await page.click('#bots-tab');await page.waitForSelector('.agent-card');
      const cards={};for(const c of await page.locator('.agent-card').all())cards[(await c.locator('strong').first().innerText())]=await c.innerText();
      assert.match(cards.Manager,/Working/);assert.match(cards.Manager,/Reviewing the release plan/);
      assert.match(cards.Developer,/Idle/);assert.doesNotMatch(cards.Developer,/Unknown/,'idle agent with no task metadata is not Unknown');
      assert.match(cards.Verifier,/Waiting/);assert.match(cards.Verifier,/Waiting on[\s\S]*Owner approval for the production deploy/);
      assert.match(cards.Reviewer,/Unknown/);assert.match(cards.Reviewer,/stale or incomplete/);
      assert.match(cards.Writer,/Unknown/);assert.match(cards.Writer,/No runtime observation/);
      for(const t of Object.values(cards)){assert.doesNotMatch(t,/Observed at|Source at|Execution since|Last result: Unknown|Current action: Unknown/,'no repeated technical or empty rows in the main view');}
      assert.equal(await page.locator('.agent-about').evaluate(n=>n.open),false,'disclaimer is folded away');
      assert.doesNotMatch(await page.locator('#agents-view').innerText(),/Status reads never wake an agent/);
      await page.screenshot({path:path.join(out,'agents-'+width+'.png'),fullPage:true});
      if(width===1280){
        // A failed evidence refresh keeps an explicit pause; fresh states fall back to Unknown with the reason.
        failAgents=true;await page.click('#agents-refresh');
        await page.waitForFunction(()=>/Evidence refresh failed/.test(document.querySelector('#agents-view').innerText));
        const failedCards={};for(const c of await page.locator('.agent-card').all())failedCards[await c.locator('strong').first().innerText()]=await c.innerText();
        assert.match(failedCards.Planner,/Paused/);assert.match(failedCards.Planner,/refresh failed/i);
        assert.match(failedCards.Manager,/Unknown/);assert.doesNotMatch(failedCards.Manager,/Working/);
        failAgents=false;await page.click('#agents-refresh');await page.waitForFunction(()=>/Working/.test(document.querySelector('.agent-card').innerText));
        await page.click('[data-agent-key="route:developer"]');await page.waitForSelector('#agent-detail:not([hidden])');
        assert.match(await page.locator('#agent-detail').innerText(),/Execution: Idle/);
        await page.click('#agent-detail .agent-detail-head button.secondary:last-child').catch(()=>{});
        await page.click('#office-settings');await page.selectOption('#ui-language','bg');await page.click('#bots-tab');await page.waitForSelector('.agent-card');
        const bg=await page.locator('#agents-view').innerText();
        assert.match(bg,/Свободен/);assert.match(bg,/Работи/);assert.match(bg,/Изчаква/);assert.match(bg,/Неизвестно/);
        await page.click('#dashboard-tab');await page.waitForSelector('#pc-card-routeport');
        assert.match(await page.locator('#pc-card-routeport').innerText(),/2 от 8 агенти не са проверени/);
        await page.click('#pc-card-routeport .pc-card-head');tc=await team();
        assert.match(tc.Verifier,/Изчаква: Owner approval for the production deploy/,'BG team card names who or what is awaited');
        await page.click('#dashboard-tab');await page.waitForSelector('#pc-card-routeport');
        await page.screenshot({path:path.join(out,'dashboard-bg.png'),fullPage:true});
        for(const mode of ['dark']){await page.click('[data-appearance="'+mode+'"]');await page.waitForTimeout(500);await page.screenshot({path:path.join(out,'dashboard-dark.png'),fullPage:true});}
      }
      assert.deepEqual(errors,[]);await page.close();
    }
    console.log('PASS: dashboard title/badges at 1280/900/390, bounded labels, concrete coverage, dated summary, EN/BG compact agent states');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});

