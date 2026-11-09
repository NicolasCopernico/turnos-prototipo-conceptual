// Utilidades del recorrido en navegador real (puppeteer-core + Chrome). Ver tests/test_e2e_navegador.py.
const puppeteer = require('puppeteer-core');

const PORT = process.env.E2E_PORT || '8766';
const BASE = `http://localhost:${PORT}`;
const API_FRONTEND = 'http://localhost:8012'; // la URL que trae escrita prototipo-turnos.html (API_URL)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CHROME = process.env.CHROME_PATH || ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find((r) => require('fs').existsSync(r));

const lanzar = () => puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });

// Abre el frontend servido por el backend de prueba. Las llamadas a API_URL (8012) se redirigen al puerto de prueba
// a nivel de red, así el navegador ejecuta el HTML real (con su CORS real) sin tocar el servidor de desarrollo.
async function abrir(browser, { skewMs = 0 } = {}) {
  const p = await browser.newPage();
  await p.setViewport({ width: 1280, height: 1000 });
  p.errs = []; p.reqs = [];
  p.on('pageerror', (e) => p.errs.push('pageerror: ' + e.message));
  p.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) p.errs.push('console: ' + m.text()); });
  if (skewMs) await p.evaluateOnNewDocument((s) => { const real = Date.now.bind(Date); Date.now = () => real() + s; }, skewMs);
  await p.setRequestInterception(true);
  p.on('request', (r) => {
    const u = r.url();
    if (!u.startsWith(API_FRONTEND)) return r.continue();
    p.reqs.push(r.method() + ' ' + u.slice(API_FRONTEND.length));
    if (p.bloquearApi) r.abort('connectionrefused'); else r.continue({ url: BASE + u.slice(API_FRONTEND.length) });
  });
  await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await p.waitForFunction(() => document.body.innerText.includes('Elegí un servicio'), { timeout: 90000 });
  await sleep(1500);
  return p;
}

const texto = (p) => p.evaluate(() => document.body.innerText);

async function clickTexto(p, re) {
  const ok = await p.evaluate((src) => {
    const rx = new RegExp(src);
    const b = [...document.querySelectorAll('button')].find((x) => rx.test(x.innerText.replace(/\s+/g, ' ').trim()) && !x.disabled);
    if (!b) return false; b.click(); return true;
  }, re.source);
  if (!ok) throw new Error('No hay botón habilitado con texto ' + re);
  await sleep(600);
}

// Próximo día hábil (lunes a viernes) posterior a hoy, en formato yyyy-mm-dd (hora local del navegador).
const proximoDiaHabil = (p) => p.evaluate(() => {
  const d = new Date(Date.now());
  do { d.setDate(d.getDate() + 1); } while ([0, 6].includes(d.getDay()));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
});

async function elegirFecha(p, ymd) {
  await p.evaluate((v) => {
    const el = document.querySelector('input[type=date]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
  }, ymd);
  await sleep(600);
}

const slots = (p) => p.evaluate(() => [...document.querySelectorAll('button')].map((b) => b.innerText.trim()).filter((t) => /^\d{2}:\d{2}$/.test(t)));

async function esperarTexto(p, re, ms = 15000) {
  try { await p.waitForFunction((src) => new RegExp(src).test(document.body.innerText), { timeout: ms }, re.source); return true; } catch { return false; }
}
async function esperarApi(fn, ms = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await sleep(300); }
  return false;
}
const api = async (path) => { const r = await fetch(BASE + path); return { status: r.status, body: await r.json() }; };

module.exports = { lanzar, abrir, texto, clickTexto, proximoDiaHabil, elegirFecha, slots, esperarTexto, esperarApi, api, sleep };
