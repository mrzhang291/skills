#!/usr/bin/env node

import crypto from 'node:crypto';
import { createReadStream, existsSync } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { captureViewportTiles } from './viewport-tile-capture.mjs';

const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const values = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) continue;
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith('--')) {
      values[key] = next;
      i += 1;
    } else {
      values[key] = true;
    }
  }
  return values;
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function displayUrl(raw) {
  try {
    const url = new URL(raw);
    for (const key of [...url.searchParams.keys()]) {
      if (/token|secret|signature|session|credential|password|logid|rpid|evext|^ak$/i.test(key)) {
        url.searchParams.set(key, '[redacted]');
      }
    }
    return url.toString();
  } catch {
    return raw;
  }
}

function findBrowserExecutable(explicit) {
  if (explicit) return existsSync(explicit) ? explicit : null;
  const candidates = [
    process.env.EDGE_EXECUTABLE_PATH,
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.PROGRAMFILES || 'C:\\Program Files', 'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Microsoft/Edge/Application/msedge.exe'),
    process.env.CHROME_EXECUTABLE_PATH,
    path.join(process.env.PROGRAMFILES || 'C:\\Program Files', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

function validateCdpEndpoint(value) {
  if (!value) return null;
  const parsed = new URL(String(value));
  if (!['http:', 'https:'].includes(parsed.protocol)
      || !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(parsed.hostname)) {
    throw new Error('--cdp-endpoint must use an HTTP(S) loopback address');
  }
  return String(value).replace(/\/$/, '');
}

function browserProductFromExecutable(executable) {
  const name = path.basename(String(executable || '')).toLowerCase();
  if (name.includes('msedge')) return 'edge';
  if (name.includes('chrome')) return 'chrome';
  return null;
}

function splitExpected(value) {
  return String(value ?? '').split('|||').map((item) => item.trim()).filter(Boolean);
}

const SEARCH_HOSTS = [
  'bing.com', 'so.com', 'yahoo.com', 'baidu.com', 'sogou.com', 'google.com',
  'duckduckgo.com', 'yandex.com', 'yandex.ru', 'search.brave.com',
];
const PLATFORM_SEARCH_HOSTS = [
  'search.jd.com', 'search.suning.com', 'search.dangdang.com', 's.taobao.com',
  'list.tmall.com', 's.1688.com', 'mobile.yangkeduo.com',
];

function hostMatches(host, values) {
  return values.some((known) => host === known || host.endsWith(`.${known}`));
}

function isSearchResultUrl(raw) {
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    const pathname = url.pathname.toLowerCase();
    if (hostMatches(host, SEARCH_HOSTS) || hostMatches(host, PLATFORM_SEARCH_HOSTS)) return true;
    if ((url.searchParams.has('q') || url.searchParams.has('keyword')) && /\/(search|s)(\/|$)/i.test(pathname)) return true;
    return false;
  } catch {
    return true;
  }
}

function classifyErrorPage({ url, title, text, httpStatus }) {
  const numericStatus = Number.parseInt(httpStatus ?? '', 10);
  const signals = [];
  if ([401, 403, 405, 407, 429, 451].includes(numericStatus)) {
    signals.push(`http_status_${numericStatus}`);
    return { state: 'access_denied', valid: false, signals };
  }
  if (Number.isFinite(numericStatus) && numericStatus >= 400) {
    signals.push(`http_status_${numericStatus}`);
    return { state: 'error_page', valid: false, signals };
  }

  let pathname = '';
  let errorHint = '';
  try {
    const parsed = new URL(url);
    pathname = parsed.pathname;
    errorHint = ['status', 'error', 'code', 'httpStatus']
      .map((key) => parsed.searchParams.get(key) || '')
      .join(' ');
  } catch {
    pathname = String(url || '');
  }
  if (/^405$/i.test(errorHint.trim()) || /(?:^|[\/_\-.])405(?:[\/_\-.]|$)|\/(?:forbidden|access[-_]?denied|permission[-_]?denied|blocked)(?:[\/.]|$)/i.test(pathname)) {
    signals.push('access_denied_url_path');
    return { state: 'access_denied', valid: false, signals };
  }
  if (/^404$/i.test(errorHint.trim()) || /(?:^|[\/_\-.])404(?:[\/_\-.]|$)|\/(?:errors?|not[-_]?found|page[-_]?not[-_]?found)(?:[\/.]|$)/i.test(pathname)) {
    signals.push('error_url_path');
    return { state: 'error_page', valid: false, signals };
  }

  const cleanTitle = String(title || '').replace(/\s+/g, ' ').trim();
  if (/(?:^|[\s([（])405(?:[\s\])）:：-]+(?:method not allowed|错误|请求不允许|不允许)|\s*$)|method not allowed|access denied|forbidden|拒绝访问|无权访问|访问受限|请求被拒绝/i.test(cleanTitle)) {
    signals.push('access_denied_title');
    return { state: 'access_denied', valid: false, signals };
  }
  if (/^(?:4(?:00|10)\b|5\d\d\b)|(?:^|[\s([（])404(?:[\s\])）:：-]+(?:not found|page not found|错误|页面不存在|页面未找到)|\s*$)|page not found|not found|页面不存在|页面未找到|找不到页面|错误页|系统错误|服务器错误|bad gateway|service unavailable/i.test(cleanTitle)) {
    signals.push('error_page_title');
    return { state: 'error_page', valid: false, signals };
  }

  const body = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 12000);
  if (/\b405\s+method not allowed\b|the requested method is not allowed|请求方法不允许|您没有权限访问|无权访问此页面|拒绝访问此页面/i.test(body)) {
    signals.push('access_denied_body');
    return { state: 'access_denied', valid: false, signals };
  }
  if (/\b404\s+(?:not found|page not found)\b|the requested (?:url|page) was not found|您访问的页面不存在|页面不存在或已删除|抱歉[，, ]*(?:您访问的)?页面(?:不存在|找不到)|系统(?:发生)?错误|服务器(?:内部)?错误/i.test(body)) {
    signals.push('error_page_body');
    return { state: 'error_page', valid: false, signals };
  }
  return null;
}

