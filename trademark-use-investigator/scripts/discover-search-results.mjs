#!/usr/bin/env node

import fs from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { acquireArtifactLock } from './browser-artifact-lock.mjs';

const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith('--')) continue;
    const key = value.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith('--')) {
      result[key] = next;
      index += 1;
    } else result[key] = true;
  }
  return result;
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
    '/usr/bin/google-chrome', '/usr/bin/chromium',
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

function normalizeQuery(value) {
  return String(value || '').replace(/[+＋]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function providerConfig(provider, query) {
  const encoded = encodeURIComponent(normalizeQuery(query));
  const configs = {
    bing: {
      url: `https://www.bing.com/search?q=${encoded}`,
      selectors: ['li.b_algo h2 a', 'li.b_ans h2 a', 'main h2 a'],
      hosts: ['bing.com'],
    },
    so360: {
      url: `https://www.so.com/s?q=${encoded}`,
      selectors: ['li.res-list h3 a', '.res-list h3 a', '.result h3 a', 'h3 a'],
      hosts: ['so.com', '360.cn'],
    },
    sogou: {
      url: `https://www.sogou.com/web?query=${encoded}`,
      selectors: ['.vrwrap h3 a', '.results h3 a', '.rb h3 a', 'h3 a'],
      hosts: ['sogou.com', 'sogoucdn.com'],
    },
    yahoo: {
      url: `https://search.yahoo.com/search?p=${encoded}`,
      selectors: ['div.algo h3 a', 'ol.searchCenterMiddle h3 a', 'main h3 a'],
      hosts: ['yahoo.com', 'yimg.com'],
    },
    baidu: {
      url: `https://www.baidu.com/s?wd=${encoded}&rn=10`,
      selectors: ['#content_left .result h3 a', '#content_left .c-container h3 a', '.result h3 a', 'h3 a'],
      hosts: ['baidu.com', 'bdimg.com', 'baidustatic.com'],
    },
  };
  if (!configs[provider]) throw new Error(`Unsupported browser search provider: ${provider}`);
  return configs[provider];
}

function decodeRedirect(raw) {
  if (!raw) return null;
  let candidate = raw.trim();
  try {
    const decoded = decodeURIComponent(candidate);
    const yahoo = decoded.match(/\/RU=([^/]+)\/RK=/i);
    if (yahoo) candidate = decodeURIComponent(yahoo[1]);
  } catch {}
  try {
    const url = new URL(candidate);
    for (const key of ['url', 'target', 'dest', 'destination', 'redirect', 'redirect_url', 'r']) {
      const value = url.searchParams.get(key);
      if (value?.startsWith('http://') || value?.startsWith('https://')) return decodeURIComponent(value);
    }
    const value = url.searchParams.get('u');
    if (value?.startsWith('a1')) {
      const payload = value.slice(2).replaceAll('-', '+').replaceAll('_', '/');
      try {
        const decoded = Buffer.from(payload, 'base64').toString('utf8');
        if (/^https?:\/\//i.test(decoded)) return decoded;
      } catch {}
    }
  } catch {}
  return candidate;
}

function providerOwned(url, hosts) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return hosts.some((known) => host === known || host.endsWith(`.${known}`));
  } catch {
    return true;
  }
}

async function resolveProviderRedirect(page, provider, candidate) {
  const redirectHosts = provider === 'baidu' ? ['baidu.com'] : provider === 'sogou' ? ['sogou.com'] : [];
  if (!redirectHosts.length || !providerOwned(candidate, redirectHosts)) return candidate;
  try {
    const parsed = new URL(candidate);
    if (!/^\/(?:link|web\/link)/i.test(parsed.pathname)) return candidate;
    const response = await page.context().request.get(candidate, {
      timeout: 2_000, failOnStatusCode: false, maxRedirects: 3,
    });
    const finalUrl = response.url();
    if (/^https?:\/\//i.test(finalUrl) && !providerOwned(finalUrl, redirectHosts)) return finalUrl;
  } catch {}
  return candidate;
}

function classify(text, title) {
  const sample = `${title}\n${text.slice(0, 100000)}`;
  if (/安全验证|验证码|滑块|captcha|wappass|risk_handler|punish/i.test(sample)) return 'captcha';
  if (/拒绝访问|访问受限|access denied|forbidden|请求异常|操作过于频繁/i.test(sample)) return 'access_denied';
  if (/登录后继续|扫码登录|请输入登录密码|login required/i.test(sample)) return 'login_required';
  if (/没有找到|没有与此相关的结果|没有相关结果|未找到相关|暂无相关|搜索结果为空|no results/i.test(sample)) return 'zero_results';
  return 'normal';
}

async function captureScreenshot(page, outputPath, preferredBackend = 'auto') {
  if (preferredBackend === 'cdp') {
    let session;
    try {
      session = await page.context().newCDPSession(page);
      const captured = await session.send('Page.captureScreenshot', {
        format: 'png', fromSurface: true, captureBeyondViewport: false,
      });
      await fs.writeFile(outputPath, Buffer.from(captured.data, 'base64'));
      return {
        ok: true, method: 'cdp_page_capture_screenshot', fallback_used: false,
        preferred_backend: 'cdp', preferred_backend_used: true,
      };
    } catch (error) {
      return {
        ok: false, method: 'failed', fallback_used: false,
        preferred_backend: 'cdp', preferred_backend_used: true,
        primary_error: String(error?.message || error),
      };
    } finally {
      if (session) await session.detach().catch(() => {});
    }
  }
  try {
    await page.screenshot({
      path: outputPath, fullPage: false,
      animations: 'disabled', caret: 'hide', timeout: 20_000,
    });
    return { ok: true, method: 'playwright_screenshot', fallback_used: false };
  } catch (primaryError) {
    let session;
    try {
      session = await page.context().newCDPSession(page);
      const captured = await session.send('Page.captureScreenshot', {
        format: 'png', fromSurface: true, captureBeyondViewport: false,
      });
      await fs.writeFile(outputPath, Buffer.from(captured.data, 'base64'));
      return {
        ok: true, method: 'cdp_page_capture_screenshot', fallback_used: true,
        primary_error: String(primaryError?.message || primaryError),
      };
    } catch (fallbackError) {
      return {
        ok: false, method: 'failed', fallback_used: true,
        primary_error: String(primaryError?.message || primaryError),
        fallback_error: String(fallbackError?.message || fallbackError),
      };
    } finally {
      if (session) await session.detach().catch(() => {});
    }
  }
}

const PRINT_SUPPRESSION_MARKER = 'data-trademark-evidence-print-suppressed';

function loadPdfTailQualityPolicy() {
  const policyPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'runtime-policy.json');
  const policy = JSON.parse(readFileSync(policyPath, 'utf8'));
  const row = policy?.pdf?.tail_quality;
  const values = {
    maxTextChars: Number(row?.max_text_chars),
    maxNonwhiteRatio: Number(row?.max_nonwhite_ratio),
    maxLargestRasterAreaRatio: Number(row?.max_largest_raster_area_ratio),
  };
  if (!Number.isInteger(values.maxTextChars) || values.maxTextChars < 0
      || !Number.isFinite(values.maxNonwhiteRatio)
      || values.maxNonwhiteRatio < 0 || values.maxNonwhiteRatio > 1
      || !Number.isFinite(values.maxLargestRasterAreaRatio)
      || values.maxLargestRasterAreaRatio < 0 || values.maxLargestRasterAreaRatio > 1) {
    throw new Error('runtime-policy.json pdf.tail_quality is invalid');
  }
  return values;
}

const PDF_TAIL_QUALITY_POLICY = loadPdfTailQualityPolicy();
const PDF_TAIL_TEXT_THRESHOLD = PDF_TAIL_QUALITY_POLICY.maxTextChars;
const PDF_TAIL_NONWHITE_THRESHOLD = PDF_TAIL_QUALITY_POLICY.maxNonwhiteRatio;
const PDF_TAIL_MAX_RASTER_AREA_RATIO = PDF_TAIL_QUALITY_POLICY.maxLargestRasterAreaRatio;

async function suppressUnsafeFixedPrintElements(page, resultSelectors) {
  return page.evaluate(({ marker, selectors }) => {
    const restoreKey = '__trademarkEvidencePrintRestore';
    const prior = [];
    let examinedFixedElements = 0;
    let protectedResultContainers = 0;
    const suppressed = [];

    for (const element of document.querySelectorAll('body *')) {
      const computed = getComputedStyle(element);
      if (computed.position !== 'fixed') continue;
      examinedFixedElements += 1;
      const rect = element.getBoundingClientRect();
      const zeroSized = rect.width <= 1 || rect.height <= 1;
      const clearlyNegative = rect.right <= -128 || rect.bottom <= -128
        || rect.left <= -1000 || rect.top <= -1000;
      if (!zeroSized && !clearlyNegative) continue;

      const containsSearchResult = selectors.some((selector) => {
        try {
          return element.matches(selector) || Boolean(element.querySelector(selector));
        } catch {
          return false;
        }
      });
      if (containsSearchResult) {
        protectedResultContainers += 1;
        continue;
      }

      prior.push({ element, style: element.getAttribute('style'), marker: element.getAttribute(marker) });
      element.setAttribute(marker, 'true');
      element.style.setProperty('display', 'none', 'important');
      element.style.setProperty('visibility', 'hidden', 'important');
      element.style.setProperty('pointer-events', 'none', 'important');
      suppressed.push({
        tag: element.tagName.toLowerCase(),
        id: element.id || null,
        width: Math.round(rect.width * 100) / 100,
        height: Math.round(rect.height * 100) / 100,
        left: Math.round(rect.left * 100) / 100,
        top: Math.round(rect.top * 100) / 100,
        reason: [zeroSized ? 'fixed_zero_size' : null, clearlyNegative ? 'fixed_clearly_negative' : null]
          .filter(Boolean),
      });
    }
    window[restoreKey] = prior;
    return {
      strategy: 'temporarily_hide_only_zero_size_or_clearly_negative_fixed_elements_v1',
      examined_fixed_elements: examinedFixedElements,
      protected_result_containers: protectedResultContainers,
      suppressed_element_count: suppressed.length,
      suppressed_elements: suppressed.slice(0, 50),
    };
  }, { marker: PRINT_SUPPRESSION_MARKER, selectors: resultSelectors });
}

async function restoreUnsafeFixedPrintElements(page) {
  return page.evaluate(({ marker }) => {
    const restoreKey = '__trademarkEvidencePrintRestore';
    const prior = Array.isArray(window[restoreKey]) ? window[restoreKey] : [];
    let restored = 0;
    for (const item of prior) {
      const element = item?.element;
      if (!element?.isConnected) continue;
      if (item.style === null) element.removeAttribute('style');
      else element.setAttribute('style', item.style);
      if (item.marker === null) element.removeAttribute(marker);
      else element.setAttribute(marker, item.marker);
      restored += 1;
    }
    delete window[restoreKey];
    return { restored_element_count: restored };
  }, { marker: PRINT_SUPPRESSION_MARKER });
}

function trimPdfEmptyTail(pdfPath) {
  const python = process.env.PYTHON_EXECUTABLE || (process.platform === 'win32' ? 'python' : 'python3');
  const trimScript = path.join(path.dirname(fileURLToPath(import.meta.url)), 'trim-pdf-empty-tail.py');
  const command = [
    trimScript, '--pdf', pdfPath,
    '--max-text-chars', String(PDF_TAIL_TEXT_THRESHOLD),
    '--max-nonwhite-ratio', String(PDF_TAIL_NONWHITE_THRESHOLD),
    '--max-largest-raster-area-ratio', String(PDF_TAIL_MAX_RASTER_AREA_RATIO),
    '--min-pages', '1',
  ];
  const completed = spawnSync(python, command, {
    encoding: 'utf8', timeout: 60_000, maxBuffer: 4 * 1024 * 1024,
  });
  if (completed.error) throw new Error(`PDF tail cleanup could not start: ${completed.error.message}`);
  if (completed.status !== 0) {
    const detail = (completed.stderr || completed.stdout || `exit ${completed.status}`).trim();
    throw new Error(`PDF tail cleanup failed: ${detail}`);
  }
  let result;
  try {
    result = JSON.parse(completed.stdout.trim());
  } catch (error) {
    throw new Error(`PDF tail cleanup returned invalid JSON: ${error.message}`);
  }
  const original = Number(result.original_pages);
  const retained = Number(result.kept_pages);
  const removed = Number(result.trimmed_pages);
  if (result.ok !== true || !Number.isInteger(original) || !Number.isInteger(retained)
      || !Number.isInteger(removed) || original < 1 || retained < 1
      || retained > original || removed !== original - retained) {
    throw new Error('PDF tail cleanup returned inconsistent page counts');
  }
  return {
    ...result,
    original_page_count: original,
    retained_page_count: retained,
    removed_page_count: removed,
    removed_page_numbers: removed ? Array.from({ length: removed }, (_, index) => retained + index + 1) : [],
    reason: removed
      ? 'removed_only_consecutive_trailing_pages_below_both_text_and_nonwhite_thresholds'
      : 'no_consecutive_low_information_trailing_pages_detected',
    tool: path.basename(trimScript),
  };
}

function queryTerms(query) {
  return String(query || '')
    .replace(/["“”'‘’]/g, ' ')
    .split(/[\s,+|/]+/)
    .map((item) => item.trim().toLowerCase())
    .filter((item) => item.length >= 2
      && !['site', 'www', 'http', 'https'].includes(item)
      && !item.startsWith('site:'));
}

function relevantToQuery(item, query) {
  const terms = queryTerms(query);
  if (!terms.length) return true;
  const sample = `${item.title || ''}\n${item.snippet || ''}\n${item.url || ''}`.normalize('NFKC').toLowerCase();
  return terms.some((term) => sample.includes(term));
}

let chromium;
try {
  ({ chromium } = require('playwright-core'));
} catch {
  ({ chromium } = require('playwright'));
}

const args = parseArgs(process.argv.slice(2));
if (!args.query || !args.provider || !args['output-dir']) {
  console.error('Usage: discover-search-results.mjs --query <text> --provider <bing|so360|sogou|yahoo|baidu> --output-dir <dir> [--headed --wait-for-unblock-ms 120000]');
  process.exit(1);
}

const limit = Math.max(1, Number.parseInt(args.limit || '20', 10));
const timeout = Math.max(10_000, Number.parseInt(args['timeout-ms'] || '45000', 10));
const parsedWaitForUnblockMs = Number.parseInt(args['wait-for-unblock-ms'] || '0', 10);
const waitForUnblockMs = Number.isFinite(parsedWaitForUnblockMs) ? Math.max(0, parsedWaitForUnblockMs) : 0;
if (waitForUnblockMs > 0 && !args.headed) {
  console.error('--wait-for-unblock-ms requires --headed');
  process.exit(1);
}
const outputDir = path.resolve(args['output-dir']);
const artifactLockPath = args['artifact-lock'] ? path.resolve(args['artifact-lock']) : null;
const screenshotBackend = args['screenshot-backend'] === 'cdp' ? 'cdp' : 'auto';
const browserExecutable = findBrowserExecutable(args['browser-executable']);
const cdpEndpoint = args['cdp-endpoint'] ? String(args['cdp-endpoint']).trim() : '';
if (cdpEndpoint) {
  let parsed;
  try { parsed = new URL(cdpEndpoint); } catch { throw new Error('Invalid --cdp-endpoint URL'); }
  if (!['http:', 'https:'].includes(parsed.protocol)
      || !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(parsed.hostname)) {
    throw new Error('--cdp-endpoint must use an HTTP(S) loopback address');
  }
}
const normalizedQuery = normalizeQuery(args.query);
if (!normalizedQuery) {
  console.error('Search query cannot be empty');
  process.exit(1);
}
const config = providerConfig(args.provider, normalizedQuery);
await fs.mkdir(outputDir, { recursive: true });

const report = {
  schema_version: '2.0',
  record_type: 'discovery_provider_run',
  query_id: args['query-id'] || null,
  query: normalizedQuery,
  provider: args.provider,
  search_url: config.url,
  captured_at: new Date().toISOString(),
  state: 'error',
  title: null,
  visible_text_chars: 0,
  raw_result_count: 0,
  result_count: 0,
  interactive_search: {
    headed: Boolean(args.headed), wait_for_unblock_ms: waitForUnblockMs,
    attached_to_existing_browser: Boolean(cdpEndpoint),
  },
  results: [],
  errors: [],
};

let browser;
let context;
let page;
let attachedToExistingBrowser = false;
let preserveAttachedPage = false;
async function targetIdForPage(activeContext, activePage) {
  try {
    const session = await activeContext.newCDPSession(activePage);
    const value = await session.send('Target.getTargetInfo');
    await session.detach().catch(() => {});
    return value?.targetInfo?.targetId || null;
  } catch {
    return null;
  }
}
try {
  if (cdpEndpoint) {
    browser = await chromium.connectOverCDP(cdpEndpoint);
    context = browser.contexts()[0];
    if (!context) throw new Error('No browser context is available from the CDP endpoint');
    attachedToExistingBrowser = true;
  } else {
    if (!browserExecutable) throw new Error('Chrome/Edge executable not found');
    browser = await chromium.launch({ executablePath: browserExecutable, headless: !args.headed });
    context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  }
  page = await context.newPage();
  let response;
  try {
    response = await page.goto(config.url, { waitUntil: 'domcontentloaded', timeout });
  } catch (error) {
    report.errors.push({ stage: 'navigation', message: String(error?.message || error) });
  }
  await page.waitForLoadState('networkidle', { timeout: Math.min(10_000, timeout) }).catch(() => {});
  await page.waitForTimeout(800);
  report.http_status = response?.status() ?? null;
  report.final_url = page.url();
  report.title = await page.title().catch(() => '');
  let visibleText = await page.locator('body').innerText({ timeout: 5000 }).catch(() => '');
  report.visible_text_chars = visibleText.length;
  report.state = classify(visibleText, report.title);
  if (args.headed && waitForUnblockMs > 0 && ['captcha', 'login_required'].includes(report.state)) {
    const deadline = Date.now() + waitForUnblockMs;
    while (Date.now() < deadline) {
      await page.waitForTimeout(Math.min(2_000, Math.max(1, deadline - Date.now())));
      report.title = await page.title().catch(() => report.title || '');
      visibleText = await page.locator('body').innerText({ timeout: 5000 }).catch(() => visibleText);
      report.visible_text_chars = visibleText.length;
      report.state = classify(visibleText, report.title);
      if (!['captcha', 'login_required'].includes(report.state)) break;
    }
  }

  if (attachedToExistingBrowser && ['captcha', 'login_required'].includes(report.state)) {
    preserveAttachedPage = true;
    await page.bringToFront().catch(() => {});
    report.manual_verification_tab_kept_open = true;
    report.verification_page = {
      provider: args.provider, query_id: report.query_id,
      url: page.url(), title: report.title,
      target_id: await targetIdForPage(context, page),
    };
  }

  const artifactLease = await acquireArtifactLock(artifactLockPath);
  report.artifact_capture_lock_wait_ms = artifactLease.waited_ms;
  try {
    await fs.writeFile(path.join(outputDir, 'serp.html'), await page.content(), 'utf8');
    report.artifacts = { ...(report.artifacts || {}), html: 'serp.html' };
    const screenshotPath = path.join(outputDir, 'serp.png');
    report.screenshot_capture = await captureScreenshot(page, screenshotPath, screenshotBackend);
    if (!report.screenshot_capture.ok) {
      report.errors.push({
        stage: 'screenshot',
        message: `${report.screenshot_capture.primary_error}; fallback: ${report.screenshot_capture.fallback_error}`,
      });
    } else if (existsSync(screenshotPath)) {
      report.artifacts.screenshot = 'serp.png';
    }
    if (['normal', 'zero_results'].includes(report.state)) {
    const pdfPath = path.join(outputDir, 'serp.pdf');
    let cleanupApplied = false;
    let pdfFailure = null;
    try {
      await page.emulateMedia({ media: 'screen' });
      report.print_layout_cleanup = await suppressUnsafeFixedPrintElements(page, config.selectors);
      cleanupApplied = true;
      await page.pdf({
        path: pdfPath, format: 'A4', printBackground: true,
        margin: { top: '6mm', right: '5mm', bottom: '12mm', left: '5mm' },
        preferCSSPageSize: false,
      });
    } catch (error) {
      pdfFailure = { stage: 'pdf', message: String(error?.message || error) };
    } finally {
      if (cleanupApplied) {
        try {
          report.print_layout_restore = await restoreUnsafeFixedPrintElements(page);
        } catch (error) {
          report.errors.push({ stage: 'print_layout_restore', message: String(error?.message || error) });
        }
      }
    }

    if (!pdfFailure) {
      try {
        report.pdf_cleanup = trimPdfEmptyTail(pdfPath);
        report.artifacts.pdf = 'serp.pdf';
      } catch (error) {
        pdfFailure = { stage: 'pdf_tail_cleanup', message: String(error?.message || error) };
      }
    }
    if (pdfFailure) {
      await fs.rm(pdfPath, { force: true }).catch(() => {});
      delete report.artifacts.pdf;
      report.observed_page_state = report.state;
      report.state = 'artifact_invalid';
      report.pdf_cleanup = {
        ok: false,
        original_page_count: null,
        retained_page_count: null,
        removed_page_count: null,
        removed_page_numbers: [],
        reason: 'pdf_generation_or_mandatory_tail_cleanup_failed',
      };
      report.errors.push(pdfFailure);
    }
    }
  } finally {
    await artifactLease.release();
  }

  const collected = [];
  for (const selector of config.selectors) {
    const found = await page.locator(selector).evaluateAll((anchors) => anchors.map((anchor) => {
      const container = anchor.closest('li, article, section, .res-list, .algo, .result, div');
      const rect = anchor.getBoundingClientRect();
      return {
        href: anchor.href || anchor.getAttribute('href'),
        data_url: anchor.getAttribute('data-url') || anchor.getAttribute('data-mdurl') || anchor.getAttribute('data-href'),
        data_landurl: anchor.getAttribute('data-landurl') || anchor.closest('[data-landurl]')?.getAttribute('data-landurl'),
        mu: anchor.getAttribute('mu') || anchor.closest('[mu]')?.getAttribute('mu'),
        title: (anchor.innerText || anchor.textContent || '').trim().replace(/\s+/g, ' '),
        snippet: (container?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 1200),
        visible: rect.width > 0 && rect.height > 0,
      };
    })).catch(() => []);
    collected.push(...found);
  }
  if (collected.filter((item) => item.visible).length < 2) {
    const generic = await page.locator('a[href]').evaluateAll((anchors) => anchors.map((anchor) => {
      const rect = anchor.getBoundingClientRect();
      const container = anchor.closest('li, article, section, div');
      return {
        href: anchor.href || anchor.getAttribute('href'),
        data_url: anchor.getAttribute('data-url') || anchor.getAttribute('data-mdurl') || anchor.getAttribute('data-href'),
        data_landurl: anchor.getAttribute('data-landurl') || anchor.closest('[data-landurl]')?.getAttribute('data-landurl'),
        mu: anchor.getAttribute('mu') || anchor.closest('[mu]')?.getAttribute('mu'),
        title: (anchor.innerText || anchor.textContent || '').trim().replace(/\s+/g, ' '),
        snippet: (container?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 1200),
        visible: rect.width > 0 && rect.height > 0,
      };
    })).catch(() => []);
    collected.push(...generic);
  }

  const seen = new Set();
  const rawSeen = new Set();
  const rawAnchorSeen = new Set();
  for (const item of collected) {
    if (!item.visible || item.title.length < 2) continue;
    const rawKey = `${item.href || ''}\n${item.title}`;
    if (!rawAnchorSeen.has(rawKey)) {
      rawAnchorSeen.add(rawKey);
      report.raw_result_count += 1;
    }
    if (!relevantToQuery({ ...item, url: '' }, normalizedQuery)) continue;
    let decoded = decodeRedirect(item.data_landurl || item.mu || item.data_url || item.href);
    if (!decoded || rawSeen.has(decoded)) continue;
    rawSeen.add(decoded);
    decoded = await resolveProviderRedirect(page, args.provider, decoded);
    if (!decoded || !/^https?:\/\//i.test(decoded) || providerOwned(decoded, config.hosts)) continue;
    let clean;
    try {
      const url = new URL(decoded);
      url.hash = '';
      if (/\.(?:js|css|woff2?|ttf|map)$/i.test(url.pathname)) continue;
      clean = url.toString();
    } catch { continue; }
    if (!relevantToQuery({ ...item, url: clean }, normalizedQuery)) continue;
    if (seen.has(clean)) continue;
    seen.add(clean);
    report.results.push({ rank: report.results.length + 1, title: item.title.slice(0, 500), snippet: item.snippet, url: clean });
    if (report.results.length >= limit) break;
  }
  report.result_count = report.results.length;
  if (report.state === 'normal' && report.result_count === 0) {
    if (report.raw_result_count > 0) report.extraction_status = 'normal_page_no_relevant_direct_urls';
    else report.state = 'no_extractable_results';
  }
} catch (error) {
  report.errors.push({ stage: 'fatal', message: String(error?.message || error) });
} finally {
  if (page && !preserveAttachedPage) await page.close().catch(() => {});
  if (!attachedToExistingBrowser && context) await context.close().catch(() => {});
  if (!attachedToExistingBrowser && browser) await browser.close().catch(() => {});
  report.artifact_integrity = {};
  for (const [key, filename] of Object.entries(report.artifacts || {})) {
    const artifactPath = path.join(outputDir, filename);
    try {
      const payload = await fs.readFile(artifactPath);
      report.artifact_integrity[key] = {
        path: filename,
        size_bytes: payload.length,
        sha256: createHash('sha256').update(payload).digest('hex'),
      };
    } catch {
      // Required-artifact validation in the parent process will reject it.
    }
  }
  await fs.writeFile(path.join(outputDir, 'results.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
}

console.log(JSON.stringify(report, null, 2));
const finalExitCode = ['normal', 'zero_results'].includes(report.state) ? 0 : 3;
if (cdpEndpoint) {
  await new Promise((resolve) => process.stdout.write('', resolve));
  process.exit(finalExitCode);
}
process.exitCode = finalExitCode;
