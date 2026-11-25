// node ui.mjs <agent> [prefix…]: start the agent, open its UI in headless Chrome and click through the screens before sign-in.
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [exe, ...prefix] = process.argv.slice(2);
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9334;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = 'shots'; mkdirSync(out, { recursive: true });
const tag = prefix.length ? 'rosetta' : 'native';

const agent = spawn(prefix[0] ?? exe, [...prefix.slice(1), ...(prefix.length ? [exe] : []), '--no-dev-tunnel', '--no-browser', '--port', '0'],
  { stdio: ['ignore', 'pipe', 'pipe'], detached: true });
let log = ''; agent.stdout.on('data', (d) => (log += d)); agent.stderr.on('data', (d) => (log += d));
let chrome; let failed = 0;
const check = (name, ok, detail = '') => { console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ': ' + detail : ''}`); if (!ok) failed += 1; };

try {
  let url;
  for (let i = 0; i < 90 && !url; i += 1) { url = log.match(/http:\/\/127\.0\.0\.1:\d+\/\?launch_token=[\w-]+/)?.[0]; if (!url) await sleep(1000); }
  if (!url) throw new Error('no launch url: ' + log.slice(-500));
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), 'chrome-'))}`,
    '--window-size=1400,1000', url], { stdio: 'ignore' });
  let page;
  for (let i = 0; i < 60 && !page; i += 1) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((x) => x.type === 'page'); } catch { /* not up yet */ }
    if (!page) await sleep(1000);
  }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0; const waiting = new Map();
  ws.onmessage = (m) => { const d = JSON.parse(m.data); waiting.get(d.id)?.(d.result); };
  const cdp = (method, params = {}) => new Promise((r) => { id += 1; waiting.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
  const ev = async (expression) => (await cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result?.value;
  const shot = async (name) => writeFileSync(join(out, `${tag}-${name}.png`), Buffer.from((await cdp('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  const state = () => ev(`({path: location.pathname, title: document.title, text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 200),
    buttons: [...document.querySelectorAll('button')].map((b) => b.innerText.trim()).filter(Boolean),
    fields: [...document.querySelectorAll('input')].map((i) => i.name)})`);
  const until = async (pred) => { let s; for (let i = 0; i < 30; i += 1) { s = await state(); if (s && pred(s)) return s; await sleep(1000); } return s; };
  const click = (text) => ev(`[...document.querySelectorAll('button')].find((b) => b.innerText.trim() === ${JSON.stringify(text)}).click()`);

  let s = await until((x) => x.fields.includes('username') && x.path === '/auth/login');
  check('login screen', s.path === '/auth/login' && s.fields.join() === 'username,password' && s.buttons.includes('Создать аккаунт'), JSON.stringify(s));
  await shot('1-login');

  await click('Забыли пароль?');
  s = await until((x) => x.text.includes('Вспоминаете'));
  check('forgot password screen', s.text.includes('Вспоминаете') && s.buttons.includes('Войти'), s.text);
  await shot('2-forgot');

  await click('Создать аккаунт');
  s = await until((x) => x.path === '/auth/register');
  check('register screen', s.path === '/auth/register' && s.fields.includes('username'), s.path);
  await shot('3-register');

  await click('Войти');
  s = await until((x) => x.path === '/auth/login' && x.buttons.includes('Забыли пароль?'));
  await ev(`(() => { const set = (n, v) => { const e = document.querySelector('input[name=' + n + ']');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, v); e.dispatchEvent(new Event('input', { bubbles: true })); };
    set('username', 'arm64-smoke-no-such-user'); set('password', 'wrong-password-1'); document.querySelector('button[type=submit]').click(); })()`);
  s = await until((x) => x.text.includes('Неверный логин'));
  check('gateway rejects a wrong password', s.text.includes('Неверный логин'), s.text);
  await shot('4-wrong-password');
  ws.close();
} catch (e) {
  check('run', false, String(e));
} finally {
  chrome?.kill(); try { process.kill(-agent.pid); } catch { /* gone */ }
}
process.exit(failed ? 1 : 0);
