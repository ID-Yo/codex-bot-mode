// Browser-only Tauri mock. No service, native IPC or Codex calls.
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '../ui');
  const evidence = path.resolve(process.argv[2] || 'output/controller-ui');
  fs.mkdirSync(evidence, { recursive: true });
  const server = http.createServer((req, res) => {
    const name = req.url === '/' ? 'index.html' : req.url.slice(1);
    if (!['index.html', 'style.css', 'app.js'].includes(name)) { res.writeHead(404); return res.end(); }
    res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 480, height: 340 } });
    await page.addInitScript(() => {
      const f = window.fixture = { calls: [], view: {
        projectPath: 'D:/Projects/' + 'long-project-path/'.repeat(18),
        codexPath: 'C:/Runtime/' + 'long-runtime-path/'.repeat(12) + 'codex.exe',
        logs: Array.from({ length: 12 }, (_, n) => `Activity ${n}: ` + 'long diagnostic text '.repeat(8)),
        status: { state: 'notStarted', canStart: true }
      } };
      window.setInterval = callback => { f.poll = callback; return 1; };
      window.__TAURI__ = {
        core: { invoke: async name => {
          f.calls.push(name);
          if (name === 'get_view') {
            if (f.holdRefresh) return new Promise(resolve => { f.resolveRefresh = resolve; });
            return structuredClone(f.view);
          }
          if (name === 'start_service' || name === 'recover_startup') {
            return new Promise((resolve, reject) => { f.resolve = resolve; f.reject = reject; });
          }
        } }, dialog: { ask: async () => true }, event: { listen: () => {} }
      };
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector('#status-label').textContent === 'Not started');
    await page.click('#start');
    assert.equal(await page.textContent('#status-label'), 'Starting');
    assert.equal(await page.isDisabled('#start'), true);
    assert.equal(await page.isDisabled('#choose-project'), true);
    await page.evaluate(() => document.querySelector('#start').click());
    assert.equal(await page.evaluate(() => fixture.calls.filter(x => x === 'start_service').length), 1);
    await page.screenshot({ path: path.join(evidence, 'wide-starting.png') });
    await page.evaluate(() => {
      fixture.view.status = { state: 'unverified', canRecover: true, canPreserve: true,
        reason: 'Startup timed out. The exact owned child remains available for recheck. ' + 'Details '.repeat(35) };
      fixture.reject('Startup timed out; retained for recheck.');
    });
    await page.waitForFunction(() => document.querySelector('#status-label').textContent === 'Unverified');
    assert.equal(await page.isVisible('#recover-startup'), true);
    assert.equal(await page.isDisabled('#start'), true);
    assert.match(await page.textContent('#action-message'), /timed out/);
    await page.screenshot({ path: path.join(evidence, 'wide-recovery.png') });
    await page.click('#recover-startup');
    assert.equal(await page.textContent('#status-label'), 'Starting');
    await page.evaluate(() => {
      fixture.view.status = { state: 'running', safety: 'unknown', canOpen: true };
      fixture.resolve(fixture.view);
    });
    await page.waitForFunction(() => document.querySelector('#status-label').textContent === 'Running');
    assert.equal(await page.isDisabled('#open-office'), false);
    assert.equal(await page.isDisabled('#stop'), true);
    assert.equal(await page.isDisabled('#restart'), true);
    for (const [width, height] of [[480, 340], [420, 300], [1280, 800]]) {
      await page.setViewportSize({ width, height });
      const boxes = await page.evaluate(() => {
        const rect = selector => { const b = document.querySelector(selector).getBoundingClientRect();
          return { x: b.x, y: b.y, right: b.right, bottom: b.bottom }; };
        return { status: rect('#status-detail'), actions: rect('.actions-panel'), setup: rect('.setup-panel'),
          logs: rect('.logs-panel'), overflow: document.documentElement.scrollWidth > innerWidth };
      });
      assert.equal(boxes.overflow, false);
      assert.ok(boxes.status.bottom <= boxes.actions.y && boxes.actions.bottom <= boxes.setup.y && boxes.setup.bottom <= boxes.logs.y, 'one compact column');
      await page.screenshot({ path: path.join(evidence, `${width}x${height}-unknown.png`) });
    }
    // An unresolved status read must not create overlapping polling requests.
    const count = await page.evaluate(() => {
      fixture.holdRefresh = true;
      fixture.poll(); fixture.poll(); fixture.poll();
      return fixture.calls.filter(x => x === 'get_view').length;
    });
    await page.evaluate(() => { fixture.poll(); fixture.poll(); });
    assert.equal(await page.evaluate(() => fixture.calls.filter(x => x === 'get_view').length), count);
    await page.evaluate(() => { fixture.holdRefresh = false; fixture.resolveRefresh(fixture.view); });
    console.log('PASS: immediate Starting, duplicate refusal, timeout/recheck, Unknown controls, non-overlap, compact layout');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