function classifyPage({ url, title, text, httpStatus, expectedText, expectedAll, expectedAny, mainContentChars, substantiveImageCount, scrollHeight, viewportHeight, allowImageOnly }) {
  const errorClassification = classifyErrorPage({ url, title, text, httpStatus });
  if (errorClassification) return errorClassification;
  const sample = `${title}\n${url}\n${text.slice(0, 120000)}`;
  const signals = [];
  const tests = [
    ['captcha', /安全验证|验证码|完成.{0,8}验证|拖动.{0,12}滑块|请按住滑块|captcha|wappass|risk_handler|punish/i],
    ['access_denied', /访问受限|拒绝访问|access denied|forbidden|请求异常|操作过于频繁/i],
    ['login_required', /请重新登录|扫码安全登录|扫码登录更安全|密码登录\s*短信登录|账号名.{0,8}(手机号|邮箱)|(手机号|邮箱).{0,8}账号名|请输入登录密码|登录后继续|login required/i],
  ];
  for (const [kind, pattern] of tests) {
    if (pattern.test(sample)) signals.push(kind);
  }
  if (signals.includes('captcha')) return { state: 'captcha', valid: false, signals };
  if (signals.includes('access_denied')) return { state: 'access_denied', valid: false, signals };
  if (signals.includes('login_required')) {
    return { state: 'login_required', valid: false, signals };
  }
  if (isSearchResultUrl(url)) {
    signals.push('search_result_page_forbidden');
    return { state: 'search_result_page', valid: false, signals };
  }
  if (/没有找到|未找到相关|暂无相关|搜索结果为空|相关商品较少|no results/i.test(sample)) {
    return { state: 'empty_results', valid: false, signals };
  }
  if (text.trim().length < 120 && substantiveImageCount < 1 && scrollHeight <= viewportHeight * 1.15) {
    signals.push('visible_content_too_small');
    return { state: 'empty_shell', valid: false, signals };
  }
  if (expectedText && !text.includes(expectedText)) {
    if (allowImageOnly && substantiveImageCount > 0) {
      signals.push('expected_text_missing_visual_review_required');
      return { state: 'manual_visual_review', valid: true, signals };
    }
    signals.push('expected_text_missing');
    return { state: 'unexpected_content', valid: false, signals };
  }
  const missingAll = expectedAll.filter((item) => !text.includes(item));
  if (missingAll.length) {
    signals.push(...missingAll.map((item) => `expected_all_missing:${item}`));
    return { state: 'unexpected_content', valid: false, signals };
  }
  if (expectedAny.length && !expectedAny.some((item) => text.includes(item))) {
    if (allowImageOnly && substantiveImageCount > 0) {
      signals.push('expected_any_missing_visual_review_required');
      return { state: 'manual_visual_review', valid: true, signals };
    }
    signals.push('expected_any_missing');
    return { state: 'unexpected_content', valid: false, signals };
  }
  if (text.trim().length < 240 && mainContentChars < 80) {
    if (substantiveImageCount > 0 && allowImageOnly) {
      signals.push('insufficient_main_text_visual_review_required');
      return { state: 'manual_visual_review', valid: true, signals };
    }
    signals.push('insufficient_main_or_body_content');
    return { state: 'empty_shell', valid: false, signals };
  }
  if (text.trim().length < 120 && substantiveImageCount > 0) {
    signals.push('image_dominant_page_visual_review_required');
    return { state: 'manual_visual_review', valid: true, signals };
  }
  return { state: 'normal', valid: true, signals };
}

async function sha256(filePath) {
  const digest = crypto.createHash('sha256');
  await new Promise((resolve, reject) => {
    const stream = createReadStream(filePath);
    stream.on('data', (chunk) => digest.update(chunk));
    stream.on('end', resolve);
    stream.on('error', reject);
  });
  return digest.digest('hex');
}

