'use strict';

const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');

const origin = new URL(process.env.SMOKE_PUBLIC_ORIGIN || 'https://127.0.0.1:18443');
const upstream = new URL(process.env.SMOKE_UPSTREAM || 'http://127.0.0.1:18001');
const outputDir = process.env.SMOKE_OUTPUT_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'omnivoice-studio-'));
const inference = process.env.SMOKE_INFERENCE === '1';
const sockets = new Set();
const failures = [];
let browser;
let page;
let expectedSubmissionFailures = 0;

const proxy = https.createServer({
  cert: fs.readFileSync(process.env.SMOKE_CERT_FILE),
  key: fs.readFileSync(process.env.SMOKE_KEY_FILE),
}, (req, res) => {
  const outgoing = http.request(new URL(req.url, upstream), {
    method: req.method,
    headers: { ...req.headers, host: '100.65.14.203', 'x-forwarded-host': '100.65.14.203', 'x-forwarded-proto': 'http' },
  }, (incoming) => {
    res.writeHead(incoming.statusCode, incoming.headers);
    incoming.pipe(res);
    incoming.on('error', () => res.destroy());
  });
  outgoing.setTimeout(45000, () => outgoing.destroy(new Error('Upstream timeout')));
  outgoing.on('error', (error) => { if (!res.headersSent) res.writeHead(502); res.end(error.message); });
  req.on('aborted', () => outgoing.destroy());
  res.on('close', () => outgoing.destroy());
  req.pipe(outgoing);
});
proxy.requestTimeout = 60000;
proxy.headersTimeout = 15000;
proxy.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });

