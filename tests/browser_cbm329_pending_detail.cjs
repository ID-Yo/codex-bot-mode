// Isolated browser fixture (#329). Never connects to the installed Office or Codex.
// A pending roster card opened as Agent detail must refresh to the fresh record without a manual reload.
const {chromium}=require('playwright');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../scripts/team_room/static');
const base='http://office-fixture.test';
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:985,height:700}}),errors=[];
    const now=()=>new Date().toISOString();
    let agentRequests=0,fresh=false;
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(()=>{window.EventSource=class {addEventListener(){}close(){}};});
    const card=()=>({bot_key:'alpha:developer',bot_id:'developer',name:'Fixture Developer',role:'Developer',project_id:'alpha',project_name:'Alpha',team_name:'Development',
      execution:fresh?{state:'working',observed_at:now(),source_at:now(),source:'Desktop activity',freshness:'fresh',certainty:'confirmed'}:{state:'unknown',observed_at:now(),source_at:null,source:null},
      runtime:fresh?{freshness:'fresh',source_at:now(),state:'active'}:undefined,task:null,dependencies:[],transitions:[],attention_reasons:[]});
    await page.route(base+'/**',async route=>{
      const url=new URL(route.request().url());let data;
      if(url.pathname==='/api/registry/agents'){agentRequests++;data={agents:[card()],enrichment:fresh?'fresh':'pending',source_warnings:fresh?[]:['Desktop evidence is still loading; execution is Unknown until it arrives.']};}
      else if(url.pathname==='/api/registry/overview')data={projects:[],bots:[],teams:[]};
      else if(url.pathname==='/api/projects')data={available:true,snapshot:{generated_at:now(),projects:[]}};
      else if(url.pathname==='/api/owner-inbox')data={schema:1,observed_at:now(),complete:true,warnings:[],coverage:'',items:[]};
      else if(url.pathname==='/api/bots')data={bots:[],lead:null};
      else if(url.pathname==='/api/rooms')data={rooms:[]};
      else if(url.pathname.startsWith('/api/settings/bots/'))data={bot:'alpha:developer',file:'PROFILE.md',content:'Original profile',version:'version-1',readonly:false};
      else if(url.pathname.startsWith('/api/'))data={};
      if(data!==undefined)return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      const file=path.join(root,url.pathname==='/'?'index.html':url.pathname.slice(1));
      if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:''});
      const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'};
      return route.fulfill({contentType:types[path.extname(file)],body:fs.readFileSync(file,'utf8').replace('__ROOM_TOKEN__','fixture-only')});
    });
    await page.goto(base);await page.click('#bots-tab');await page.waitForSelector('.agent-card');
    assert.match(await page.locator('#agent-view-status').innerText(),/Loading Desktop evidence/);
    await page.locator('.agent-card-open').first().click();await page.waitForSelector('#agent-detail:not([hidden])');
    assert.equal(await page.locator('#agents-view').isHidden(),true,'detail hides the Agents view');
    assert.match(await page.locator('#agent-detail').innerText(),/Execution: Unknown/);
    await page.waitForSelector('.agent-profile-content');
    await page.fill('.agent-profile-content','Unsaved owner draft');
    await page.locator('.agent-profile-content').focus();
    await page.locator('.agent-profile-content').evaluate(n=>n.setSelectionRange(3,7,'backward'));
    const before=agentRequests;fresh=true;
    await page.waitForFunction(()=>/Execution: Working/.test(document.querySelector('#agent-detail').innerText),null,{timeout:9000});
    assert.ok(agentRequests>before,'the retry must continue while the detail is open');
    assert.equal(await page.locator('#agent-detail').isVisible(),true);
    const editor=await page.evaluate(()=>{const f=document.querySelector('.agent-profile-content');return {draft:f.value,focused:document.activeElement===f,start:f.selectionStart,end:f.selectionEnd,direction:f.selectionDirection};});
    assert.deepEqual(editor,{draft:'Unsaved owner draft',focused:true,start:3,end:7,direction:'backward'},'the editor keeps its draft, focus and selection');
    assert.deepEqual(errors,[]);
    console.log('PASS pending detail refreshed to fresh with '+(agentRequests-before)+' new request(s)');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});