async function artifactInfo(outputDir, fileName) {
  const filePath = path.join(outputDir, fileName);
  const stat = await fs.stat(filePath);
  return { path: fileName.replaceAll('\\', '/'), size_bytes: stat.size, sha256: await sha256(filePath) };
}

async function promoteLazyImageSources(page) {
  return page.evaluate(() => {
    const lazyAttrs = ['data-src', 'data-lazy-src', 'data-ks-lazyload', 'data-original', 'data-img'];
    let promoted = 0;
    let promotedSrcset = 0;
    for (const img of document.images) {
      img.loading = 'eager';
      const current = String(img.currentSrc || img.getAttribute('src') || '').trim();
      if (!current || /^data:image\/(?:gif|svg\+xml);base64,/i.test(current)) {
        const intended = lazyAttrs.map((name) => img.getAttribute(name)).find((value) => value && !/^data:image\/(?:gif|svg\+xml);base64,/i.test(value));
        if (intended) {
          img.setAttribute('src', intended);
          promoted += 1;
        }
      }
      const lazySrcset = img.getAttribute('data-srcset');
      if (lazySrcset && !img.getAttribute('srcset')) {
        img.setAttribute('srcset', lazySrcset);
        promotedSrcset += 1;
      }
    }
    for (const source of document.querySelectorAll('picture source[data-srcset]')) {
      if (!source.getAttribute('srcset')) {
        source.setAttribute('srcset', source.getAttribute('data-srcset'));
        promotedSrcset += 1;
      }
    }
    const images = [...document.images].map((img) => {
      const rect = img.getBoundingClientRect();
      const declaredWidth = Number.parseFloat(img.getAttribute('width') || '0');
      const declaredHeight = Number.parseFloat(img.getAttribute('height') || '0');
      const substantive = Math.max(rect.width, declaredWidth, img.naturalWidth) >= 60
        && Math.max(rect.height, declaredHeight, img.naturalHeight) >= 45;
      return { substantive, loaded: img.complete && img.naturalWidth > 1 && img.naturalHeight > 1 };
    });
    const substantive = images.filter((item) => item.substantive).length;
    const loaded = images.filter((item) => item.substantive && item.loaded).length;
    return {
      total: images.length, substantive, loaded,
      unresolved_substantive: Math.max(0, substantive - loaded),
      promoted, promoted_srcset: promotedSrcset,
    };
  }).catch(() => ({
    total: 0, substantive: 0, loaded: 0, unresolved_substantive: 0,
    promoted: 0, promoted_srcset: 0,
  }));
}

let chromium;
let playwrightVersion = null;
let playwrightLoadError = null;
try {
  ({ chromium } = require('playwright-core'));
  playwrightVersion = require('playwright-core/package.json').version;
} catch (coreError) {
  try {
    ({ chromium } = require('playwright'));
    playwrightVersion = require('playwright/package.json').version;
  } catch (playwrightError) {
    playwrightLoadError = [
      'Playwright runtime is missing. Run npm install --omit=dev --ignore-scripts in the skill directory.',
      `playwright-core: ${coreError instanceof Error ? coreError.message : String(coreError)}`,
      `playwright: ${playwrightError instanceof Error ? playwrightError.message : String(playwrightError)}`,
    ].join(' ');
  }
}

const args = parseArgs(process.argv.slice(2));
if (!args.url || !args['output-dir']) {
  console.error('Usage: capture-page-to-pdf.mjs --url <url> --output-dir <dir> [--source-id S001] [--expected-text text] [--probe-only] [--headed --wait-for-unblock-ms 120000] [--user-data-dir dir]');
  process.exit(1);
}

const probeOnly = Boolean(args['probe-only']);
const outputDir = path.resolve(args['output-dir']);
const timeoutMs = positiveInt(args['timeout-ms'], 45_000);
const maxScrollSteps = positiveInt(args['max-scroll-steps'], probeOnly ? 8 : 40);
const scrollDelayMs = positiveInt(args['scroll-delay-ms'], probeOnly ? 100 : 250);
const settleMs = positiveInt(args['settle-ms'], probeOnly ? 500 : 1_200);
const waitForUnblockMs = positiveInt(args['wait-for-unblock-ms'], 0);
if (waitForUnblockMs > 0 && !args.headed) {
  console.error('--wait-for-unblock-ms requires --headed');
  process.exit(1);
}
const expectedAll = splitExpected(args['expected-text-all']);
const expectedAny = splitExpected(args['expected-text-any']);
const capturedAt = new Date().toISOString();
const browserExecutable = findBrowserExecutable(args['browser-executable']);
const cdpEndpoint = validateCdpEndpoint(args['cdp-endpoint']);
await fs.mkdir(outputDir, { recursive: true });
for (const generatedName of ['page.pdf', 'probe.png', 'fullpage.png', 'offline.png', 'mhtml-offline.png', 'page.html', 'rendered-dom.html', 'response.html', 'body-text.txt', 'response-headers.json', 'page.mhtml', 'page.singlefile.html', 'page.singlefile.raw.html', 'offline-validation.json', 'mhtml-validation.json', 'archive-visual-comparison.json', 'page-images.json', 'page-links.json', 'metadata.json', 'images']) {
  await fs.rm(path.join(outputDir, generatedName), { recursive: true, force: true });
}

