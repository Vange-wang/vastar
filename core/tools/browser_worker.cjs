// Isolated single adapter. Anonymous context. Every resource is brokered or denied.
process.stdout.write('{"type":"boot","stage":"node"}\n');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const work = __dirname;
const scratch = path.resolve(work, '../Temp/browser');
process.env.TEMP = process.env.TMP = scratch;
const { chromium } = require('./playwright');
process.stdout.write('{"type":"boot","stage":"adapter_loaded"}\n');
const { version, capture } = require('./browser_capture.cjs');
const { install } = require('./browser_spawn.cjs');
const { action, verify } = require('./browser_action.cjs');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const emit = data => process.stdout.write(JSON.stringify(data) + '\n');
let networkID = 0;

async function readFile(name, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (fs.existsSync(name)) return JSON.parse(fs.readFileSync(name, 'utf8'));
    await sleep(10);
  }
  throw new Error('timeout');
}

async function executeCommand(context, tabs, command, problems, events) {
  let page = command.tab_id ? tabs.get(command.tab_id) : null;
  if (command.tab_id && (!page || page.isClosed())) throw new Error('tab_closed');
  if (command.op === 'navigate') {
    if (!page) page = await context.newPage();
    await page.goto(command.url, { waitUntil: 'domcontentloaded', timeout: 10000 });
    await page.waitForTimeout(60);
  } else if (!page) throw new Error('tab_closed');
  if (problems.length) throw new Error(problems[0]);
  const tab_id = [...tabs].find(([, p]) => p === page)[0];
  if (command.op === 'action') {
    await action(page, command, emit, () => readFile(path.join(work, 'action-gate-' + command.id + '.json'), 10000));
  } else if (command.op === 'verify_action') {
    return { proof: await verify(page, command), tab_id, url: page.url() };
  } else {
    const rev = await version(page);
    if (command.page_revision && command.page_revision !== rev) throw new Error('stale_page_revision');
  }
  const value = command.op === 'version' ? { page_revision: await version(page) } : await capture(page);
  return { ...value, tab_id, url: page.url(), load_status: 'domcontentloaded', events,
    tabs: [...tabs].map(([id,p]) => ({ tab_id: id, closed: p.isClosed() })) };
}

async function run() {
  emit({ type: 'boot', stage: 'launch' });
  await install(path.join(work, 'engine/chrome-headless-shell.exe'));
  const browser = await chromium.launch({ executablePath: path.join(work, 'engine/chrome-headless-shell.exe'),
    headless: true, chromiumSandbox: false, downloadsPath: scratch, timeout: 12000,
    args: ['--disable-gpu', '--disable-background-networking', '--no-proxy-server',
      '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-extensions', '--disable-sync'],
    env: { ...process.env, TEMP: scratch, TMP: scratch } });
  const context = await browser.newContext({ acceptDownloads: false, serviceWorkers: 'block',
    viewport: { width: 1000, height: 700 }, permissions: [], bypassCSP: false });
  emit({ type: 'boot', stage: 'context_created' });
  await context.routeWebSocket('**/*', ws => ws.close());
  const tabs = new Map();
  let problems = [], events = [], active = null;
  context.on('page', page => {
    const id = 'tab-' + crypto.randomUUID();
    tabs.set(id, page);
    events.push({ event: 'new_tab', tab_id: id });
    page.on('dialog', async dialog => { events.push({ event: 'dialog_dismissed', tab_id: id }); await dialog.dismiss(); });
    page.on('download', async download => { problems.push('download_blocked'); await download.cancel(); });
    page.on('close', () => events.push({ event: 'tab_closed', tab_id: id }));
  });
  await context.route('**/*', async route => {
    const request = route.request(), key = String(++networkID);
    if ((request.method() !== 'GET' && active?.op !== 'action') || !/^https?:\/\//.test(request.url())) {
      problems.push('request_denied'); return route.abort();
    }
    // Request headers, cookies and storage are intentionally never transmitted.
    emit({ type: 'network', key, url: request.url(), method: request.method(),
      body: active?.op === 'action' ? (request.postDataBuffer() || Buffer.alloc(0)).toString('base64') : '',
      content_type: active?.op === 'action' ? await request.headerValue('content-type') || '' : '' });
    try {
      const response = await readFile(path.join(work, 'network-' + key + '.json'), 10000);
      if (response.error) { problems.push(response.error); return route.abort(); }
      if (response.location) {
        // Playwright does not route a request again after HTTP redirect fulfillment.
        // Main navigation is resolved by B29 before goto. Other redirects fail closed.
        problems.push('redirected_resource_unavailable');
        return route.abort();
      }
      await route.fulfill({ status: response.status || 200,
        headers: response.location ? { location: response.location } : { 'content-type': response.content_type },
        body: Buffer.from(response.body || '', 'base64') });
    } catch { problems.push('timeout'); await route.abort().catch(() => {}); }
  });
  emit({ type: 'ready', engine_version: browser.version() });
  for (let sequence = 1; ; sequence++) {
    const command = await readFile(path.join(work, 'command-' + sequence + '.json'), 120000);
    active = command;
    problems = []; events = [];
    try {
      emit({ type: 'result', id: command.id, value: await executeCommand(context, tabs, command, problems, events) });
    } catch (error) {
      const codes = ['tab_closed', 'stale_page_revision', 'page_changed_during_capture', 'frame_limit',
        'screenshot_too_large', 'page_not_observable', 'timeout', 'origin_changed', 'target_changed',
        'target_not_actionable', 'target_frame_changed', 'business_state_preexisting', 'authorization_revoked'];
      emit({ type: 'result', id: command.id, error: problems[0] || (codes.includes(error.message) ? error.message : 'load_failed') });
    } finally { active = null; }
  }
}

run().catch(() => { emit({ type: 'fatal', error: 'isolated_browser_unavailable' }); process.exitCode = 1; });