function readConfig() {
  return new Promise((resolve, reject) => {
    const req = https.get(new URL('/api/config', origin), { rejectUnauthorized: false }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('error', reject);
      res.on('end', () => {
        try { assert.equal(res.statusCode, 200); resolve(JSON.parse(body)); } catch (error) { reject(error); }
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

function checkWav(filename, expectPause) {
  const data = fs.readFileSync(filename);
  assert.equal(data.toString('ascii', 0, 4), 'RIFF');
  assert.equal(data.toString('ascii', 8, 12), 'WAVE');
  let sampleRate, channels, bits, pcm;
  for (let offset = 12; offset + 8 <= data.length;) {
    const kind = data.toString('ascii', offset, offset + 4);
    const size = data.readUInt32LE(offset + 4);
    if (kind === 'fmt ') {
      assert.equal(data.readUInt16LE(offset + 8), 1, 'Expected PCM WAV');
      channels = data.readUInt16LE(offset + 10);
      sampleRate = data.readUInt32LE(offset + 12);
      bits = data.readUInt16LE(offset + 22);
    }
    if (kind === 'data') pcm = data.subarray(offset + 8, offset + 8 + size);
    offset += 8 + size + (size % 2);
  }
  assert.equal(sampleRate, 24000);
  assert.equal(channels, 1);
  assert.equal(bits, 16);
  assert.ok(pcm && pcm.length > sampleRate / 2, 'Audio is shorter than 0.25 seconds');
  let peak = 0, zeros = 0, longestZeros = 0;
  for (let i = 0; i < pcm.length; i += 2) {
    const value = pcm.readInt16LE(i);
    peak = Math.max(peak, Math.abs(value));
    zeros = value === 0 ? zeros + 1 : 0;
    longestZeros = Math.max(longestZeros, zeros);
  }
  assert.ok(peak > 0, 'Generated file is silent');
  if (expectPause) assert.ok(longestZeros >= sampleRate, 'Requested one-second pause is missing');
  console.log(JSON.stringify({ filename, sampleRate, duration: pcm.length / 2 / sampleRate, peak, longestZeroSeconds: longestZeros / sampleRate }));
}

async function generateAndDownload(name, expectPause = false) {
  await page.locator('#review').click();
  await page.locator('#plan').waitFor({ state: 'visible' });
  let job, replay;
  if (inference && name === 'voice-design') {
    let accepted;
    const submitted = new Promise((resolve) => { accepted = resolve; });
    await page.route('**/api/jobs', async (route) => {
      const response = await route.fetch();
      assert.equal(response.status(), 202);
      replay = {headers:route.request().headers(),body:route.request().postDataBuffer()};
      accepted(await response.json());
      expectedSubmissionFailures++;
      await route.abort('failed');
    }, {times:1});
    await page.locator('#generate').click();
    job = await submitted;
    await page.locator('#resume-job').waitFor({state:'visible'});
    await page.locator('#resume-job').click();
    await page.waitForFunction(() => !document.querySelector('#generation-status').classList.contains('error'), null, {timeout:20000});
  } else {
    const submitted = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/jobs' && response.request().method() === 'POST');
    await page.locator('#generate').click();
    const response = await submitted;
    assert.equal(response.status(), 202, await response.text());
    job = await response.json();
  }
  await page.waitForFunction(() => {
    const audio = document.querySelector('#output-audio');
    const status = document.querySelector('#generation-status');
    if (status?.classList.contains('error')) throw new Error(status.textContent);
    return audio && audio.readyState >= 1 && !audio.closest('[hidden]');
  }, null, { timeout: 600000 });
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#download-audio').click();
  const download = await downloadPromise;
  const filename = path.join(outputDir, `${name}.wav`);
  await download.saveAs(filename);
  checkWav(filename, expectPause);
  if (replay) {
    const response = await page.request.post(new URL('/api/jobs',origin).href, {
      data:replay.body,
      headers:{'content-type':replay.headers['content-type'],'X-OmniVoice-Request':'1','Idempotency-Key':job.id},
    });
    assert.equal(response.status(),202);
    const repeated = await response.json();
    assert.equal(repeated.id,job.id);
    assert.equal(repeated.status,'done','Replaying an accepted request started synthesis again');
  }
  await page.screenshot({ path: path.join(outputDir, `${name}.png`), fullPage: true });
}

async function run() {
  fs.mkdirSync(outputDir, { recursive: true });
  await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(Number(origin.port || 443), origin.hostname, resolve); });
  let config;
  const readyBy = Date.now() + 120000;
  while (Date.now() < readyBy) {
    try { config = await readConfig(); break; } catch { await new Promise((resolve) => setTimeout(resolve, 1000)); }
  }
  assert.ok(config, 'Studio HTTP app not ready');
  assert.equal(config.ready, inference, 'Smoke mode does not match actual model readiness');
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'], timeout: 30000 });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, permissions: ['microphone'], acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
  context.setDefaultTimeout(20000);
  page = await context.newPage();
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin.origin && isPrivateHost(url.hostname)) failures.push(`Private-host request: ${url}`);
  });
  page.on('pageerror', (error) => failures.push(`Page error: ${error.message}`));
  page.on('requestfailed', (request) => {
    const url = new URL(request.url());
    if (expectedSubmissionFailures && url.pathname === '/api/jobs' && request.failure()?.errorText === 'net::ERR_FAILED') {expectedSubmissionFailures--;return;}
    if (url.origin === origin.origin && request.failure()?.errorText !== 'net::ERR_ABORTED') failures.push(`Request failed: ${url} ${request.failure()?.errorText}`);
  });
  page.on('response', (response) => {
    if (new URL(response.url()).origin === origin.origin && response.status() >= 500) failures.push(`HTTP ${response.status()}: ${response.url()}`);
  });
  await page.goto(origin.origin, { waitUntil: 'load' });
  await page.waitForFunction((count) => document.querySelector('#language')?.options.length === count, config.languages.length);
  assert.equal(await page.locator('.phrase').count(),1,'A new script must start with one phrase');
  assert.equal(await page.locator('.phrase-delete').isEnabled(),false,'The final phrase must remain');
  assert.equal(await page.locator('[data-id="1"] [data-tag]').count(), 13);
  await page.locator('#phrase-1').fill('ก่อน หลัง');
  await page.locator('#phrase-1').evaluate((el) => el.setSelectionRange(4, el.value.length));
  await page.locator('[data-id="1"] .field-tools summary').click();
  await page.locator('[data-id="1"] [data-tag="[laughter]"]').click();
  assert.equal((await page.locator('#phrase-1').inputValue()).replace('[laughter]', '').replace(/\s+/g, ' ').trim(), 'ก่อน หลัง');
  await page.locator('#language-search').fill('ไทย');
  await page.locator('#language').selectOption('Thai');
  await page.locator('#add-block').click();
  await page.locator('.phrase textarea').last().fill('Second phrase to remove.');
  await page.locator('#language').selectOption('English');
  await page.locator('#add-block').click();
  await page.locator('.phrase textarea').last().fill('Third phrase to keep.');
  await page.locator('#language').selectOption('English');
  await page.locator('[data-id="2"] .phrase-delete').click();
  assert.equal(await page.locator('.phrase').count(),2);
  assert.equal(await page.locator('.phrase textarea').last().inputValue(),'Third phrase to keep.');
  assert.equal(await page.locator('#language').inputValue(),'English','Deleting another phrase lost selected settings');
  await page.locator('#phrase-1').focus();
  assert.equal(await page.locator('#language').inputValue(), 'Thai');
  await page.locator('#add-block').click();
  await page.locator('#review').click();
  assert.match(await page.locator('#review-error').innerText(), /ช่วงที่ 3/);
  await page.locator('.phrase-delete').last().click();
  await page.locator('#record-reference').click();
  await page.locator('#stop-recording').waitFor({ state: 'visible' });
  await page.locator('.voice-summary').click({force:true});
  assert.equal(await page.locator('#voice-settings').evaluate((el) => el.open),true,'Recording stop was hidden by collapsed settings');
  await page.waitForTimeout(1200);
  await page.locator('#stop-recording').click();
  await page.waitForFunction(() => document.querySelector('#reference-audio').readyState >= 1);
  await page.locator('#reference-text').fill('เสียงอ้างอิงสำหรับตรวจหน้าจอ');
  await page.locator('#review').click();
  await page.locator('#plan').waitFor({ state: 'visible' });
  await page.locator('#clear-file').click();
  await page.locator('#design-mode').click();
  await page.locator('#review').click();
  await page.locator('#plan').waitFor({ state: 'visible' });
  if (!inference) assert.equal(await page.locator('#generate').isEnabled(), false);
  if (inference) {
    assert.ok(process.env.SMOKE_REFERENCE_FILE, 'Real clone smoke needs an audio file');
    await page.locator('.phrase-delete').last().click();
    assert.equal(await page.locator('.phrase-delete').isEnabled(),false);
    await page.locator('#phrase-1').fill('Hello, welcome to OmniVoice.');
    await page.locator('#language-search').fill('');
    await page.locator('#language').selectOption('English');
    await page.locator('#voice-gender').selectOption('female');
    await generateAndDownload('voice-design');
    await page.locator('#clone-mode').click();
    await page.locator('#reference-file').setInputFiles(process.env.SMOKE_REFERENCE_FILE);
    await page.locator('#reference-text').fill('Hello, welcome to OmniVoice.');
    await page.locator('#pause').selectOption('1');
    await page.locator('#add-block').click();
    await page.locator('.phrase textarea').last().fill('Let us try this voice together.');
    await page.locator('#language').selectOption('English');
    await generateAndDownload('voice-clone', true);
  }
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => document.fonts.ready);
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
    assert.ok(dimensions.content <= dimensions.viewport + 1, `${name} overflow: ${JSON.stringify(dimensions)}`);
    for (const position of [0,0.5,1]) {
      const action = await page.evaluate((position) => {
        window.scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*position);
        const button=document.querySelector('#review'), box=button.getBoundingClientRect();
        const hit=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2);
        return {visible:box.top>=0&&box.bottom<=innerHeight&&Boolean(hit?.closest('#review')),barHeight:document.querySelector('.editor-actions').getBoundingClientRect().height,reserved:parseFloat(getComputedStyle(document.body).paddingBottom)};
      },position);
      assert.ok(action.visible,`${name}: review action was inaccessible at scroll ${position}`);
      assert.ok(action.reserved>=action.barHeight,`${name}: action bar covers the end of the page`);
    }
    await page.screenshot({ path: path.join(outputDir, `${name}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.phrase-settings').first().click();
  assert.equal(await page.locator('#language-search').evaluate((el) => el === document.activeElement), true);
  await page.locator('#return-to-script').click();
  assert.equal(await page.locator('#phrase-1').evaluate((el) => el === document.activeElement), true);
  assert.deepEqual(failures, [], 'Studio resources must remain on the public origin');
  console.log(`PASS: Studio HTTPS public proxy, tags, per-phrase language, recording lifecycle, validation, desktop/mobile${inference ? ', real design/clone WAV and pause' : '; metadata-only, no inference'}. Evidence: ${outputDir}`);
}

(async () => {
  let deadline;
  try {
    await Promise.race([run(), new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('Studio browser smoke deadline exceeded')), inference ? 1500000 : 240000); })]);
  } catch (error) {
    console.error(error);
    console.error(failures);
    if (page && !page.isClosed()) await page.screenshot({ path: path.join(outputDir, 'failure.png'), fullPage: true, timeout: 5000 }).catch(() => {});
    process.exitCode = 1;
  } finally {
    clearTimeout(deadline);
    if (browser) await browser.close();
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => proxy.close(resolve));
  }
})();