const metadata = {
  schema_version: '2.0',
  record_type: probeOnly ? 'target_page_probe' : 'target_page_capture',
  capture_mode: probeOnly ? 'probe' : 'full',
  page_role: 'candidate',
  source_id: args['source-id'] ?? path.basename(outputDir),
  order: args.order ? Number.parseInt(args.order, 10) : null,
  label: args.label ?? null,
  page_type: args['page-type'] ?? 'other',
  query_id: args['query-id'] ?? null,
  query: args.query ?? null,
  expected_text: args['expected-text'] ?? null,
  expected_text_all: expectedAll,
  expected_text_any: expectedAny,
  requested_url: args.url,
  final_url: null,
  display_url: null,
  title: null,
  captured_at: capturedAt,
  http_status: null,
  redirect_chain: [],
  response_headers_redacted: true,
  user_agent: null,
  viewport: probeOnly ? { width: 1024, height: 640 } : { width: 1440, height: 900 },
  browser_executable: browserExecutable,
  runtime: { node: process.version, platform: process.platform, playwright: playwrightVersion, browser_version: null },
  interactive_capture: {
    headed: Boolean(args.headed),
    private_profile_reused: Boolean(args['user-data-dir']),
    wait_for_unblock_ms: waitForUnblockMs,
  },
  page_state: 'failed',
  content_valid: false,
  block_signals: [],
  text_chars: 0,
  main_content_chars: 0,
  substantive_image_count: 0,
  commercial_signals: [],
  search_result_page: false,
  archive_primary: probeOnly ? null : 'page.mhtml',
  portable_html: probeOnly ? null : 'page.singlefile.html',
  singlefile_original: probeOnly ? null : 'page.singlefile.raw.html',
  pdf_is_derivative: probeOnly ? null : true,
  raster_fallback: false,
  scroll: { max_steps: maxScrollSteps, completed_steps: 0, reached_bottom: false },
  artifacts: {},
  errors: [],
};

let browser = null;
let context = null;
let page = null;
let ownsBrowser = false;
let ownsContext = false;
let connectedBrowser = false;

