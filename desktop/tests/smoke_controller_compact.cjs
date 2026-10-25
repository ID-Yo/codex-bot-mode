// Browser-only Tauri mock: the compact controller in every state at its default, minimum and maximized sizes (#245).
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const SIZES = { default: [480, 340], minimum: [420, 300], maximized: [1920, 1040] };
const LOGS = Array.from({ length: 12 }, (_, n) => `Activity ${n}: ` + 'diagnostic text '.repeat(6));
const PROJECT = 'D:/Projects/' + 'long-project-path/'.repeat(8);
const CODEX = 'C:/Runtime/' + 'long-runtime-path/'.repeat(6) + 'codex.exe';
const STATES = {
  notStarted: { status: { state: 'notStarted', canStart: true, reason: 'The previous Office service is no longer running; its stale descriptor was archived.' }, label: 'Not started', fits: true },
  starting: { status: { state: 'starting', reason: 'Waiting for authenticated service identity.' }, label: 'Starting', fits: true },
  running: { status: { state: 'running', safety: 'safe', canStop: true, canRestart: true, canOpen: true }, label: 'Running', fits: true },
  busy: { status: { state: 'running', safety: 'busy', busyCount: 2, pendingCount: 3, canStop: true, canRestart: true, canOpen: true }, label: 'Running', fits: true },
  unverified: { status: { state: 'unverified', canRecover: true, canPreserve: true,
    reason: 'The authenticated Office listener did not respond. The exact owned child remains available for recheck.' }, label: 'Unverified', fits: true },
  preserved: { status: { state: 'preserved', canClearPreserved: true, canQuitPreserved: true,
    reason: 'Unverified Office child PID 52692 was left untouched. Verify it has stopped before clearing the safety lock.' }, label: 'Child preserved', fits: false },
  error: { status: { state: 'notStarted', canStart: true, reason: 'No managed Office service has been started for this project.' }, label: 'Not started', message: 'Bundled Office service could not start: The system cannot find the file specified. (os error 2)', fits: true }
};

(async () => {
  const root = path.resolve(__dirname, '../ui');
  const evidence = path.resolve(process.argv[2] || 'output/controller-compact');
  fs.mkdirSync(evidence, { recursive: true });
  const server = http.createServer((req, res) => {
    const name = req.url === '/' ? 'index.html' : req.url.slice(1);
    if (!['index.html', 'style.css', 'app.js'].includes(name)) { res.writeHead(404); return res.end(); }
    res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const problems = [];
  try {
    for (const [stateName, state] of Object.entries(STATES)) for (const [sizeName, [width, height]] of Object.entries(SIZES)) {
      const page = await browser.newPage({ viewport: { width, height } });
      await page.addInitScript(([view, message]) => {
        window.setInterval = () => 1;
        window.__TAURI__ = { core: { invoke: async name => {
          if (name === 'get_view') return structuredClone(view);
          if (name === 'start_service') throw message;
        } }, dialog: { ask: async () => true }, event: { listen: () => {} } };
      }, [{ projectPath: PROJECT, codexPath: CODEX, logs: LOGS, status: state.status }, state.message || '']);
      await page.goto(`http://127.0.0.1:${server.address().port}/`);
      await page.waitForFunction(label => document.querySelector('#status-label').textContent === label, state.label);
      if (state.message) await page.click('#start');
      if (state.message) await page.waitForFunction(() => document.querySelector('#action-message').textContent.length > 0);
      const tag = `${stateName}-${sizeName}`;
      const info = await page.evaluate(() => {
        const visible = [...document.querySelectorAll('.shell *')].filter(el => el.offsetParent !== null || el.closest('details:not([open])') === null && getComputedStyle(el).position === 'fixed');
        // Text that is cut without an ellipsis: buttons, labels and paragraphs must show all of their text.
        const clipped = visible.filter(el => el.matches('button, .status-badge, .setup-label, .status-detail, .action-message, h1, .version, .logs-title, .footer span')
          && (el.scrollWidth > el.clientWidth + 1)).map(el => el.id || el.className || el.tagName);
        const outside = visible.filter(el => { const b = el.getBoundingClientRect(); return el.matches('button, .status-badge, .path-value') && (b.right > innerWidth + 0.5 || b.left < -0.5); }).map(el => el.id || el.className);
        return { clipped, outside, hScroll: document.documentElement.scrollWidth > innerWidth,
          needed: Math.ceil(document.querySelector('.footer').getBoundingClientRect().bottom + 8),
          setup: [...document.querySelectorAll('.path-value')].map(el => el.scrollWidth > el.clientWidth ? 'ellipsis' : 'full') };
      });
      if (info.clipped.length || info.outside.length || info.hScroll) problems.push({ tag, ...info });
      if (sizeName !== 'minimum' && state.fits && info.needed > height) problems.push({ tag, note: 'needs scrolling at this size', needed: info.needed, height });
      if (sizeName !== 'maximized') console.log(tag.padEnd(24), 'needs', info.needed, 'px');
      await page.screenshot({ path: path.join(evidence, tag + '.png') });
      await page.close();
    }
    assert.deepEqual(problems, []);
    console.log('PASS: compact controller states at default, minimum and maximized sizes; screenshots in ' + evidence);
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
