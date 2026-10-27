// #352 summary: generated count summaries are translated EN/BG by their exact grammar; authored text stays verbatim.
// Isolated HTTP responses; never connects to Office or Codex.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(process.argv[3]||path.resolve(__dirname,'../scripts/team_room/static'));
const out=path.resolve(process.argv[2]||'output/cbm352-summary');fs.mkdirSync(out,{recursive:true});
const base='http://cbm352-summary-fixture.test';
const CASES=[
  ['p1','Manager report: 1 reported work item, 2 open issues, 1 blocker, 3 owner decisions.','\u041E\u0442\u0447\u0435\u0442 \u043D\u0430 \u041C\u0435\u043D\u0438\u0434\u0436\u044A\u0440\u0430: 1 \u043E\u0442\u0447\u0435\u0442\u0435\u043D\u0430 \u0437\u0430\u0434\u0430\u0447\u0430, 2 \u043E\u0442\u0432\u043E\u0440\u0435\u043D\u0438 issue-\u0442\u0430, 1 \u0431\u043B\u043E\u043A\u0435\u0440, 3 \u0440\u0435\u0448\u0435\u043D\u0438\u044F \u043D\u0430 \u0441\u043E\u0431\u0441\u0442\u0432\u0435\u043D\u0438\u043A\u0430.'],
  ['p2','Manager report: 4 reported work items.','\u041E\u0442\u0447\u0435\u0442 \u043D\u0430 \u041C\u0435\u043D\u0438\u0434\u0436\u044A\u0440\u0430: 4 \u043E\u0442\u0447\u0435\u0442\u0435\u043D\u0438 \u0437\u0430\u0434\u0430\u0447\u0438.'],
  ['p3','Manager report: 1 blocker, 1 owner decision.','\u041E\u0442\u0447\u0435\u0442 \u043D\u0430 \u041C\u0435\u043D\u0438\u0434\u0436\u044A\u0440\u0430: 1 \u0431\u043B\u043E\u043A\u0435\u0440, 1 \u0440\u0435\u0448\u0435\u043D\u0438\u0435 \u043D\u0430 \u0441\u043E\u0431\u0441\u0442\u0432\u0435\u043D\u0438\u043A\u0430.'],
  ['p4','Manager report: 1 open issue, 12 owner decisions.','\u041E\u0442\u0447\u0435\u0442 \u043D\u0430 \u041C\u0435\u043D\u0438\u0434\u0436\u044A\u0440\u0430: 1 \u043E\u0442\u0432\u043E\u0440\u0435\u043D issue, 12 \u0440\u0435\u0448\u0435\u043D\u0438\u044F \u043D\u0430 \u0441\u043E\u0431\u0441\u0442\u0432\u0435\u043D\u0438\u043A\u0430.'],
  ['p5','Manager report received; it lists no counts.','\u041F\u043E\u043B\u0443\u0447\u0435\u043D \u0435 \u043E\u0442\u0447\u0435\u0442 \u043D\u0430 \u041C\u0435\u043D\u0438\u0434\u0436\u044A\u0440\u0430; \u0442\u043E\u0439 \u043D\u0435 \u0441\u044A\u0434\u044A\u0440\u0436\u0430 \u0431\u0440\u043E\u0439\u043A\u0438.'],
  ['p6','Release 0.2.0 is waiting for QA; owner decision expected on Friday.',null],
  ['p7','Manager report: 2 blocker.',null],
  ['p8','Manager report: 1 blockers.',null],
  ['p9','Manager report: 1 blocker, 2 open issues.',null],
  ['p10','Manager report: 0 blockers.',null],
  ['p11','Manager report: 02 blockers.',null],
  ['p12','Manager report: 3 blockers. Extra detail.',null],
  ['p13','manager report: 3 blockers.',null],
  ['p14','Manager report: 3 blockers, 3 blockers.',null],
];
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const now=new Date().toISOString();
    const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
    page.on('pageerror',e=>errors.push(e.stack));
    await page.addInitScript(()=>{window.EventSource=class {addEventListener(){}close(){}};});
    const projects=CASES.map(([id,summary])=>({id,name:'Project '+id,status:'working',summary,manager:'M',open_tasks:1,source:{kind:'local',label:'Manager Report',available:true,as_of:now},reports:[],tasks:[],decisions:[]}));
    await page.route(base+'/**',async route=>{
      const url=new URL(route.request().url());let data;
      if(url.pathname==='/api/owner-inbox')data={schema:1,observed_at:now,items:[],complete:true,warnings:[],coverage:'Fixture'};
      else if(url.pathname==='/api/projects')data={available:true,snapshot:{generated_at:now,projects}};
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
    const read=async()=>{const o={};for(const [id] of CASES){const n=page.locator('#pc-card-'+id+' .pc-summary');o[id]={text:(await n.textContent()).trim(),title:await n.getAttribute('title')};}return o;};
    await page.goto(base);await page.click('#projects-tab');await page.waitForSelector('#pc-card-p14');
    let got=await read();
    for(const [id,original] of CASES){assert.equal(got[id].text,original,'English keeps the backend text '+id);assert.equal(got[id].title,original);}
    await page.click('#office-settings');await page.selectOption('#ui-language','bg');await page.click('#projects-tab');await page.waitForSelector('#pc-card-p14');
    got=await read();
    for(const [id,original,bg] of CASES){assert.equal(got[id].text,bg||original,'Bulgarian '+id);assert.equal(got[id].title,bg||original,'tooltip '+id);}
    // The project page uses the same text.
    await page.click('#pc-card-p1 .pc-card-head');await page.waitForSelector('.pc-summary');
    assert.equal((await page.locator('.pc-summary').first().textContent()).trim(),CASES[0][2]);
    await page.screenshot({path:path.join(out,'summary-bg.png'),fullPage:true});
    await page.click('#office-settings');await page.selectOption('#ui-language','en');await page.click('#projects-tab');await page.waitForSelector('#pc-card-p1');
    got=await read();assert.equal(got.p1.text,CASES[0][1],'switching back restores English');
    assert.deepEqual(errors,[]);
    console.log('PASS: generated summaries translated EN/BG by exact grammar ('+CASES.filter(c=>c[2]).length+'), '+CASES.filter(c=>!c[2]).length+' authored or near-miss texts verbatim');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