try {
  if (!chromium) {
    throw new Error(playwrightLoadError || 'Playwright runtime is unavailable');
  }
  if (!browserExecutable && !cdpEndpoint) {
    throw new Error('Chrome/Edge executable was not found; pass --browser-executable');
  }
  if (cdpEndpoint) {
    const expectedProduct = browserProductFromExecutable(browserExecutable);
    if (expectedProduct) {
      const response = await fetch(`${cdpEndpoint}/json/version`);
      if (!response.ok) throw new Error(`CDP version probe failed: HTTP ${response.status}`);
      const version = await response.json();
      const product = String(version.Browser || '').toLowerCase();
      const matches = expectedProduct === 'edge'
        ? (product.includes('edg/') || product.includes('microsoft edge'))
        : (product.includes('chrome/') && !product.includes('edg/'));
      if (!matches) throw new Error(`CDP browser product mismatch: expected ${expectedProduct}, got ${version.Browser || 'unknown'}`);
    }
    browser = await chromium.connectOverCDP(cdpEndpoint);
    connectedBrowser = true;
    context = browser.contexts()[0];
    if (!context) throw new Error('No browser context is available at the CDP endpoint');
  } else if (args['user-data-dir']) {
    const launchArgs = ['--disable-background-mode'];
    if (browserExecutable && path.basename(browserExecutable).toLowerCase().includes('msedge')) {
      launchArgs.push('--edge-skip-compat-layer-relaunch', '--disable-features=msEdgeUpdateLaunchServicesPreferredVersion');
    }
    if (args['profile-directory']) launchArgs.push(`--profile-directory=${args['profile-directory']}`);
    context = await chromium.launchPersistentContext(path.resolve(args['user-data-dir']), {
      executablePath: browserExecutable,
      headless: !args.headed,
      viewport: metadata.viewport,
      ignoreHTTPSErrors: false,
      args: launchArgs,
    });
    ownsContext = true;
  } else {
    const launchArgs = [];
    if (browserExecutable && path.basename(browserExecutable).toLowerCase().includes('msedge')) {
      launchArgs.push('--edge-skip-compat-layer-relaunch', '--disable-features=msEdgeUpdateLaunchServicesPreferredVersion');
    }
    browser = await chromium.launch({ executablePath: browserExecutable, headless: !args.headed, args: launchArgs });
    ownsBrowser = true;
    const contextOptions = { viewport: metadata.viewport, ignoreHTTPSErrors: false };
    if (args['storage-state']) contextOptions.storageState = path.resolve(args['storage-state']);
    context = await browser.newContext(contextOptions);
    ownsContext = true;
  }
  page = await context.newPage();
  try {
    metadata.runtime.browser_version = (browser || context.browser())?.version() ?? null;
  } catch {}

  let response = null;
  try {
    response = await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
  } catch (error) {
    metadata.errors.push({ stage: 'navigation', message: error instanceof Error ? error.message : String(error) });
  }
  metadata.http_status = response?.status() ?? null;
  if (response) {
    try {
      const chain = [];
      let request = response.request();
      while (request) {
        chain.unshift(request.url());
        request = request.redirectedFrom();
      }
      metadata.redirect_chain = [...new Set(chain)];
      const headers = await response.allHeaders().catch(() => response.headers());
      if (!probeOnly) {
        const redacted = {};
        for (const [key, value] of Object.entries(headers || {})) {
          redacted[key] = /set-cookie|authorization|proxy-authenticate|x-api-key/i.test(key) ? '[redacted]' : value;
        }
        await fs.writeFile(path.join(outputDir, 'response-headers.json'), `${JSON.stringify(redacted, null, 2)}\n`, 'utf8');
        metadata.artifacts.response_headers = await artifactInfo(outputDir, 'response-headers.json');
        if (/html|xhtml/i.test(String(headers?.['content-type'] || ''))) {
          const body = await response.body();
          if (body?.length) {
            await fs.writeFile(path.join(outputDir, 'response.html'), body);
            metadata.artifacts.raw_html = await artifactInfo(outputDir, 'response.html');
          }
        }
      }
    } catch (error) {
      metadata.errors.push({ stage: 'response_capture', message: error instanceof Error ? error.message : String(error) });
    }
  }
  await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, probeOnly ? 2_000 : 8_000) }).catch(() => {});
  await page.evaluate(async () => { if (document.fonts?.ready) await document.fonts.ready; }).catch(() => {});

  const lazyImageInitial = await promoteLazyImageSources(page);
  let lastHeight = 0;
  let stableHeightCount = 0;
  for (let step = 0; step < maxScrollSteps; step += 1) {
    const state = await page.evaluate(() => {
      const root = document.scrollingElement || document.documentElement;
      const viewport = window.innerHeight;
      const height = Math.max(root.scrollHeight, document.body?.scrollHeight || 0);
      const y = root.scrollTop;
      window.scrollBy(0, Math.max(500, Math.floor(viewport * 0.8)));
      return { height, viewport, y };
    }).catch(() => null);
    if (!state) break;
    metadata.scroll.completed_steps = step + 1;
    stableHeightCount = state.height === lastHeight ? stableHeightCount + 1 : 0;
    lastHeight = state.height;
    if (state.y + state.viewport >= state.height - 4 && stableHeightCount >= 2) {
      metadata.scroll.reached_bottom = true;
      break;
    }
    await page.waitForTimeout(scrollDelayMs);
    await promoteLazyImageSources(page);
  }
  await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
  await page.waitForTimeout(settleMs);
  await page.evaluate(async () => {
    const pending = [...document.images]
      .filter((img) => {
        const rect = img.getBoundingClientRect();
        return Math.max(rect.width, img.naturalWidth) >= 60 && Math.max(rect.height, img.naturalHeight) >= 45;
      })
      .map((img) => img.decode?.().catch(() => undefined));
    await Promise.race([
      Promise.allSettled(pending),
      new Promise((resolve) => setTimeout(resolve, 5000)),
    ]);
  }).catch(() => {});
  let lazyImageFinal = await promoteLazyImageSources(page);
  let lazyImageRetried = false;
  let loadedRatio = lazyImageFinal.substantive ? lazyImageFinal.loaded / lazyImageFinal.substantive : 1;
  if (lazyImageFinal.unresolved_substantive > 2 && loadedRatio < 0.85) {
    lazyImageRetried = true;
    await page.evaluate(() => window.scrollTo(0, Math.max(0, (document.scrollingElement?.scrollHeight || 0) / 2))).catch(() => {});
    await page.waitForTimeout(Math.max(700, scrollDelayMs * 2));
    await promoteLazyImageSources(page);
    await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
    await page.waitForTimeout(Math.max(700, scrollDelayMs * 2));
    lazyImageFinal = await promoteLazyImageSources(page);
    loadedRatio = lazyImageFinal.substantive ? lazyImageFinal.loaded / lazyImageFinal.substantive : 1;
  }
  metadata.lazy_image_hydration = {
    initial: lazyImageInitial,
    final: lazyImageFinal,
    retried: lazyImageRetried,
    loaded_ratio: Number(loadedRatio.toFixed(4)),
    acceptable: lazyImageFinal.unresolved_substantive <= 2 || loadedRatio >= 0.85,
  };
  metadata.image_load_complete = metadata.lazy_image_hydration.acceptable;

  let latestVisibleText = '';
  async function refreshState() {
    const title = await page.title().catch(() => '');
    const topFrameText = await page.locator('body').innerText({ timeout: 3_000 }).catch(() => '');
    const frameTexts = await Promise.all(page.frames().map(async (frame) => {
      const frameText = await frame.locator('body').innerText({ timeout: 3_000 }).catch(() => '');
      return frameText ? `[frame:${frame.url()}]\n${frameText}` : '';
    }));
    const text = frameTexts.filter(Boolean).join('\n');
    latestVisibleText = topFrameText;
    const url = page.url();
    const metrics = await page.evaluate(() => {
      const root = document.scrollingElement || document.documentElement;
      const viewportHeight = window.innerHeight || 1;
      const mainElements = [...document.querySelectorAll('main,article,[role="main"]')];
      const mainText = mainElements.map((item) => item.innerText || '').join('\n').trim();
      const substantiveImages = [...document.images].filter((img) => {
        const rect = img.getBoundingClientRect();
        const style = getComputedStyle(img);
        return rect.width >= 80 && rect.height >= 60 && img.naturalWidth >= 80 && img.naturalHeight >= 60 && style.visibility !== 'hidden' && style.display !== 'none';
      }).length;
      return {
        mainContentChars: mainText.length,
        substantiveImageCount: substantiveImages,
        scrollHeight: Math.max(root.scrollHeight, document.body?.scrollHeight || 0),
        viewportHeight,
      };
    }).catch(() => ({ mainContentChars: 0, substantiveImageCount: 0, scrollHeight: 0, viewportHeight: 1 }));
    let classification = classifyPage({
      url, title, text, httpStatus: metadata.http_status,
      expectedText: args['expected-text'], expectedAll, expectedAny,
      substantiveImageCount: metrics.substantiveImageCount,
      mainContentChars: metrics.mainContentChars,
      scrollHeight: metrics.scrollHeight,
      viewportHeight: metrics.viewportHeight,
      allowImageOnly: Boolean(args['allow-image-only']),
    });
    const visibleCaptchaFrame = await page.locator('iframe[src*="captcha" i]:visible, iframe[src*="punish" i]:visible, iframe[src*="_____tmd_____" i]:visible').count().catch(() => 0);
    const visibleLoginFrame = await page.locator('iframe[src*="/login" i]:visible, iframe[src*="login." i]:visible, iframe[src*="passport" i]:visible, iframe[src*="member/login" i]:visible').count().catch(() => 0);
    if (visibleLoginFrame > 0) {
      classification = { state: 'login_required', valid: false, signals: [...new Set([...classification.signals, 'visible_login_frame'])] };
    } else if (visibleCaptchaFrame > 0) {
      classification = { state: 'captcha', valid: false, signals: [...new Set([...classification.signals, 'visible_captcha_frame'])] };
    }
    metadata.title = title;
    metadata.final_url = url;
    metadata.display_url = displayUrl(url);
    metadata.text_chars = text.length;
    metadata.main_content_chars = metrics.mainContentChars;
    metadata.substantive_image_count = metrics.substantiveImageCount;
    metadata.search_result_page = isSearchResultUrl(url);
    metadata.commercial_signals = [
      /[¥￥]\s*\d|\d+(?:\.\d{1,2})?\s*元/.test(text) ? 'price' : null,
      /加入购物车|立即购买|购买|下单|buy now|add to cart/i.test(text) ? 'purchase_action' : null,
      /库存|销量|已售|发货|配送|sku/i.test(text) ? 'commerce_metadata' : null,
      /店铺|旗舰店|经销商|供应商|厂家|company|manufacturer/i.test(text) ? 'seller_or_actor' : null,
    ].filter(Boolean);
    metadata.page_state = classification.state;
    metadata.content_valid = classification.valid;
    metadata.block_signals = classification.signals;
    return classification;
  }

  let classification = await refreshState();
  if (!classification.valid && args.headed && waitForUnblockMs > 0) {
    const deadline = Date.now() + waitForUnblockMs;
    while (Date.now() < deadline) {
      await page.waitForTimeout(2_000);
      classification = await refreshState();
      if (classification.valid) break;
    }
  }
  metadata.user_agent = await page.evaluate(() => navigator.userAgent).catch(() => null);

  try {
    await fs.writeFile(path.join(outputDir, 'body-text.txt'), latestVisibleText, 'utf8');
    metadata.artifacts.body_text = await artifactInfo(outputDir, 'body-text.txt');
  } catch (error) {
    metadata.errors.push({ stage: 'body_text', message: error instanceof Error ? error.message : String(error) });
  }

  if (probeOnly) {
    try {
      await page.screenshot({ path: path.join(outputDir, 'probe.png'), fullPage: false });
      metadata.artifacts.probe = await artifactInfo(outputDir, 'probe.png');
    } catch (error) {
      metadata.errors.push({ stage: 'probe_screenshot', message: error instanceof Error ? error.message : String(error) });
    }
    if (!metadata.artifacts.body_text || !metadata.artifacts.probe) {
      metadata.content_valid = false;
      metadata.page_state = 'probe_artifact_failed';
      metadata.block_signals = [...new Set([...metadata.block_signals, 'required_probe_artifact_missing'])];
    }
  } else {
    if (args['save-storage-state']) {
      await context.storageState({ path: path.resolve(args['save-storage-state']) });
    }

    try {
    const imageIndex = await page.locator('img').evaluateAll((images) => images.slice(0, 300).map((img, index) => {
      const rect = img.getBoundingClientRect();
      return { index, src: img.currentSrc || img.src || null, alt: img.alt || '', width: rect.width, height: rect.height, natural_width: img.naturalWidth, natural_height: img.naturalHeight };
    }));
    await fs.writeFile(path.join(outputDir, 'page-images.json'), `${JSON.stringify(imageIndex, null, 2)}\n`, 'utf8');
    metadata.artifacts.images_index = await artifactInfo(outputDir, 'page-images.json');
  } catch (error) {
    metadata.errors.push({ stage: 'image_index', message: error instanceof Error ? error.message : String(error) });
  }

  try {
    const pageLinks = await page.locator('a[href]').evaluateAll((links) => links.slice(0, 1000).map((link) => ({
      text: (link.innerText || link.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300),
      href: link.href || null,
    })).filter((item) => item.href));
    const meaningful = pageLinks.filter((item) => item.text.length >= 2 && /^https?:/i.test(item.href));
    await fs.writeFile(path.join(outputDir, 'page-links.json'), `${JSON.stringify(pageLinks, null, 2)}\n`, 'utf8');
    metadata.link_count = pageLinks.length;
    metadata.meaningful_link_count = meaningful.length;
    metadata.artifacts.links_index = await artifactInfo(outputDir, 'page-links.json');
  } catch (error) {
    metadata.errors.push({ stage: 'links_index', message: error instanceof Error ? error.message : String(error) });
  }

  if (args['capture-images']) {
    const imageDir = path.join(outputDir, 'images');
    await fs.mkdir(imageDir, { recursive: true });
    const locators = page.locator('img');
    const count = Math.min(await locators.count(), positiveInt(args['max-images'], 30));
    let saved = 0;
    for (let i = 0; i < count; i += 1) {
      const locator = locators.nth(i);
      const box = await locator.boundingBox().catch(() => null);
      if (!box || box.width < 80 || box.height < 60) continue;
      const name = `images/image-${String(saved + 1).padStart(3, '0')}.png`;
      try {
        await locator.screenshot({ path: path.join(outputDir, name) });
        saved += 1;
      } catch {}
    }
    metadata.extracted_image_count = saved;
  }

  await page.addStyleTag({ content: '*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition:none!important}' }).catch(() => {});
  try {
    const visualCapture = await captureViewportTiles(page, path.join(outputDir, 'fullpage.png'));
    metadata.visual_capture = visualCapture;
    metadata.capture_strategy = visualCapture.strategy;
    metadata.image_load_complete = Boolean(metadata.image_load_complete && visualCapture.acceptable);
    if (!visualCapture.acceptable) {
      metadata.content_valid = false;
      metadata.page_state = 'visual_capture_incomplete';
      metadata.block_signals = [...new Set([...metadata.block_signals, 'painted_image_validation_failed'])];
      metadata.errors.push({ stage: 'visual_capture_quality', message: 'stitched screenshot did not pass painted-image validation' });
    }
    metadata.artifacts.fullpage = await artifactInfo(outputDir, 'fullpage.png');
  } catch (error) {
    metadata.errors.push({ stage: 'screenshot', message: error instanceof Error ? error.message : String(error) });
    metadata.content_valid = false;
    metadata.page_state = 'visual_capture_incomplete';
    metadata.block_signals = [...new Set([...metadata.block_signals, 'viewport_tile_capture_failed'])];
  }
  try {
    await fs.writeFile(path.join(outputDir, 'rendered-dom.html'), await page.content(), 'utf8');
    metadata.artifacts.rendered_dom = await artifactInfo(outputDir, 'rendered-dom.html');
  } catch (error) {
    metadata.errors.push({ stage: 'rendered_dom', message: error instanceof Error ? error.message : String(error) });
  }
  try {
    const session = await context.newCDPSession(page);
    const snapshot = await session.send('Page.captureSnapshot', { format: 'mhtml' });
    await fs.writeFile(path.join(outputDir, 'page.mhtml'), snapshot.data, 'utf8');
    metadata.artifacts.mhtml = await artifactInfo(outputDir, 'page.mhtml');
    await session.detach();
  } catch (error) {
    metadata.errors.push({ stage: 'mhtml', message: error instanceof Error ? error.message : String(error) });
  }
  try {
    await page.emulateMedia({ media: 'screen' });
    metadata.print_layout_cleanup = await page.evaluate(() => {
      let fixed = 0;
      let sticky = 0;
      for (const element of document.querySelectorAll('body *')) {
        const position = getComputedStyle(element).position;
        if (position === 'fixed') {
          element.style.setProperty('position', 'absolute', 'important');
          fixed += 1;
        } else if (position === 'sticky' || position === '-webkit-sticky') {
          element.style.setProperty('position', 'relative', 'important');
          element.style.setProperty('top', 'auto', 'important');
          sticky += 1;
        }
      }
      document.documentElement.style.setProperty('height', 'auto', 'important');
      document.body.style.setProperty('height', 'auto', 'important');
      document.body.style.setProperty('min-height', '0', 'important');
      return { fixed_to_absolute: fixed, sticky_to_relative: sticky };
    }).catch(() => null);
    const title = escapeHtml(metadata.title || 'Untitled page');
    const url = escapeHtml(metadata.display_url || args.url);
    await page.pdf({
      path: path.join(outputDir, 'page.pdf'), format: 'A4', printBackground: true, displayHeaderFooter: true,
      preferCSSPageSize: false, margin: { top: '16mm', right: '10mm', bottom: '18mm', left: '10mm' },
      headerTemplate: `<div style="width:100%;font-size:8px;color:#555;padding:0 10mm;display:flex;justify-content:space-between;gap:12px"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${title}</span><span>${escapeHtml(capturedAt)}</span></div>`,
      footerTemplate: `<div style="width:100%;font-size:7px;color:#555;padding:0 10mm;display:flex;justify-content:space-between;gap:12px"><span style="max-width:72%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${url}</span><span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>`,
      timeout: timeoutMs,
    });
    const python = args.python || process.env.PYTHON_EXECUTABLE || (process.platform === 'win32' ? 'python' : 'python3');
    const trimScript = path.join(path.dirname(fileURLToPath(import.meta.url)), 'trim-pdf-empty-tail.py');
    const trim = spawnSync(python, [trimScript, '--pdf', path.join(outputDir, 'page.pdf')], { encoding: 'utf8' });
    if (trim.status === 0) {
      try {
        metadata.pdf_cleanup = JSON.parse(trim.stdout.trim());
      } catch {
        metadata.pdf_cleanup = { ok: true, output: trim.stdout.trim() };
      }
    } else {
      metadata.errors.push({ stage: 'pdf_trim', message: (trim.stderr || trim.stdout || 'PDF tail trim failed').trim() });
    }
    metadata.artifacts.pdf = await artifactInfo(outputDir, 'page.pdf');
  } catch (error) {
    metadata.errors.push({ stage: 'pdf', message: error instanceof Error ? error.message : String(error) });
  }

    if (!metadata.artifacts.pdf && metadata.artifacts.fullpage) {
    const python = args.python || process.env.PYTHON_EXECUTABLE || (process.platform === 'win32' ? 'python' : 'python3');
    const fallbackScript = path.join(path.dirname(fileURLToPath(import.meta.url)), 'raster-to-pdf.py');
    const fallback = spawnSync(python, [fallbackScript, '--image', path.join(outputDir, 'fullpage.png'), '--output', path.join(outputDir, 'page.pdf'), '--title', metadata.title || '', '--url', metadata.display_url || args.url], { encoding: 'utf8' });
    if (fallback.status === 0 && existsSync(path.join(outputDir, 'page.pdf'))) {
      metadata.raster_fallback = true;
      metadata.artifacts.pdf = await artifactInfo(outputDir, 'page.pdf');
    } else {
      metadata.errors.push({ stage: 'raster_fallback', message: (fallback.stderr || fallback.stdout || 'fallback failed').trim() });
    }
    }
  }
} catch (error) {
  metadata.errors.push({ stage: 'fatal', message: error instanceof Error ? error.message : String(error) });
} finally {
  if (page) await page.close().catch(() => {});
  if (ownsContext && context) await context.close().catch(() => {});
  if (ownsBrowser && browser) await browser.close().catch(() => {});
  await fs.writeFile(path.join(outputDir, 'metadata.json'), `${JSON.stringify(metadata, null, 2)}\n`, 'utf8');
}

console.log(JSON.stringify({ source_id: metadata.source_id, output_dir: outputDir, capture_mode: metadata.capture_mode, page_state: metadata.page_state, content_valid: metadata.content_valid, probe_created: Boolean(metadata.artifacts.probe), pdf_created: Boolean(metadata.artifacts.pdf), raster_fallback: metadata.raster_fallback, errors: metadata.errors }, null, 2));
let finalExitCode = 0;
if (probeOnly) finalExitCode = metadata.content_valid ? 0 : 3;
else if (!metadata.artifacts.pdf) finalExitCode = 2;
else if (!metadata.content_valid) finalExitCode = 3;
if (connectedBrowser) {
  await new Promise((resolve) => process.stdout.write('', resolve));
  process.exit(finalExitCode);
}
process.exitCode = finalExitCode;
