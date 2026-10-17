'use strict';

// Playwright is CI tooling supplied through NODE_PATH, not an app dependency.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');

const origin = new URL(process.env.SMOKE_PUBLIC_ORIGIN || 'https://127.0.0.1:18443');
const upstream = new URL(process.env.SMOKE_UPSTREAM || 'http://127.0.0.1:18001');
const outputDir = process.env.SMOKE_OUTPUT_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'omnivoice-browser-'));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;
let page;
const sockets = new Set();
const failures = [];
const themes = [];
const queueStreams = [];

const proxy = https.createServer({
  cert: fs.readFileSync(process.env.SMOKE_CERT_FILE),
  key: fs.readFileSync(process.env.SMOKE_KEY_FILE),
}, (req, res) => {
  const outgoing = http.request(new URL(req.url, upstream), {
    method: req.method,
    headers: {
      ...req.headers,
      host: '100.65.14.203',
      'x-forwarded-host': '100.65.14.203',
      'x-forwarded-proto': 'http',
    },
  }, (incoming) => {
    res.writeHead(incoming.statusCode, incoming.headers);
    res.flushHeaders();
    incoming.pipe(res);
    incoming.on('error', () => res.destroy());
  });
  outgoing.setTimeout(45000, () => outgoing.destroy(new Error('Upstream timeout')));
  outgoing.on('error', (error) => {
    if (!res.headersSent) res.writeHead(502);
    res.end(error.message);
  });
  req.on('aborted', () => outgoing.destroy());
  res.on('close', () => outgoing.destroy());
  req.pipe(outgoing);
});
proxy.requestTimeout = 60000;
proxy.headersTimeout = 15000;
proxy.on('connection', (socket) => {
  sockets.add(socket);
  socket.on('close', () => sockets.delete(socket));
});

function getConfig() {
  return new Promise((resolve, reject) => {
    const req = https.get(new URL('/config', origin), { rejectUnauthorized: false }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('error', reject);
      res.on('end', () => {
        try {
          assert.equal(res.statusCode, 200);
          resolve(JSON.parse(body));
        } catch (error) { reject(error); }
      });
    });
    req.setTimeout(3000, () => req.destroy(new Error('Readiness timeout')));
    req.on('error', reject);
  });
}

function isPrivateHost(host) {
  return host === 'localhost' || host.endsWith('.localhost') || host === '[::1]'
    || /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|0\.0\.0\.0$)/.test(host);
}

async function checkViewport(name, viewport) {
  await page.setViewportSize(viewport);
  for (const tab of ['Voice Clone', 'Voice Design']) {
    await page.getByRole('tab', { name: tab, exact: true }).click();
    await page.evaluate(() => document.fonts.ready);
    const dimensions = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
    }));
    assert.ok(dimensions.content <= dimensions.viewport + 1,
      `${name} ${tab} horizontal overflow: ${JSON.stringify(dimensions)}`);
    await page.screenshot({
      path: path.join(outputDir, `${name}-${tab.toLowerCase().replaceAll(' ', '-')}.png`),
      fullPage: true,
    });
  }
}

async function run() {
  fs.mkdirSync(outputDir, { recursive: true });
  await new Promise((resolve, reject) => {
    proxy.once('error', reject);
    proxy.listen(Number(origin.port || 443), origin.hostname, resolve);
  });
  const readyBy = Date.now() + 100000;
  let config;
  while (Date.now() < readyBy) {
    try { config = await getConfig(); break; } catch { await sleep(1000); }
  }
  assert.ok(config, 'Real UI did not become ready within 100 seconds');
  assert.equal(config.root, origin.origin, 'Gradio must advertise the public HTTPS origin');

  browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || undefined,
    timeout: 30000,
  });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 1000 } });
  context.setDefaultTimeout(20000);
  page = await context.newPage();
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin.origin && isPrivateHost(url.hostname)) {
      failures.push(`Private-host request: ${request.url()}`);
    }
    if (url.pathname.endsWith('/queue/data')) queueStreams.push({ url: request.url(), status: null });
  });
  page.on('requestfailed', (request) => {
    const error = request.failure()?.errorText;
    // Gradio closes its event stream after the last job; that is not a failed asset.
    if (new URL(request.url()).pathname.endsWith('/queue/data') && error === 'net::ERR_ABORTED') return;
    failures.push(`Failed resource: ${request.url()} ${error}`);
  });
  page.on('pageerror', (error) => failures.push(`Page error: ${error.message}`));
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.endsWith('/theme.css')) themes.push({ origin: url.origin, status: response.status() });
    if (url.pathname.endsWith('/queue/data')) {
      queueStreams.push({ url: response.url(), status: response.status() });
    }
    if (response.status() >= 400) failures.push(`HTTP ${response.status()}: ${response.url()}`);
  });
  await page.goto(origin.origin, { waitUntil: 'load', timeout: 30000 });
  await page.locator('.gradio-container').waitFor({ state: 'visible' });
  await page.getByRole('tab', { name: 'Voice Clone', exact: true }).waitFor();
  await page.waitForFunction(() => {
    const container = document.querySelector('.gradio-container');
    return container && getComputedStyle(container).getPropertyValue('--body-text-color').trim() !== '';
  });
  assert.ok(themes.some((theme) => theme.origin === origin.origin && theme.status === 200),
    `Public theme.css did not load successfully: ${JSON.stringify(themes)}`);

  for (const tab of ['Voice Clone', 'Voice Design']) {
    // A fresh client session makes each mode prove its own event stream.
    await page.reload({ waitUntil: 'load' });
    await page.getByRole('tab', { name: tab, exact: true }).click();
    await page.getByRole('textbox', { name: /Text to Synthesize/ }).filter({ visible: true }).fill('');
    const streamStart = queueStreams.length;
    const submission = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith('/queue/join')
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Generate / 生成', exact: true }).filter({ visible: true }).click();
    const joined = await submission;
    assert.equal(joined.status(), 200, `${tab} public queue submission failed`);
    assert.equal(new URL(joined.url()).origin, origin.origin);
    const streamBy = Date.now() + 20000;
    while (Date.now() < streamBy && !queueStreams.slice(streamStart).some((stream) => stream.status === 200)) await sleep(200);
    assert.ok(queueStreams.slice(streamStart).some((stream) => stream.status === 200
      && new URL(stream.url).origin === origin.origin), `${tab} public queue event stream failed`);
    await page.screenshot({ path: path.join(outputDir, `${tab.toLowerCase().replaceAll(' ', '-')}-validation.png`), fullPage: true });
  }
  await checkViewport('desktop', { width: 1440, height: 1000 });
  await checkViewport('mobile', { width: 390, height: 844 });
  assert.deepEqual(failures, [], 'Browser resources must load without private-host URLs or failures');
  console.log(`PASS: public HTTPS assets, clone/design queue transport, desktop/mobile layout. Screenshots: ${outputDir}`);
}

(async () => {
  let deadline;
  try {
    await Promise.race([
      run(),
      new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('Browser smoke exceeded 240 seconds')), 240000); }),
    ]);
  } catch (error) {
    console.error(error);
    console.error(failures);
    if (page && !page.isClosed()) {
      await page.screenshot({ path: path.join(outputDir, 'failure.png'), fullPage: true, timeout: 5000 }).catch(() => {});
    }
    process.exitCode = 1;
  } finally {
    clearTimeout(deadline);
    if (browser) await browser.close();
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => proxy.close(resolve));
  }
})();
