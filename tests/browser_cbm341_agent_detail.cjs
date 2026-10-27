// Agent Detail (#341): compact summary, details disclosure and automatic per-file profile loading.
// Isolated HTTP responses; never connects to Office or Codex. Optional argv[3] points at another static root for a before/after measurement.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(process.argv[3]||path.resolve(__dirname,'../scripts/team_room/static'));
const out=path.resolve(process.argv[2]||'output/cbm341-agent-detail');fs.mkdirSync(out,{recursive:true});
const compact=!process.argv[3];
const base='http://agent-detail-fixture.test';
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],gets=[],posts=[];
    const now=new Date().toISOString();
    const agent={bot_key:'alpha:agent1',bot_id:'agent1',name:'Appointed Developer',role:'Developer',project_id:'alpha',project_name:'Alpha',team_name:'Delivery team',
      thread_id:'00000000-0000-0000-0000-000000000002',is_current:true,runtime:{status:'idle',source_at:now,freshness:'fresh'},execution:{state:'unknown'},
      task:{id:'TASK-1',title:'Recorded ticket',url:'https://github.com/example/project/issues/1',updated_at:now,workflow:'in_progress'},dependencies:[],transitions:[],coverage:'Coverage explanation.'};
    page.on('pageerror',e=>errors.push(e.stack));
    await page.addInitScript(()=>{window.EventSource=class {addEventListener(){}close(){}};});
    let soulFails=1;
    await page.route(base+'/**',async route=>{
      const req=route.request(),url=new URL(req.url());let data;
      if(url.pathname==='/api/owner-inbox')data={schema:1,observed_at:now,items:[],complete:false,warnings:[],coverage:'Fixture'};
      else if(url.pathname==='/api/projects')data={available:true,snapshot:{generated_at:now,projects:[{id:'alpha',name:'Alpha',status:'working',summary:'s',source:{available:true,as_of:now},reports:[]}]}};
      else if(url.pathname==='/api/registry/overview')data={status:'ok',projects:[{id:'alpha',name:'Alpha',applicable:true}],bots:[agent],teams:[]};
      else if(url.pathname==='/api/registry/agents')data={agents:[agent]};
      else if(url.pathname==='/api/bots')data={bots:[],lead:'manager'};
      else if(url.pathname==='/api/rooms')data={rooms:[]};
      else if(url.pathname==='/api/state')data={status:'idle',hold:true,active:null,event_cursor:0,turns:0,remaining_seconds:null,participants:[],journal:[],deliveries:[],provisional:{},intents:[],work:{tasks:[]}};
      else if(url.pathname==='/api/settings/office/general')data={name:'Fixture Office',version:'1'};
      else if(url.pathname.startsWith('/api/settings/bots/')){
        const bot=decodeURIComponent(url.pathname.split('/').at(-1));
        const file=url.searchParams.get('file')||JSON.parse(req.postData()||'{}').file;
        if(req.method()==='POST'){const body=JSON.parse(req.postData());posts.push(body);data={bot,file,content:body.content,version:'saved-version'};}
        else{
          gets.push(file);await new Promise(r=>setTimeout(r,150));
          if(file==='SOUL.md'&&soulFails>0){soulFails--;return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'not installed'})});}
          data={bot,file,content:'# '+bot+' '+file,version:'v-'+file,readonly:file==='memory',path:'agent/'+file};
        }
      }else if(url.pathname.startsWith('/api/'))data={};
      if(data!==undefined)return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      const file=path.join(root,url.pathname==='/'?'index.html':url.pathname.slice(1));
      if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)],body:fs.readFileSync(file,'utf8').replace('__ROOM_TOKEN__','fixture-only')});
    });
    await page.goto(base);await page.click('#bots-tab');await page.locator('[data-agent-key="alpha:agent1"],[data-bot-key="alpha:agent1"]').first().click();
    await page.waitForSelector('.agent-profile-settings');
    const metrics=await page.evaluate(()=>{const d=document.querySelector('#agent-detail'),p=d.querySelector('.agent-profile-settings');return {profileTop:Math.round(p.getBoundingClientRect().top-d.getBoundingClientRect().top+d.scrollTop),fieldRows:d.querySelectorAll('.agent-field').length};});
    fs.writeFileSync(path.join(out,compact?'metrics-after.json':'metrics-before.json'),JSON.stringify(metrics));
    if(!compact){console.log('BASELINE '+JSON.stringify(metrics));await page.screenshot({path:path.join(out,'detail-before.png'),fullPage:true});return;}
    const detail=page.locator('#agent-detail');
    // Automatic load of the default section, once.
    await page.waitForSelector('.agent-profile-content');
    assert.deepEqual(gets,['PROFILE.md']);
    assert.equal(await page.getByRole('button',{name:'Load selected section'}).count(),0);
    // Compact summary: two columns, unknown values stay visible as Unknown.
    const cols=await page.evaluate(()=>getComputedStyle(document.querySelector('.agent-summary')).gridTemplateColumns.split(' ').length);
    assert.equal(cols,2);
    const text=await detail.innerText();
    assert.match(text,/Execution: Idle/);assert.match(text,/Current action\s*\n?\s*Unknown/);
    assert.match(await page.locator('.agent-missing').innerText(),/Unknown or not recorded: Last recorded result, Waiting on/);
    assert.equal(await page.locator('.agent-summary .agent-fact').count(),3);
    assert.equal(await page.locator('.agent-more').evaluate(n=>n.open),false);
    assert.equal(await detail.getByText('Runtime observed').isVisible(),false,'evidence stays in the collapsed disclosure');
    await page.click('.agent-more > summary');assert.equal(await detail.getByText('Runtime observed').isVisible(),true);
    // Tabs: memory loads read-only, once; the edited Profile draft survives switching.
    await page.fill('.agent-profile-content','Edited draft');
    await page.click('[data-profile-file="memory"]');
    await page.waitForFunction(()=>document.querySelector('.agent-profile-content')?.readOnly);
    assert.equal(await page.locator('.agent-profile-settings').getByRole('button',{name:'Save changes'}).count(),0);
    assert.equal(await page.locator('.agent-more').evaluate(n=>n.open),true,'disclosure state survives re-render');
    assert.equal(await page.evaluate(()=>document.activeElement?.dataset.profileFile),'memory','focus stays on the clicked tab');
    await page.click('[data-profile-file="PROFILE.md"]');
    assert.equal(await page.inputValue('.agent-profile-content'),'Edited draft');
    await page.click('[data-profile-file="memory"]');await page.click('[data-profile-file="PROFILE.md"]');
    assert.deepEqual(gets,['PROFILE.md','memory'],'each agent/file loads once');
    // A failed section is not retried in a loop; an explicit reload retries it.
    await page.click('[data-profile-file="SOUL.md"]');
    await page.waitForFunction(()=>/not installed/i.test(document.querySelector('.agent-profile-settings').textContent));
    await page.waitForTimeout(600);assert.deepEqual(gets.filter(f=>f==='SOUL.md'),['SOUL.md']);
    await page.getByRole('button',{name:'Reload from file'}).click();
    await page.waitForSelector('.agent-profile-content');assert.equal(gets.filter(f=>f==='SOUL.md').length,2);
    // Dirty reload needs an explicit discard; Keep editing changes nothing.
    await page.click('[data-profile-file="PROFILE.md"]');
    const before=gets.length;
    await page.getByRole('button',{name:'Reload from file'}).click();
    assert.match(await page.locator('.agent-reload-confirm').innerText(),/unsaved edits/);
    assert.equal(gets.length,before);
    await page.getByRole('button',{name:'Keep editing'}).click();
    assert.equal(await page.inputValue('.agent-profile-content'),'Edited draft');assert.equal(gets.length,before);
    await page.getByRole('button',{name:'Reload from file'}).click();
    await page.getByRole('button',{name:'Discard edits and reload'}).click();
    await page.waitForFunction(()=>(document.querySelector('.agent-profile-content')?.value||'').startsWith('# '));
    assert.equal(gets.length,before+1);
    // Saving still works and a clean draft reloads directly.
    await page.fill('.agent-profile-content','Saved text');
    await page.getByRole('button',{name:'Save changes'}).click();
    await page.waitForFunction(()=>document.querySelector('.agent-profile-settings').textContent.includes('Saved. Applies'));
    assert.equal(posts.length,1);
    // Editing right after a render (no tab switch) must still ask before Reload replaces the draft.
    const n=gets.length;
    await page.fill('.agent-profile-content','Direct edit');
    await page.getByRole('button',{name:'Reload from file'}).click();
    assert.match(await page.locator('.agent-reload-confirm').innerText(),/unsaved edits/);
    assert.equal(gets.length,n,'no request before the discard is confirmed');
    assert.equal(await page.inputValue('.agent-profile-content'),'Direct edit');
    await page.getByRole('button',{name:'Keep editing'}).click();
    assert.equal(gets.length,n);assert.equal(await page.inputValue('.agent-profile-content'),'Direct edit');
    await page.fill('.agent-profile-content','Direct edit 2');
    await page.getByRole('button',{name:'Reload from file'}).click();
    await page.getByRole('button',{name:'Discard edits and reload'}).click();
    await page.waitForFunction(()=>(document.querySelector('.agent-profile-content')?.value||'').startsWith('# '));
    assert.equal(gets.length,n+1);
    for(const mode of ['light','dark']){await page.click('[data-appearance="'+mode+'"]');await page.waitForTimeout(600);await page.screenshot({path:path.join(out,'detail-after-'+mode+'.png'),fullPage:true});}
    assert.deepEqual(errors,[]);
    console.log('PASS: compact summary '+JSON.stringify(metrics)+', auto-load once per agent/file, drafts, discard prompt, retry, disclosure state');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
