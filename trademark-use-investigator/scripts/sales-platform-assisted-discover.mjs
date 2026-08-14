#!/usr/bin/env node

import fs from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import {
  captureViewportScreenshot,
  captureViewportTiles,
  withOperationTimeout,
} from './viewport-tile-capture.mjs';
import { acquireArtifactLock } from './browser-artifact-lock.mjs';
import { roundRobinByKey } from './controlled-search-scheduler.mjs';
import {
  applyRiskStrike,
  markRiskProbeAttempt,
  migrateLegacyFirstStrike,
  rearmInconclusiveProbe,
  resolvePacketEligibleSuccess,
  riskGate,
  validateRiskCircuitBreakerPolicy,
} from './sales-risk-cooldown.mjs';
import {
  canonicalPlatformSearchUrl,
  canonicalTaobaoSearchUrl,
  normalizeSearchQuery,
  summarizePlatformEligibility,
  verifyQueryBinding,
} from './sales-query-binding.mjs';
import { computeQueryPacing } from './sales-request-pacing.mjs';
import { writeTextAtomic } from './atomic-text-write.mjs';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright-core')); }
catch { ({ chromium } = require('playwright')); }

const PLATFORM_CONFIG = {
  taobao: {
    label: '淘宝', domains: ['taobao.com'],
    home: 'https://www.taobao.com/',
    searchUrl: canonicalTaobaoSearchUrl,
    searchSelectors: ['#q', 'input[name="q"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['button.next-next', 'a.next-next', '[aria-label="下一页"]', 'button:has-text("下一页")'],
    currentPageSelectors: ['.next-pagination-item.next-current', '[aria-current="page"]'],
  },
  tmall: {
    label: '天猫', domains: ['tmall.com'],
    home: 'https://www.tmall.com/',
    searchSelectors: ['#mq', 'input[name="q"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['button.next-next', 'a.next-next', '[aria-label="下一页"]', 'button:has-text("下一页")'],
    currentPageSelectors: ['.next-pagination-item.next-current', '[aria-current="page"]'],
  },
  jd: {
    label: '京东', domains: ['jd.com'],
    home: 'https://www.jd.com/',
    reuseSearchResultForm: true,
    searchSelectors: [
      '#key', '.jd_pc_search_bar_react_search_input', 'input[aria-label="搜索"]',
      'input[name="keyword"]', 'input[placeholder*="搜索"]',
    ],
    keyboardInput: true,
    submitWithEnter: true,
    submitSelectors: ['.jd_pc_search_bar_react_search_btn', 'button:has-text("搜索")'],
    nextSelectors: ['a.pn-next', '.p-num a.pn-next', '[aria-label="下一页"]', 'a:has-text("下一页")'],
    currentPageSelectors: ['.p-num a.curr', '.p-num .curr', '[aria-current="page"]'],
  },
  '1688': {
    label: '1688', domains: ['1688.com'],
    home: 'https://www.1688.com/',
    searchSelectors: ['#alisearch-keywords', 'input[name="keywords"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['.fui-next', '.pagination-next', '[aria-label="下一页"]', 'a:has-text("下一页")'],
    currentPageSelectors: ['.fui-pagination-num-active', '[class*="pagination"] [class*="active"]', '[aria-current="page"]'],
  },
  pinduoduo: {
    label: '拼多多', domains: ['pinduoduo.com'],
    home: 'https://www.pinduoduo.com/',
    searchSelectors: ['input[name="search_key"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['[aria-label="下一页"]', 'button:has-text("下一页")', 'a:has-text("下一页")'],
    currentPageSelectors: ['[aria-current="page"]', '[class*="pagination"] [class*="active"]'],
  },
  suning: {
    label: '苏宁易购', domains: ['suning.com'],
    home: 'https://www.suning.com/',
    searchSelectors: ['#searchKeywords', 'input[name="keyword"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['a.next', '.next', '[aria-label="下一页"]', 'a:has-text("下一页")'],
    currentPageSelectors: ['.search-page .current', '[aria-current="page"]'],
  },
  dangdang: {
    label: '当当', domains: ['dangdang.com'],
    home: 'https://www.dangdang.com/',
    searchSelectors: ['#key_S', 'input[name="key"]', 'input[placeholder*="搜索"]'],
    nextSelectors: ['li.next a', 'a.next', '[aria-label="下一页"]', 'a:has-text("下一页")'],
    currentPageSelectors: ['.paging .current', '.page .current', '[aria-current="page"]'],
  },
};

const RUNTIME_POLICY = JSON.parse(
  readFileSync(new URL('./runtime-policy.json', import.meta.url), 'utf8'),
);
function validateRatePolicy(policy) {
  if (!policy || typeof policy !== 'object' || !policy.default) {
    throw new Error('runtime-policy.json must define sales_rate_limit.default');
  }
  for (const [name, row] of Object.entries(policy)) {
    const minimum = Number(row?.min_delay_ms);
    const maximum = Number(row?.max_delay_ms);
    const legacyCooldown = Number(row?.legacy_fixed_cooldown_minutes);
    if (!Number.isFinite(minimum) || minimum < 0 || !Number.isFinite(maximum) || maximum < minimum || !Number.isFinite(legacyCooldown) || legacyCooldown < 0) {
      throw new Error(`runtime-policy.json has an invalid sales_rate_limit entry: ${name}`);
    }
    if (row.batch_size !== undefined) {
      const pauseMinimum = Number(row.batch_pause_min_ms);
      const pauseMaximum = Number(row.batch_pause_max_ms);
      if (!Number.isInteger(row.batch_size) || row.batch_size <= 0 || !Number.isFinite(pauseMinimum) || pauseMinimum < 0 || !Number.isFinite(pauseMaximum) || pauseMaximum < pauseMinimum) {
        throw new Error(`runtime-policy.json has an invalid batch policy entry: ${name}`);
      }
    }
    if (row.delay_from_task_finish !== undefined && typeof row.delay_from_task_finish !== 'boolean') {
      throw new Error(`runtime-policy.json has an invalid task-finish delay anchor: ${name}`);
    }
    const initialSettleMinimum = row.initial_settle_min_ms;
    const initialSettleMaximum = row.initial_settle_max_ms;
    if ((initialSettleMinimum === undefined) !== (initialSettleMaximum === undefined)) {
      throw new Error(`runtime-policy.json has an incomplete initial settle range: ${name}`);
    }
    if (initialSettleMinimum !== undefined && (
      !Number.isFinite(initialSettleMinimum) || initialSettleMinimum < 0
      || !Number.isFinite(initialSettleMaximum) || initialSettleMaximum < initialSettleMinimum
    )) {
      throw new Error(`runtime-policy.json has an invalid initial settle range: ${name}`);
    }
    const postBatchMinimum = row.post_batch_cooldown_min_minutes;
    const postBatchMaximum = row.post_batch_cooldown_max_minutes;
    if ((postBatchMinimum === undefined) !== (postBatchMaximum === undefined)) {
      throw new Error(`runtime-policy.json has an incomplete post-batch cooldown range: ${name}`);
    }
    if (postBatchMinimum !== undefined && (
      !Number.isInteger(postBatchMinimum) || postBatchMinimum < 0
      || !Number.isInteger(postBatchMaximum) || postBatchMaximum < postBatchMinimum
    )) {
      throw new Error(`runtime-policy.json has an invalid post-batch cooldown range: ${name}`);
    }
  }
  return policy;
}
const RATE_LIMIT_POLICY = validateRatePolicy(RUNTIME_POLICY.sales_rate_limit);
const RISK_CIRCUIT_BREAKER_POLICY = validateRiskCircuitBreakerPolicy(
  RUNTIME_POLICY.sales_risk_circuit_breaker,
);

function ratePolicy(platform) {
  return RATE_LIMIT_POLICY[platform] || RATE_LIMIT_POLICY.default;
}

function randomBetween(minimum, maximum) {
  return Math.floor(minimum + Math.random() * (maximum - minimum + 1));
}

function queryDelayPlan(policy, platformState) {
  return computeQueryPacing(policy, platformState);
}

function isHumanVerificationState(value) {
  return /captcha|login_required/i.test(String(value || ''));
}

function isRiskCircuitBreakerState(value) {
  return /access_denied|rate_limited|访问频繁|频繁访问/i.test(String(value || ''));
}

function isBlockingState(value) {
  return isHumanVerificationState(value) || isRiskCircuitBreakerState(value);
}

function sanitizedObservationUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  } catch {
    return null;
  }
}

function parseArgs(argv) {
  const output = { platform: [], 'query-id': [] };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith('--')) continue;
    const key = item.slice(2);
    const next = argv[index + 1];
    const value = next && !next.startsWith('--') ? next : true;
    if (value !== true) index += 1;
    if (key === 'platform') output.platform.push(value);
    else if (key === 'query-id') output['query-id'].push(value);
    else output[key] = value;
  }
  return output;
}

function findBrowserExecutable(explicit, browser = 'edge') {
  if (explicit) return existsSync(explicit) ? explicit : null;
  const edgeCandidates = [
    process.env.EDGE_EXECUTABLE_PATH,
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.PROGRAMFILES || 'C:\\Program Files', 'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Microsoft/Edge/Application/msedge.exe'),
  ];
  const chromeCandidates = [
    process.env.CHROME_EXECUTABLE_PATH,
    path.join(process.env.PROGRAMFILES || 'C:\\Program Files', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google/Chrome/Application/chrome.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
  ];
  const candidates = [
    ...(browser === 'chrome' ? chromeCandidates : edgeCandidates),
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

function browserProductFromExecutable(executable) {
  const name = path.basename(String(executable || '')).toLowerCase();
  if (name.includes('msedge')) return 'edge';
  if (name.includes('chrome')) return 'chrome';
  return null;
}

function cleanHost(value) {
  const host = String(value || '').toLowerCase().replace(/\.$/, '');
  return host.startsWith('www.') ? host.slice(4) : host;
}

function hostMatches(host, domain) {
  host = cleanHost(host); domain = cleanHost(domain);
  return host === domain || host.endsWith(`.${domain}`);
}

function platformPageKind(platform, rawUrl) {
  let url;
  try { url = new URL(rawUrl); } catch { return null; }
  const host = cleanHost(url.hostname);
  const pathname = url.pathname.toLowerCase();
  const query = url.search.toLowerCase();
  if (platform === 'taobao') {
    if (pathname.includes('item.htm') || pathname.includes('/list/item/')) return 'product';
    if (/^shop\d+\.taobao\.com$/.test(host) || pathname.startsWith('/shop/')) return 'shop';
  } else if (platform === 'tmall') {
    if (hostMatches(host, 'detail.tmall.com') && (pathname.includes('item.htm') || query.includes('id='))) return 'product';
    if (host.includes('shop') || pathname.includes('/shop')) return 'shop';
  } else if (platform === 'jd') {
    if (hostMatches(host, 'item.jd.com') && /\/\d+\.html$/.test(pathname)) return 'product';
    if (['/chanpin/', '/hprm/', '/phb/'].some((token) => pathname.includes(token))) return 'category';
    if (hostMatches(host, 'mall.jd.com') || hostMatches(host, 'shop.jd.com')) return 'shop';
  } else if (platform === '1688') {
    if (hostMatches(host, 'detail.1688.com') && pathname.includes('/offer/')) return 'product';
    if (hostMatches(host, '1688.com') && query.includes('offerid=')) return 'product';
    if (pathname.includes('/offer/') || pathname.includes('/shop/')) return 'shop_or_product';
    if (pathname.includes('/brand/') || pathname.includes('/chanpin/')) return 'category';
  } else if (platform === 'pinduoduo') {
    if (query.includes('goods_id=') || pathname.includes('/goods')) return 'product';
  } else if (platform === 'suning') {
    if (pathname.includes('/item/')) return 'product';
    if (pathname.includes('/shop/')) return 'shop';
  } else if (platform === 'dangdang') {
    if (hostMatches(host, 'product.dangdang.com') && pathname.endsWith('.html')) return 'product';
  }
  return null;
}

function classify(text, title, url) {
  const sample = `${title}\n${url}\n${String(text || '').slice(0, 100000)}`;
  if (/安全验证|验证码|滑块|captcha|qcaptcha|risk_handler|punish/i.test(sample)) return 'captcha';
  if (/拒绝访问|访问受限|访问频繁导致无法搜索|访问过于频繁|频繁访问|access denied|forbidden|请求异常|操作过于频繁/i.test(sample)) return 'access_denied';
  if (/登录后继续|扫码登录|请输入登录密码|login required/i.test(sample)) return 'login_required';
  return 'normal';
}

function canonicalUrl(raw) {
  const url = new URL(raw);
  url.hash = '';
  const keep = new Set(['id', 'skuId', 'goods_id', 'itemId', 'productId', 'offerId']);
  for (const key of [...url.searchParams.keys()]) if (!keep.has(key)) url.searchParams.delete(key);
  return url.toString();
}

async function pageState(page) {
  const title = await page.title().catch(() => '');
  const frameSamples = await Promise.all(page.frames().map(async (frame) => ({
    url: frame.url(),
    text: await frame.locator('body').innerText({ timeout: 3000 }).catch(() => ''),
  })));
  const text = frameSamples.map((item) => `${item.url}\n${item.text}`).join('\n');
  return {
    title, text, state: classify(text, title, page.url()), url: page.url(),
    frame_count: frameSamples.length,
  };
}

async function findSearchInput(page, selectors) {
  for (const selector of selectors) {
    const matches = page.locator(selector);
    const count = await matches.count().catch(() => 0);
    for (let index = 0; index < Math.min(count, 5); index += 1) {
      const candidate = matches.nth(index);
      if (await candidate.isVisible().catch(() => false)) return { locator: candidate, selector };
    }
  }
  return null;
}

async function searchFromHomepage(page, platformConfig, query, expectedSearchUrl, timeoutMs) {
  const result = {
    navigation_mode: 'homepage_search_form',
    home_url: platformConfig.home,
    search_input_selector: null,
    submitted: false,
    submission_verified: false,
  };
  if (typeof platformConfig.searchUrl === 'function') {
    result.navigation_mode = 'direct_search_url';
    const targetUrl = platformConfig.searchUrl(query);
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 8000) }).catch(() => {});
    await page.waitForTimeout(1800);
    const directState = await pageState(page);
    result.home_state = directState.state;
    result.home_final_url = platformConfig.home;
    result.search_input_value = query;
    result.submitted = true;
    result.submitted_url = page.url();
    result.query_binding = await verifyQueryBinding(page, platformConfig, query, expectedSearchUrl);
    result.submitted_query_param = result.query_binding.actual_url_query_value;
    result.submitted_query_matches = result.query_binding.url_query_matches;
    result.submission_verified = directState.state === 'normal'
      && page.url() !== platformConfig.home && result.query_binding.verified;
    if (!result.submission_verified) {
      result.error = directState.state === 'normal'
        ? 'query_binding_failed'
        : `direct_search_${directState.state}`;
    }
    result.resultPage = page;
    return result;
  }
  let homeState = null;
  let found = null;
  if (platformConfig.reuseSearchResultForm) {
    let currentHost = '';
    try { currentHost = new URL(page.url()).hostname; } catch {}
    if (platformConfig.domains.some((domain) => hostMatches(currentHost, domain))) {
      const currentState = await pageState(page);
      if (currentState.state === 'normal') {
        found = await findSearchInput(page, platformConfig.searchSelectors);
        if (found) {
          result.navigation_mode = 'existing_search_result_form';
          result.reused_existing_result_page = true;
          homeState = currentState;
        }
      }
    }
  }
  if (!homeState) {
    await page.goto(platformConfig.home, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 8000) }).catch(() => {});
    await page.waitForTimeout(1200);
    homeState = await pageState(page);
    result.reused_existing_result_page = false;
  }
  result.home_state = homeState.state;
  result.home_final_url = homeState.url;
  if (homeState.state !== 'normal') return result;
  if (!found) found = await findSearchInput(page, platformConfig.searchSelectors);
  if (!found) {
    result.error = 'visible_search_input_not_found';
    return result;
  }
  result.search_input_selector = found.selector;
  try {
    if (platformConfig.keyboardInput) {
      await found.locator.click({ timeout: 5000 });
      await page.keyboard.press('Control+A');
      await page.keyboard.type(query, { delay: 90 });
      await page.waitForTimeout(400);
    } else {
      await found.locator.fill(query, { timeout: 5000 });
    }
  } catch {
    await found.locator.evaluate((element, value) => {
      const prototype = element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      if (setter) setter.call(element, value);
      else element.value = value;
      element.focus();
      element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
    }, query);
  }
  result.search_input_value = await found.locator.inputValue().catch(() => '');
  const beforeSubmitUrl = page.url();
  await found.locator.evaluate((element) => {
    const form = element.form || element.closest('form');
    if (form) {
      form.removeAttribute('target');
      form.target = '_self';
    }
    element.focus();
  });
  const popupPromise = page.waitForEvent('popup', { timeout: Math.min(timeoutMs, 5000) }).catch(() => null);
  const submitButton = !platformConfig.submitWithEnter && platformConfig.submitSelectors
    ? await findSearchInput(page, platformConfig.submitSelectors)
    : null;
  if (platformConfig.submitWithEnter) {
    result.search_submit_selector = 'keyboard:Enter';
    await page.keyboard.press('Enter');
  } else if (submitButton) {
    result.search_submit_selector = submitButton.selector;
    await submitButton.locator.click({ timeout: 5000 });
  } else {
    await page.keyboard.press('Enter');
  }
  const popup = await popupPromise;
  let resultPage = popup && !popup.isClosed() ? popup : page;
  result.submitted = true;
  await resultPage.waitForURL((url) => resultPage !== page || url.href !== beforeSubmitUrl, {
    timeout: Math.min(timeoutMs, 15000),
  }).catch(() => {});
  await resultPage.waitForLoadState('domcontentloaded', { timeout: Math.min(timeoutMs, 12000) }).catch(() => {});
  await resultPage.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 8000) }).catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 1800));
  if (resultPage.isClosed()) {
    resultPage = page;
  }
  result.submitted_url = resultPage.url();
  result.opened_new_tab = resultPage !== page;
  result.query_binding = await verifyQueryBinding(resultPage, platformConfig, query, expectedSearchUrl);
  result.submission_verified = (resultPage !== page || result.submitted_url !== beforeSubmitUrl)
    && result.query_binding.verified;
  if (!result.submission_verified) {
    result.error = result.query_binding.verified
      ? 'search_submission_did_not_leave_homepage'
      : 'query_binding_failed';
  }
  result.resultPage = resultPage;
  return result;
}

async function hydrateLazyImages(page, options = {}) {
  const maxSteps = Math.max(4, Math.min(30, Number(options.maxSteps || 24)));
  const delayMs = Math.max(120, Math.min(800, Number(options.delayMs || 280)));
  const collect = async (promote = true) => page.evaluate(({ shouldPromote }) => {
    const lazyAttrs = ['data-src', 'data-lazy-src', 'data-ks-lazyload', 'data-original', 'data-img'];
    let promoted = 0;
    let promotedSrcset = 0;
    if (shouldPromote) {
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
    }
    const entries = [...document.images].map((img) => {
      const rect = img.getBoundingClientRect();
      const declaredWidth = Number.parseFloat(img.getAttribute('width') || '0');
      const declaredHeight = Number.parseFloat(img.getAttribute('height') || '0');
      const substantive = Math.max(rect.width, declaredWidth, img.naturalWidth) >= 60
        && Math.max(rect.height, declaredHeight, img.naturalHeight) >= 45;
      return { substantive, loaded: img.complete && img.naturalWidth > 1 && img.naturalHeight > 1 };
    });
    const substantive = entries.filter((item) => item.substantive).length;
    const loaded = entries.filter((item) => item.substantive && item.loaded).length;
    return {
      total: entries.length, substantive, loaded,
      unresolved_substantive: Math.max(0, substantive - loaded),
      promoted, promoted_srcset: promotedSrcset,
      height: Math.max(document.scrollingElement?.scrollHeight || 0, document.body?.scrollHeight || 0),
      viewport: window.innerHeight || 1,
      y: window.scrollY || document.scrollingElement?.scrollTop || 0,
    };
  }, { shouldPromote: promote }).catch(() => ({
    total: 0, substantive: 0, loaded: 0, unresolved_substantive: 0,
    promoted: 0, promoted_srcset: 0, height: 0, viewport: 1, y: 0,
  }));

  const initial = await collect(true);
  let completedSteps = 0;
  let lastHeight = 0;
  let stableBottom = 0;
  for (let step = 0; step < maxSteps; step += 1) {
    const state = await page.evaluate(() => {
      const root = document.scrollingElement || document.documentElement;
      const height = Math.max(root.scrollHeight, document.body?.scrollHeight || 0);
      const viewport = window.innerHeight || 1;
      const before = root.scrollTop;
      window.scrollTo(0, Math.min(height, before + Math.max(500, Math.floor(viewport * 0.82))));
      return { height, viewport, before };
    }).catch(() => null);
    if (!state) break;
    completedSteps = step + 1;
    await page.waitForTimeout(delayMs);
    await collect(true);
    const atBottom = state.before + state.viewport >= state.height - 8;
    stableBottom = atBottom && state.height === lastHeight ? stableBottom + 1 : 0;
    lastHeight = state.height;
    if (stableBottom >= 1) break;
  }
  await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
  await page.waitForTimeout(delayMs);
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
  let final = await collect(true);
  let retried = false;
  const ratio = final.substantive ? final.loaded / final.substantive : 1;
  if (final.unresolved_substantive > 2 && ratio < 0.85) {
    retried = true;
    await page.evaluate(() => window.scrollTo(0, Math.max(0, (document.scrollingElement?.scrollHeight || 0) / 2))).catch(() => {});
    await page.waitForTimeout(Math.max(700, delayMs * 2));
    await collect(true);
    await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
    await page.waitForTimeout(Math.max(700, delayMs * 2));
    final = await collect(false);
  }
  const finalRatio = final.substantive ? final.loaded / final.substantive : 1;
  return {
    initial, final, completed_steps: completedSteps, retried,
    acceptable: final.unresolved_substantive <= 2 || finalRatio >= 0.85,
    loaded_ratio: Number(finalRatio.toFixed(4)),
  };
}

async function extractItems(page, platform, limit, scroll = true) {
  if (scroll) {
    await page.evaluate(async () => {
      for (let index = 0; index < 2; index += 1) {
        window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));
        await new Promise((resolve) => setTimeout(resolve, 600));
      }
      window.scrollTo(0, 0);
    }).catch(() => {});
  }
  const synthetic = platform === 'jd'
    ? await page.locator('.plugin_goodsCardWrapper[data-sku], [data-sku]').evaluateAll((nodes) => nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      const sku = String(node.getAttribute('data-sku') || '').trim();
      const titleNode = node.querySelector('[title]');
      const title = String(titleNode?.getAttribute('title') || titleNode?.textContent || '').trim().replace(/\s+/g, ' ');
      const snippet = String(node.innerText || node.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 1200);
      return {
        url: /^\d{5,}$/.test(sku) ? `https://item.jd.com/${sku}.html` : null,
        title,
        snippet,
        visible: Boolean(sku) && (rect.width > 0 || rect.height > 0 || title || snippet),
      };
    })).catch(() => [])
    : [];
  const anchors = await page.locator('a[href]').evaluateAll((nodes) => nodes.map((anchor) => {
    const rect = anchor.getBoundingClientRect();
    const container = anchor.closest('li, article, section, [class*="item"], [class*="product"], [class*="goods"], div');
    return {
      url: anchor.href || anchor.getAttribute('href'),
      title: (anchor.innerText || anchor.textContent || '').trim().replace(/\s+/g, ' '),
      snippet: (container?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 1200),
      visible: rect.width > 0 && rect.height > 0,
    };
  })).catch(() => []);
  const seen = new Set();
  const output = [];
  for (const item of [...synthetic, ...anchors]) {
    if (!item.visible || !item.url) continue;
    let url;
    try { url = canonicalUrl(item.url); } catch { continue; }
    const config = PLATFORM_CONFIG[platform];
    if (!config.domains.some((domain) => hostMatches(new URL(url).hostname, domain))) continue;
    const pageKind = platformPageKind(platform, url);
    if (!pageKind || seen.has(url)) continue;
    const title = item.title || item.snippet.slice(0, 300);
    if (title.length < 2) continue;
    seen.add(url);
    output.push({ url, title: title.slice(0, 500), snippet: item.snippet, page_kind: pageKind });
    if (output.length >= limit) break;
  }
  return output;
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function relativeToRun(runDir, value) {
  return path.relative(runDir, value).split(path.sep).join('/');
}

async function artifactRecord(runDir, filePath) {
  const data = await fs.readFile(filePath);
  return {
    path: relativeToRun(runDir, filePath),
    size_bytes: data.length,
    sha256: sha256(data),
  };
}

function explicitZeroResults(text) {
  return /\u6682\u672a\u627e\u5230\u76f8\u5173\u5546\u54c1|\u672a\u627e\u5230\u76f8\u5173\u5546\u54c1|\u6ca1\u6709\u627e\u5230[^\n]{0,30}\u5546\u54c1|\u641c\u7d22\u65e0\u7ed3\u679c|\u6682\u65e0\u76f8\u5173\u7ed3\u679c|\u62b1\u6b49[^\n]{0,30}\u6ca1\u6709\u627e\u5230/i.test(String(text || ''));
}

function itemSignature(items) {
  const stable = items.slice(0, 20).map((item) => `${item.url}\n${item.title}`).sort();
  return sha256(JSON.stringify(stable));
}

async function declaredPageNumber(page, selectors = []) {
  const candidates = [...selectors, '[aria-current="page"]'];
  for (const selector of candidates) {
    const matches = page.locator(selector);
    const count = await matches.count().catch(() => 0);
    for (let index = 0; index < Math.min(count, 4); index += 1) {
      const locator = matches.nth(index);
      if (!await locator.isVisible().catch(() => false)) continue;
      const value = await locator.evaluate((element) => ({
        text: String(element.innerText || element.textContent || '').trim(),
        value: String(element.value || '').trim(),
        aria: String(element.getAttribute('aria-label') || '').trim(),
      })).catch(() => null);
      const match = `${value?.text || ''} ${value?.value || ''} ${value?.aria || ''}`.match(/\b(\d{1,4})\b/);
      if (match) return Number.parseInt(match[1], 10);
    }
  }
  return null;
}

async function findNextControl(page, selectors = []) {
  const candidates = [
    ...selectors,
    '[rel="next"]',
    '[aria-label="\u4e0b\u4e00\u9875"]',
    'a:has-text("\u4e0b\u4e00\u9875")',
    'button:has-text("\u4e0b\u4e00\u9875")',
  ];
  for (const selector of candidates) {
    const matches = page.locator(selector);
    const count = await matches.count().catch(() => 0);
    for (let index = 0; index < Math.min(count, 6); index += 1) {
      const locator = matches.nth(index);
      if (!await locator.isVisible().catch(() => false)) continue;
      const disabled = await locator.evaluate((element) => {
        const className = String(element.className || '').toLowerCase();
        return element.matches(':disabled')
          || element.getAttribute('aria-disabled') === 'true'
          || /(^|[\s_-])disabled([\s_-]|$)/.test(className);
      }).catch(() => true);
      if (!disabled) return { locator, selector };
    }
  }
  return null;
}

async function advanceToNextPage(page, platformConfig, timeoutMs) {
  const next = await findNextControl(page, platformConfig.nextSelectors || []);
  if (!next) return { attempted: false, verified: false, reason: 'next_control_not_found' };
  const fromUrl = page.url();
  try {
    await next.locator.click({ timeout: Math.min(timeoutMs, 8000) });
  } catch (error) {
    return {
      attempted: true, verified: false, reason: 'next_control_click_failed',
      selector: next.selector, from_url: fromUrl, error: String(error?.message || error),
    };
  }
  await page.waitForLoadState('domcontentloaded', { timeout: Math.min(timeoutMs, 12000) }).catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 8000) }).catch(() => {});
  await page.waitForTimeout(1600);
  return {
    attempted: true, verified: null, reason: 'awaiting_content_verification',
    selector: next.selector, from_url: fromUrl, to_url: page.url(),
  };
}

async function savePageArtifacts(page, runDir, artifactDir, metadata, items, options = {}) {
  await fs.mkdir(artifactDir, { recursive: true });
  metadata.artifact_dir = relativeToRun(runDir, artifactDir);
  const artifacts = {};
  const protocolTimeoutMs = Math.max(3_000, Number(options.protocolTimeoutMs || 30_000));
  const pdfTimeoutMs = Math.max(protocolTimeoutMs, Number(options.pdfTimeoutMs || 45_000));
  const renderedDomPath = path.join(artifactDir, 'rendered-dom.html');
  const compatibilityHtmlPath = path.join(artifactDir, 'search.html');
  const html = await withOperationTimeout(
    () => page.content(), protocolTimeoutMs, 'page_content',
  ).catch(() => '');
  if (html) {
    await fs.writeFile(renderedDomPath, html, 'utf8');
    await fs.writeFile(compatibilityHtmlPath, html, 'utf8');
    artifacts.rendered_dom = await artifactRecord(runDir, renderedDomPath);
    artifacts.search_html = await artifactRecord(runDir, compatibilityHtmlPath);
  }

  const screenshotPath = path.join(artifactDir, 'search.png');
  if (options.precomputedScreenshotPath && existsSync(options.precomputedScreenshotPath)) {
    await fs.copyFile(options.precomputedScreenshotPath, screenshotPath);
  } else {
    metadata.capture_strategy = 'diagnostic_viewport';
    await captureViewportScreenshot(page, screenshotPath, {
      timeoutMs: 5_000,
      protocolTimeoutMs,
      backendState: options.backendState,
    }).catch(() => {});
  }
  if (existsSync(screenshotPath)) artifacts.fullpage = await artifactRecord(runDir, screenshotPath);

  const mhtmlPath = path.join(artifactDir, 'page.mhtml');
  let mhtmlSession = null;
  try {
    mhtmlSession = await withOperationTimeout(
      () => page.context().newCDPSession(page),
      Math.min(protocolTimeoutMs, 5_000),
      'mhtml_cdp_session_create',
    );
    const snapshot = await withOperationTimeout(
      () => mhtmlSession.send('Page.captureSnapshot', { format: 'mhtml' }),
      protocolTimeoutMs,
      'cdp_page_capture_snapshot',
      () => mhtmlSession?.detach().catch(() => {}),
    );
    if (snapshot?.data) {
      await fs.writeFile(mhtmlPath, snapshot.data, 'utf8');
      artifacts.mhtml = await artifactRecord(runDir, mhtmlPath);
    }
  } catch (error) {
    metadata.errors.push({ stage: 'mhtml', message: String(error?.message || error) });
  } finally {
    if (mhtmlSession) {
      await withOperationTimeout(
        () => mhtmlSession.detach(), 2_000, 'mhtml_cdp_session_detach',
      ).catch(() => {});
    }
  }

  const pdfPath = path.join(artifactDir, 'page.pdf');
  try {
    await withOperationTimeout(
      () => page.pdf({ path: pdfPath, format: 'A4', printBackground: true, preferCSSPageSize: true }),
      pdfTimeoutMs,
      'page_pdf',
      () => page.close({ runBeforeUnload: false }).catch(() => {}),
    );
    if (existsSync(pdfPath)) artifacts.pdf = await artifactRecord(runDir, pdfPath);
  } catch (error) {
    metadata.errors.push({ stage: 'pdf', message: String(error?.message || error) });
  }

  const itemsPath = path.join(artifactDir, 'items.json');
  await fs.writeFile(itemsPath, `${JSON.stringify(items, null, 2)}\n`, 'utf8');
  artifacts.items = await artifactRecord(runDir, itemsPath);
  metadata.artifacts = artifacts;
  const metadataPath = path.join(artifactDir, 'metadata.json');
  await fs.writeFile(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`, 'utf8');
  return { artifact_dir: metadata.artifact_dir, artifacts };
}

async function artifactBundlePresent(runDir, metadata) {
  const required = ['rendered_dom', 'search_html', 'fullpage', 'mhtml', 'pdf', 'items'];
  for (const key of required) {
    const record = metadata?.artifacts?.[key];
    const minimum = key === 'items' ? 2 : 1000;
    if (!record || Number(record.size_bytes || 0) < minimum
        || !/^[a-f0-9]{64}$/i.test(String(record.sha256 || ''))) return false;
    const absolute = path.resolve(runDir, String(record.path || ''));
    if (absolute !== runDir && !absolute.startsWith(`${runDir}${path.sep}`)) return false;
    try {
      const stat = await fs.stat(absolute);
      if (!stat.isFile() || stat.size !== Number(record.size_bytes)) return false;
      const digest = createHash('sha256').update(await fs.readFile(absolute)).digest('hex');
      if (digest !== String(record.sha256 || '').toLowerCase()) return false;
    } catch {
      return false;
    }
  }
  return true;
}

async function recoverCompletedArtifactRuns(runDir, tasks, pagesPerQuery) {
  const taskById = new Map(tasks.map((task) => [String(task.query_id || ''), task]));
  const root = path.join(runDir, 'discovery', 'assisted-platforms');
  const recoveredRuns = [];
  const recoveredItems = [];
  if (!existsSync(root)) return { platform_runs: recoveredRuns, items: recoveredItems, query_ids: [] };
  for (const platformEntry of await fs.readdir(root, { withFileTypes: true })) {
    if (!platformEntry.isDirectory() || !PLATFORM_CONFIG[platformEntry.name]) continue;
    const platform = platformEntry.name;
    const platformDir = path.join(root, platform);
    for (const queryEntry of await fs.readdir(platformDir, { withFileTypes: true })) {
      if (!queryEntry.isDirectory()) continue;
      const queryId = queryEntry.name;
      const task = taskById.get(queryId);
      if (!task || task.target_platform !== platform) continue;
      const queryDir = path.join(platformDir, queryId);
      const pageRuns = [];
      const pageEntries = (await fs.readdir(queryDir, { withFileTypes: true }))
        .filter((entry) => entry.isDirectory() && /^page-\d+$/i.test(entry.name))
        .sort((left, right) => left.name.localeCompare(right.name));
      for (const pageEntry of pageEntries) {
        const metadataPath = path.join(queryDir, pageEntry.name, 'metadata.json');
        if (!existsSync(metadataPath)) continue;
        try {
          const metadata = JSON.parse(await fs.readFile(metadataPath, 'utf8'));
          if (metadata.delivery_eligible !== true || metadata.query_id !== queryId
              || metadata.platform !== platform
              || normalizeSearchQuery(metadata.search_query) !== normalizeSearchQuery(task.platform_search_query)
              || String(metadata.target_good || '') !== String(task.target_good || '')
              || !await artifactBundlePresent(runDir, metadata)) continue;
          pageRuns.push(metadata);
        } catch {}
      }
      if (!pageRuns.length) continue;
      const zeroResults = pageRuns.some((value) => value.state === 'zero_results');
      if (!zeroResults && pageRuns.length < pagesPerQuery) continue;
      recoveredRuns.push({
        platform,
        platform_label: PLATFORM_CONFIG[platform].label,
        query_id: queryId,
        target_good: task.target_good || null,
        search_query: task.platform_search_query,
        home_url: PLATFORM_CONFIG[platform].home,
        expected_search_url: task.expected_search_url || null,
        initial_state: pageRuns[0].state,
        final_state: pageRuns.at(-1).state,
        result_count: pageRuns.reduce((sum, value) => sum + Number(value.result_count || 0), 0),
        delivery_eligible: true,
        filing_eligible: true,
        artifact_dir: pageRuns.find((value) => value.artifact_dir)?.artifact_dir || null,
        errors: [],
        pages_requested: pagesPerQuery,
        pages_captured: pageRuns.length,
        pagination_status: zeroResults ? 'zero_results' : 'complete',
        page_runs: pageRuns,
        recovered_from_complete_artifacts: true,
      });
      for (const pageRun of pageRuns) {
        const itemRecord = pageRun.artifacts?.items;
        if (!itemRecord?.path) continue;
        try {
          const items = JSON.parse(await fs.readFile(path.resolve(runDir, itemRecord.path), 'utf8'));
          for (let rank = 0; rank < items.length; rank += 1) {
            recoveredItems.push({
              ...items[rank], platform, platform_label: PLATFORM_CONFIG[platform].label,
              query_id: queryId, query: task.platform_search_query,
              target_good: task.target_good || null, provider: 'platform-browser', rank: rank + 1,
              result_page_index: pageRun.page_index || 1,
              declared_page_number: pageRun.declared_page_number ?? null,
              recovered_from_complete_artifacts: true,
            });
          }
        } catch {}
      }
    }
  }
  return {
    platform_runs: recoveredRuns,
    items: recoveredItems,
    query_ids: recoveredRuns.map((value) => value.query_id),
  };
}

const args = parseArgs(process.argv.slice(2));
if (!args['run-dir'] || !args['user-data-dir']) {
  console.error('Usage: sales-platform-assisted-discover.mjs --run-dir <dir> --user-data-dir <persistent-profile-outside-run> [--profile-directory Default] [--platform taobao] [--browser auto|edge|chrome] [--pages-per-query 1..20] [--include-zero-results]');
  process.exit(1);
}

const runDir = path.resolve(args['run-dir']);
const userDataDir = path.resolve(args['user-data-dir']);
if (userDataDir === runDir || userDataDir.startsWith(`${runDir}${path.sep}`)) {
  throw new Error('--user-data-dir must be outside RUN_DIR so cookies never enter the evidence package');
}
const config = JSON.parse(await fs.readFile(path.join(runDir, 'run-config.json'), 'utf8'));
const plan = JSON.parse(await fs.readFile(path.join(runDir, 'discovery', 'query-plan.json'), 'utf8'));
if (config.execution_profile !== 'quick' || plan.discovery_scope !== 'sales_platforms') {
  throw new Error('Assisted sales discovery requires quick profile and discovery_scope=sales_platforms');
}
const taskSource = String(args['task-source'] || 'query-plan').toLowerCase();
if (!['query-plan', 'manual-capture-queue'].includes(taskSource)) {
  throw new Error('--task-source must be query-plan or manual-capture-queue');
}
let manualQueue = null;
if (taskSource === 'manual-capture-queue') {
  const manualQueuePath = path.join(runDir, 'discovery', 'manual-capture-queue.json');
  if (!existsSync(manualQueuePath)) throw new Error('manual-capture-queue.json is required for the selected task source');
  manualQueue = JSON.parse(await fs.readFile(manualQueuePath, 'utf8'));
}
const sourcePlatforms = taskSource === 'manual-capture-queue'
  ? (manualQueue.items || []).map((item) => item.platform).filter(Boolean)
  : plan.items.map((item) => item.target_platform).filter(Boolean);
const requested = args.platform.length ? args.platform : sourcePlatforms;
const platforms = [...new Set(requested)].filter((item) => PLATFORM_CONFIG[item]);
if (!platforms.length) throw new Error('No supported platform selected');
const mark = String(config.trademark?.name || '').trim();
const goods = (config.trademark?.goods_services || []).map(String).filter(Boolean);
const planSearches = (plan.items || [])
  .filter((item) => platforms.includes(item.target_platform))
  .map((item) => ({
    ...item,
    platform_search_query: String(item.platform_search_query || '').trim(),
    expected_search_url: String(
      item.search_url || canonicalPlatformSearchUrl(item.target_platform, item.platform_search_query) || '',
    ).trim(),
  }));
const manualSearches = taskSource === 'manual-capture-queue'
  ? (manualQueue.items || [])
    .filter((item) => platforms.includes(item.platform))
    .filter((item) => !args['query-id'].length || args['query-id'].includes(item.task_id))
    .map((item) => ({
      query_id: item.task_id,
      target_platform: item.platform,
      target_good: item.target_good || null,
      platform_search_query: String(item.query || '').trim(),
      expected_search_url: String(item.search_url || '').trim(),
      query_kind: item.query_kind || null,
    }))
  : [];
const unscheduledSearchTasks = manualSearches.length
  ? manualSearches
  : (planSearches.length ? planSearches
  : platforms.flatMap((platform) => (goods.length ? goods : ['']).map((good, index) => ({
      query_id: `SMQ-${platform}-${String(index + 1).padStart(3, '0')}`,
      target_platform: platform,
      target_good: good,
      platform_search_query: [mark, good].filter(Boolean).join(' ').trim(),
      expected_search_url: canonicalPlatformSearchUrl(
        platform, [mark, good].filter(Boolean).join(' ').trim(),
      ),
    }))));
let searchTasks = roundRobinByKey(
  unscheduledSearchTasks, (item) => item.target_platform, platforms,
);
if (!searchTasks.length || searchTasks.some((item) => !item.platform_search_query)) {
  throw new Error('Sales query plan does not contain usable mark + goods platform searches');
}
if (searchTasks.some((item) => /%[0-9a-f]{2}/i.test(normalizeSearchQuery(item.platform_search_query)))) {
  throw new Error('Sales query plan contains a pre-encoded query; raw text is required');
}
if (taskSource === 'manual-capture-queue' && searchTasks.some((item) => !item.expected_search_url)) {
  throw new Error('Manual sales task is missing its canonical expected_search_url');
}
const plannedSearchTasks = [...searchTasks];
if (args['plan-only'] === true) {
  console.log(JSON.stringify({
    assisted_discovery_plan_only: true,
    task_source: taskSource,
    query_strategy: taskSource === 'manual-capture-queue'
      ? 'mark_only_and_mark_plus_each_good_per_platform'
      : 'mark_plus_each_good_per_platform',
    query_count: searchTasks.length,
    platforms,
    task_ids: searchTasks.map((item) => item.query_id || null),
    queries: searchTasks.map((item) => item.platform_search_query),
  }, null, 2));
  process.exit(0);
}
const timeoutMs = Math.max(10_000, Number.parseInt(args['timeout-ms'] || '30000', 10));
const limit = Math.max(1, Math.min(50, Number.parseInt(args.limit || '20', 10)));
const pagesPerQuery = Math.max(1, Math.min(20, Number.parseInt(args['pages-per-query'] || '1', 10)));
const includeZeroResults = args['include-zero-results'] === true;
const artifactLockPath = args['artifact-lock'] ? path.resolve(args['artifact-lock']) : null;
if (artifactLockPath && artifactLockPath !== runDir && !artifactLockPath.startsWith(`${runDir}${path.sep}`)) {
  throw new Error('--artifact-lock must stay inside RUN_DIR');
}
const artifactLockOptions = {
  timeoutMs: Number(RUNTIME_POLICY.concurrency?.artifact_lock_timeout_sec || 180) * 1000,
  staleMs: Number(RUNTIME_POLICY.concurrency?.artifact_lock_stale_sec || 300) * 1000,
  pollMs: Number(RUNTIME_POLICY.concurrency?.artifact_lock_poll_ms || 150),
};
const artifactCaptureWallTimeoutMs = Number(
  RUNTIME_POLICY.concurrency?.artifact_capture_wall_timeout_sec || 90,
) * 1000;
const browserProtocolTimeoutMs = Number(
  RUNTIME_POLICY.concurrency?.browser_protocol_timeout_sec || 30,
) * 1000;
const pdfCaptureTimeoutMs = Number(
  RUNTIME_POLICY.concurrency?.pdf_capture_timeout_sec || 45,
) * 1000;
const progressStallGraceMs = Number(
  RUNTIME_POLICY.concurrency?.progress_stall_grace_sec || 30,
) * 1000;
const liveProgressPath = path.join(runDir, 'discovery', 'sales-live-progress.json');
const recoveredArtifacts = args['merge-existing'] === true
  ? await recoverCompletedArtifactRuns(runDir, searchTasks, pagesPerQuery)
  : { platform_runs: [], items: [], query_ids: [] };
const recoveredQueryIds = new Set(recoveredArtifacts.query_ids);
searchTasks = searchTasks.filter((task) => !recoveredQueryIds.has(task.query_id));
let liveProgress = {
  schema_version: '1.0',
  record_type: 'sales_search_live_progress',
  state: 'starting',
  started_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  planned_task_count: plannedSearchTasks.length,
  recovered_task_count: recoveredQueryIds.size,
  completed_task_count: recoveredQueryIds.size,
  active_task: null,
  stage: 'starting',
  stage_deadline_at: null,
  last_completed_task: null,
  last_error: null,
};
async function updateLiveProgress(update = {}) {
  liveProgress = { ...liveProgress, ...update, updated_at: new Date().toISOString() };
  await writeTextAtomic(liveProgressPath, `${JSON.stringify(liveProgress, null, 2)}\n`);
}
let fatalProgressHandling = false;
async function recordFatalProgress(reason) {
  if (fatalProgressHandling) return;
  fatalProgressHandling = true;
  const message = String(reason?.stack || reason?.message || reason || 'unknown fatal error');
  console.error(message);
  try {
    await updateLiveProgress({
      state: 'failed',
      stage: 'fatal_error',
      stage_deadline_at: null,
      artifact_lock_held: false,
      last_error: message,
      finished_at: new Date().toISOString(),
    });
  } catch (progressError) {
    console.error(
      `Failed to persist fatal progress; original error above is retained: ${String(progressError?.message || progressError)}`,
    );
  }
  process.exit(1);
}
process.once('uncaughtException', recordFatalProgress);
process.once('unhandledRejection', recordFatalProgress);
if (args['recover-artifacts-only'] === true) {
  console.log(JSON.stringify({
    recovered_artifacts_only: true,
    planned_task_count: plannedSearchTasks.length,
    recovered_task_count: recoveredQueryIds.size,
    recovered_query_ids: [...recoveredQueryIds],
    remaining_task_count: searchTasks.length,
  }, null, 2));
  process.exit(0);
}
const browserRequested = String(args.browser || 'auto').toLowerCase();
if (!['auto', 'edge', 'chrome'].includes(browserRequested)) throw new Error('--browser must be auto, edge or chrome');
let browserProduct = browserRequested === 'auto' ? 'edge' : browserRequested;
let browserExecutable = findBrowserExecutable(args['browser-executable'], browserProduct);
const cdpEndpoint = args['cdp-endpoint'] ? String(args['cdp-endpoint']) : null;
if (cdpEndpoint) {
  const endpointUrl = new URL(cdpEndpoint);
  if (!['http:', 'https:'].includes(endpointUrl.protocol)
      || !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(endpointUrl.hostname)) {
    throw new Error('--cdp-endpoint must use an HTTP(S) loopback address');
  }
  const response = await fetch(`${cdpEndpoint.replace(/\/$/, '')}/json/version`);
  if (!response.ok) throw new Error(`CDP version probe failed: HTTP ${response.status}`);
  const version = await response.json();
  const product = String(version.Browser || '').toLowerCase();
  if (browserRequested === 'auto') {
    if (product.includes('edg/') || product.includes('microsoft edge')) browserProduct = 'edge';
    else if (product.includes('chrome/') && !product.includes('edg/')) browserProduct = 'chrome';
  }
  const matches = browserProduct === 'edge'
    ? (product.includes('edg/') || product.includes('microsoft edge'))
    : (product.includes('chrome/') && !product.includes('edg/'));
  if (!matches) throw new Error(`CDP browser product mismatch: expected ${browserProduct}, got ${version.Browser || 'unknown'}`);
}
if (!browserExecutable && !cdpEndpoint && !args['browser-executable'] && browserRequested === 'auto') {
  browserProduct = 'chrome';
  browserExecutable = findBrowserExecutable(null, browserProduct);
}
if (!browserExecutable && !cdpEndpoint) throw new Error('Chrome/Edge executable not found');
const executableProduct = browserProductFromExecutable(browserExecutable);
if (executableProduct && executableProduct !== browserProduct) {
  throw new Error(`Browser product ${browserProduct} does not match executable ${path.basename(browserExecutable)}`);
}
await fs.mkdir(userDataDir, { recursive: true });
const successfulDir = path.join(runDir, 'discovery', 'assisted-platforms');
const diagnosticsDir = path.join(runDir, 'capture-diagnostics', 'sales-platform-search');
const rateLimitStatePath = path.join(runDir, 'discovery', 'sales-rate-limit-state.json');
await fs.mkdir(successfulDir, { recursive: true });
await fs.mkdir(diagnosticsDir, { recursive: true });
let rateLimitState = { schema_version: '1.0', updated_at: null, platforms: {} };
if (existsSync(rateLimitStatePath)) {
  rateLimitState = JSON.parse(await fs.readFile(rateLimitStatePath, 'utf8'));
  rateLimitState.platforms ||= {};
}
async function persistRateLimitState() {
  rateLimitState.updated_at = new Date().toISOString();
  await writeTextAtomic(rateLimitStatePath, `${JSON.stringify(rateLimitState, null, 2)}\n`);
}

// Older releases incorrectly converted a CAPTCHA/login page into a timed
// platform freeze. Remove that synthetic timer locally before any retry. The
// next owned page will still detect and preserve a verification page if the
// employee has not completed it.
let migratedLegacyVerificationCooldown = false;
let migratedLegacyRiskCooldown = false;
for (const [platform, originalPlatformState] of Object.entries(rateLimitState.platforms)) {
  let platformState = originalPlatformState;
  if (platformState && isRiskCircuitBreakerState(platformState.trigger_state)) {
    const migration = migrateLegacyFirstStrike(
      platformState,
      ratePolicy(platform),
      RISK_CIRCUIT_BREAKER_POLICY,
    );
    if (migration.migrated) {
      platformState = migration.state;
      rateLimitState.platforms[platform] = platformState;
      migratedLegacyRiskCooldown = true;
    }
  }
  if (!platformState || !isHumanVerificationState(platformState.trigger_state)) continue;
  if (platformState.cooldown_until || platformState.post_batch_not_before) {
    platformState.cooldown_until = null;
    platformState.post_batch_not_before = null;
    platformState.cooldown_triggered_at = null;
    platformState.cooldown_minutes_applied = null;
    platformState.cooldown_tier_min_strike_count = null;
    platformState.legacy_verification_cooldown_removed_at = new Date().toISOString();
    migratedLegacyVerificationCooldown = true;
  }
  platformState.manual_verification_required = true;
  platformState.platform_freeze_observed = false;
}
if (migratedLegacyVerificationCooldown || migratedLegacyRiskCooldown) await persistRateLimitState();

const report = {
  schema_version: '2.0', record_type: 'browser_sales_platform_discovery',
  started_at: new Date().toISOString(), finished_at: null,
  headed_browser_used: true, persistent_profile_used: true,
  browser_control_plugin_used: false, computer_use_used: false,
  browser_product: browserProduct, browser_executable: browserExecutable,
  browser_selection_policy: 'edge_then_chrome',
  browser_fallback_used: args['browser-fallback-used'] === true
    || (browserRequested === 'auto' && browserProduct === 'chrome'),
  browser_handoff_mode: cdpEndpoint ? 'attach' : 'restart',
  cdp_endpoint_loopback: Boolean(cdpEndpoint),
  manual_interaction_required: false, user_data_dir_stored_in_run: false,
  automated_captcha_interaction: false,
  query_strategy: taskSource === 'manual-capture-queue'
    ? 'mark_only_and_mark_plus_each_good_per_platform'
    : 'mark_plus_each_good_per_platform',
  task_source: taskSource,
  platforms_requested: platforms, query_count: plannedSearchTasks.length,
  pages_per_query_requested: pagesPerQuery,
  zero_result_shortfall_allowed: includeZeroResults,
  rate_limit_policy: RATE_LIMIT_POLICY,
  risk_circuit_breaker_policy: RISK_CIRCUIT_BREAKER_POLICY,
  risk_recovery_events: [],
  controlled_concurrency: {
    cross_channel_parallelism: Number(RUNTIME_POLICY.concurrency?.max_search_channels || 1),
    same_domain_concurrency: Number(RUNTIME_POLICY.concurrency?.max_same_domain_requests || 1),
    artifact_capture_concurrency: Number(RUNTIME_POLICY.concurrency?.max_artifact_captures || 1),
    platform_round_robin: true,
  },
  rate_limit_events: [],
  verification_events: [],
  platform_runs: [...recoveredArtifacts.platform_runs], items: [...recoveredArtifacts.items],
  recovered_complete_artifact_query_count: recoveredQueryIds.size,
};
async function markTaskProgressComplete(run) {
  const completedRuns = report.platform_runs.filter((value) => value.final_state).length;
  const deliveryEligibleRuns = report.platform_runs.filter((value) => value.delivery_eligible === true).length;
  await updateLiveProgress({
    state: 'running',
    stage: 'task_complete',
    stage_deadline_at: null,
    active_task: null,
    processed_task_count: completedRuns,
    completed_task_count: deliveryEligibleRuns,
    last_completed_task: {
      platform: run.platform,
      query_id: run.query_id,
      query: run.search_query,
      final_state: run.final_state,
      delivery_eligible: run.delivery_eligible === true,
      artifact_dir: run.artifact_dir || null,
    },
  });
}
await updateLiveProgress({
  state: 'running',
  stage: 'ready',
  completed_task_count: recoveredQueryIds.size,
});

const launchArgs = [
  '--disable-background-mode',
  '--no-first-run',
  '--no-default-browser-check',
];
if (browserProduct === 'edge') {
  launchArgs.push('--edge-skip-compat-layer-relaunch', '--disable-features=msEdgeUpdateLaunchServicesPreferredVersion');
}
if (args['profile-directory']) launchArgs.push(`--profile-directory=${args['profile-directory']}`);
let attachedBrowser = null;
let context = null;
let originalPages = new Set();
const ownedPages = new Set();
const preservedVerificationPages = new Set();
// A CAPTCHA/login page is a hard stop for the remainder of that platform in
// the current invocation.  A later resume may make one bounded retry after
// the employee has handled the page, but this invocation must never continue
// sending queries behind an unresolved challenge.
const verificationBlockedPlatforms = new Set();
// An expired risk cooldown releases one canary for that platform.  If that
// canary is inconclusive, all remaining same-platform tasks stay deferred
// until the same tier has rested again.
const inconclusiveRiskProbePlatforms = new Set();
const screenshotBackendStatesByPlatform = new Map();
function screenshotBackendStateForPlatform(platform) {
  if (!screenshotBackendStatesByPlatform.has(platform)) {
    screenshotBackendStatesByPlatform.set(platform, {
      platform, forceCdp: false, switched_at: null, reason: null,
    });
  }
  return screenshotBackendStatesByPlatform.get(platform);
}
if (cdpEndpoint) {
  attachedBrowser = await chromium.connectOverCDP(cdpEndpoint);
  context = attachedBrowser.contexts()[0];
  if (!context) throw new Error('No browser context is available at the CDP endpoint');
  originalPages = new Set(context.pages());
} else {
  context = await chromium.launchPersistentContext(userDataDir, {
    executablePath: browserExecutable,
    headless: false,
    viewport: { width: 1440, height: 1000 },
    args: launchArgs,
  });
}
async function newOwnedPage() {
  const candidate = await context.newPage();
  ownedPages.add(candidate);
  return candidate;
}
async function targetIdForPage(candidate) {
  let session = null;
  try {
    session = await withOperationTimeout(
      () => context.newCDPSession(candidate),
      Math.min(browserProtocolTimeoutMs, 5_000),
      'target_info_session_create',
    );
    const value = await withOperationTimeout(
      () => session.send('Target.getTargetInfo'), browserProtocolTimeoutMs, 'target_get_target_info',
    );
    return value?.targetInfo?.targetId || null;
  } catch {
    return null;
  } finally {
    if (session) {
      await withOperationTimeout(
        () => session.detach(), 2_000, 'target_info_session_detach',
      ).catch(() => {});
    }
  }
}
async function pageForTargetId(targetId) {
  if (!targetId) return null;
  for (const candidate of context.pages()) {
    if (candidate.isClosed()) continue;
    if (await targetIdForPage(candidate) === targetId) return candidate;
  }
  return null;
}
try {
  let page;
  if (cdpEndpoint) {
    page = await newOwnedPage();
  } else {
    page = context.pages()[0] || await newOwnedPage();
    ownedPages.add(page);
  }
  const platformWorkPages = new Map();
  let initialWorkPageAvailable = true;
  for (let index = 0; index < searchTasks.length; index += 1) {
    const task = searchTasks[index];
    const platform = task.target_platform;
    const screenshotBackendState = screenshotBackendStateForPlatform(platform);
    const existingPlatformPage = platformWorkPages.get(platform);
    if (existingPlatformPage && !existingPlatformPage.isClosed()
        && !(cdpEndpoint && originalPages.has(existingPlatformPage))) {
      page = existingPlatformPage;
    } else if (initialWorkPageAvailable && page && !page.isClosed()
        && !(cdpEndpoint && originalPages.has(page))) {
      initialWorkPageAvailable = false;
      platformWorkPages.set(platform, page);
    } else {
      page = await newOwnedPage();
      initialWorkPageAvailable = false;
      platformWorkPages.set(platform, page);
    }
    const platformConfig = PLATFORM_CONFIG[platform];
    const searchQuery = task.platform_search_query;
    const run = {
      platform, platform_label: platformConfig.label, query_id: task.query_id || null,
      target_good: task.target_good || null,
      search_query: searchQuery, home_url: platformConfig.home, initial_state: null,
      expected_search_url: task.expected_search_url || null,
      final_state: null, result_count: 0, delivery_eligible: false,
      filing_eligible: false, artifact_dir: null, errors: [],
      pages_requested: pagesPerQuery, pages_captured: 0,
      pagination_status: 'incomplete', page_runs: [],
    };
    report.platform_runs.push(run);
    await updateLiveProgress({
      state: 'running',
      stage: 'task_started',
      stage_deadline_at: null,
      active_task: {
        platform,
        query_id: run.query_id,
        query: searchQuery,
        target_good: run.target_good,
        started_at: new Date().toISOString(),
      },
    });
    if (verificationBlockedPlatforms.has(platform)) {
      run.final_state = 'deferred_manual_verification';
      run.manual_verification_required = true;
      run.errors.push({
        stage: 'manual_verification',
        message: 'A CAPTCHA or login confirmation is already pending for this platform; no request was sent.',
      });
      await markTaskProgressComplete(run);
      continue;
    }
    if (inconclusiveRiskProbePlatforms.has(platform)) {
      run.final_state = 'deferred_rate_limit_probe_inconclusive';
      run.errors.push({
        stage: 'rate_limit_probe',
        message: 'The single post-cooldown canary was inconclusive; no additional same-platform request was sent.',
      });
      await markTaskProgressComplete(run);
      continue;
    }
    const policy = ratePolicy(platform);
    const persistedPlatformState = rateLimitState.platforms[platform] || {};
    const persistedHumanVerification = isHumanVerificationState(persistedPlatformState.trigger_state);
    if (persistedHumanVerification && persistedPlatformState.verification_target_id) {
      const previousVerificationPage = await pageForTargetId(persistedPlatformState.verification_target_id);
      if (previousVerificationPage) {
        const previousState = await pageState(previousVerificationPage);
        if (isHumanVerificationState(previousState.state)) {
          verificationBlockedPlatforms.add(platform);
          preservedVerificationPages.add(previousVerificationPage);
          await previousVerificationPage.bringToFront().catch(() => {});
          report.manual_interaction_required = true;
          run.final_state = 'deferred_manual_verification';
          run.manual_verification_required = true;
          run.manual_verification_tab_kept_open = true;
          run.verification_page = {
            platform, query_id: run.query_id, state: previousState.state,
            url: previousVerificationPage.url(), title: previousState.title,
            target_id: persistedPlatformState.verification_target_id,
          };
          run.errors.push({
            stage: 'manual_verification',
            message: 'The previously preserved CAPTCHA/login page is still unresolved; no request was sent.',
          });
          await markTaskProgressComplete(run);
          continue;
        }
        await previousVerificationPage.close().catch(() => {});
      }
      persistedPlatformState.verification_target_id = null;
      persistedPlatformState.manual_verification_required = false;
      persistedPlatformState.trigger_state = null;
      persistedPlatformState.trigger_query_id = null;
      if (Number(persistedPlatformState.strike_count || 0) > 0) {
        persistedPlatformState.probe_required = true;
        persistedPlatformState.probe_attempted_at = null;
        persistedPlatformState.probe_attempted_for_cooldown_until = null;
        persistedPlatformState.probe_query_id = null;
      }
      await persistRateLimitState();
    }
    const cooldownUntilMs = persistedHumanVerification
      ? Number.NaN
      : Date.parse(persistedPlatformState.cooldown_until || '');
    const postBatchUntilMs = Date.parse(persistedPlatformState.post_batch_not_before || '');
    const activeCooldownUntilMs = Math.max(
      Number.isFinite(cooldownUntilMs) ? cooldownUntilMs : 0,
      Number.isFinite(postBatchUntilMs) ? postBatchUntilMs : 0,
    );
    if (activeCooldownUntilMs > Date.now()) {
      const riskCooldownActive = Number.isFinite(cooldownUntilMs) && cooldownUntilMs >= activeCooldownUntilMs;
      run.final_state = riskCooldownActive ? 'deferred_rate_limit_cooldown' : 'deferred_post_batch_rest';
      run.cooldown_until = new Date(activeCooldownUntilMs).toISOString();
      run.errors.push({
        stage: 'rate_limit',
        message: `Platform rest window remains active until ${run.cooldown_until}`,
      });
      await markTaskProgressComplete(run);
      continue;
    }
    const platformRiskGate = riskGate(persistedPlatformState);
    if (!platformRiskGate.allow_request) {
      run.final_state = platformRiskGate.reason === 'probe_already_attempted'
        ? 'deferred_rate_limit_probe_already_attempted'
        : 'deferred_rate_limit_cooldown';
      run.cooldown_until = platformRiskGate.cooldown_until || null;
      run.errors.push({
        stage: 'rate_limit_probe',
        message: platformRiskGate.reason === 'probe_already_attempted'
          ? 'The one permitted post-cooldown canary was already attempted; no request was sent.'
          : `Platform risk cooldown remains active until ${run.cooldown_until}`,
      });
      await markTaskProgressComplete(run);
      continue;
    }
    run.risk_probe = platformRiskGate.risk_probe === true;
    run.risk_probe_reason = platformRiskGate.reason;
    const delayPlan = queryDelayPlan(policy, persistedPlatformState);
    const targetDelayMs = delayPlan.interval_target_ms;
    const waitMs = delayPlan.wait_ms;
    run.rate_limit_delay_ms = waitMs;
    run.rate_limit_interval_target_ms = targetDelayMs;
    run.rate_limit_batch_break = delayPlan.batch_break;
    run.rate_limit_delay_anchor = delayPlan.anchor_kind;
    run.rate_limit_delay_anchor_at = delayPlan.anchor_at;
    if (waitMs > 0) {
      await updateLiveProgress({
        state: 'waiting_rate_limit',
        stage: 'rate_limit_wait',
        stage_deadline_at: new Date(Date.now() + waitMs + progressStallGraceMs).toISOString(),
      });
      await new Promise((resolve) => setTimeout(resolve, waitMs));
    }
    if (page.isClosed() || (cdpEndpoint && originalPages.has(page))) {
      page = await newOwnedPage();
      platformWorkPages.set(platform, page);
    }
    let requestPlatformState = {
      ...persistedPlatformState,
      last_request_at: new Date().toISOString(),
      cooldown_until: run.risk_probe ? (persistedPlatformState.cooldown_until || null) : null,
      post_batch_not_before: null,
      trigger_state: null,
      trigger_query_id: null,
      triggered_at_utc: null,
      trigger_evidence_dir: null,
      last_automation_observation: null,
      manual_verification_required: false,
      platform_freeze_observed: false,
      requests_since_break: delayPlan.next_requests_since_break,
    };
    if (run.risk_probe) {
      requestPlatformState = markRiskProbeAttempt(
        requestPlatformState, platformRiskGate, run.query_id,
      );
      run.risk_probe_attempted_at = requestPlatformState.probe_attempted_at;
    }
    rateLimitState.platforms[platform] = requestPlatformState;
    await persistRateLimitState();
    let activePage = page;
    try {
      await updateLiveProgress({
        state: 'running',
        stage: 'navigation',
        stage_deadline_at: new Date(Date.now() + timeoutMs + progressStallGraceMs).toISOString(),
      });
      const navigation = await searchFromHomepage(
        page, platformConfig, searchQuery, task.expected_search_url, timeoutMs,
      );
      activePage = navigation.resultPage || page;
      if (platform === 'jd' && navigation.submitted) {
        let resultHost = '';
        try { resultHost = new URL(activePage.url()).hostname; } catch {}
        if (!hostMatches(resultHost, 'search.jd.com')) {
          navigation.submission_verified = false;
          navigation.error = 'unexpected_search_result_page';
        }
      }
      const { resultPage, ...navigationRecord } = navigation;
      Object.assign(run, navigationRecord);
      const queryDirName = String(task.query_id || `Q${index + 1}`).replace(/[^A-Za-z0-9._-]/g, '_');
      let previousPage = null;
      let transition = {
        attempted: true,
        verified: Boolean(navigation.submission_verified),
        reason: navigation.submission_verified ? 'initial_search_result' : (navigation.error || 'search_not_submitted'),
        from_url: navigation.home_final_url || platformConfig.home,
        to_url: activePage.url(),
      };
      let globalRank = 0;
      for (let pageIndex = 1; pageIndex <= pagesPerQuery; pageIndex += 1) {
        const state = await pageState(activePage);
        const queryBinding = await verifyQueryBinding(
          activePage, platformConfig, searchQuery, task.expected_search_url,
        );
        if (pageIndex === 1) run.initial_state = state.state;
        let lazyImageHydration = null;
        let lazyImageHydrationError = null;
        if (navigation.submission_verified && state.state === 'normal') {
          try {
            await updateLiveProgress({
              state: 'running',
              stage: 'lazy_image_hydration',
              stage_deadline_at: new Date(Date.now() + browserProtocolTimeoutMs * 2 + progressStallGraceMs).toISOString(),
            });
            lazyImageHydration = await withOperationTimeout(
              () => hydrateLazyImages(activePage),
              browserProtocolTimeoutMs * 2,
              'lazy_image_hydration',
              () => activePage.close({ runBeforeUnload: false }).catch(() => {}),
            );
          } catch (error) {
            lazyImageHydrationError = String(error?.message || error);
          }
        }
        const items = navigation.submission_verified && state.state === 'normal'
          ? await extractItems(activePage, platform, limit, false)
          : [];
        const zeroResults = state.state === 'normal' && items.length === 0 && explicitZeroResults(state.text);
        let resultState = state.state;
        if (!navigation.submission_verified || !queryBinding.verified) resultState = navigation.error || 'query_binding_failed';
        else if (zeroResults) resultState = 'zero_results';
        else if (state.state === 'normal' && !items.length) resultState = 'no_extractable_results';
        const declared = await declaredPageNumber(activePage, platformConfig.currentPageSelectors || []);
        const signature = itemSignature(items);

        if (pageIndex > 1) {
          const urlChanged = String(transition.from_url || '') !== state.url;
          const itemSignatureChanged = Boolean(previousPage && previousPage.item_signature !== signature && items.length);
          const declaredPageAdvanced = Boolean(
            previousPage?.declared_page_number != null
            && declared != null
            && declared > previousPage.declared_page_number
          );
          transition = {
            ...transition,
            to_url: state.url,
            url_changed: urlChanged,
            item_signature_changed: itemSignatureChanged,
            declared_page_advanced: declaredPageAdvanced,
            verified: state.state === 'normal' && (itemSignatureChanged || declaredPageAdvanced),
            reason: state.state !== 'normal'
              ? `next_page_${state.state}`
              : (itemSignatureChanged || declaredPageAdvanced ? 'content_advanced' : 'repeated_or_unverifiable_page'),
          };
          if (!transition.verified && state.state === 'normal') resultState = 'repeated_page';
        }

        let pageDeliveryEligible = Boolean(
          navigation.submission_verified
          && queryBinding.verified
          && transition.verified
          && (
            (resultState === 'normal' && items.length > 0)
            || (resultState === 'zero_results' && includeZeroResults && pageIndex === 1)
          )
        );
        let visualCapture = null;
        let visualCaptureError = null;
        let screenshotStagingDir = null;
        let stagedScreenshotPath = null;
        let pageRecord = null;
        let saved = null;
        await updateLiveProgress({
          state: 'waiting_artifact_lock',
          stage: 'artifact_lock_wait',
          stage_deadline_at: new Date(
            Date.now() + artifactLockOptions.timeoutMs + progressStallGraceMs,
          ).toISOString(),
        });
        const artifactLease = await acquireArtifactLock(artifactLockPath, artifactLockOptions);
        try {
          await updateLiveProgress({
            state: 'running',
            stage: 'artifact_capture',
            stage_deadline_at: new Date(
              Date.now() + artifactCaptureWallTimeoutMs + pdfCaptureTimeoutMs
                + browserProtocolTimeoutMs + progressStallGraceMs,
            ).toISOString(),
            artifact_lock_held: true,
          });
          if (pageDeliveryEligible) {
            try {
              screenshotStagingDir = await fs.mkdtemp(path.join(os.tmpdir(), 'tmui-search-capture-'));
              stagedScreenshotPath = path.join(screenshotStagingDir, 'search.png');
              const captureResult = {
                quality: await captureViewportTiles(activePage, stagedScreenshotPath, {
                  expectedItemCount: items.length,
                  python: args.python || null,
                  backendState: screenshotBackendState,
                  wallTimeoutMs: artifactCaptureWallTimeoutMs,
                  protocolTimeoutMs: browserProtocolTimeoutMs,
                }),
                lock_wait_ms: artifactLease.waited_ms,
              };
              visualCapture = {
                ...captureResult.quality,
                artifact_capture_lock_wait_ms: captureResult.lock_wait_ms,
              };
              pageDeliveryEligible = Boolean(visualCapture.acceptable && visualCapture.output_created);
            } catch (error) {
              visualCaptureError = String(error?.message || error);
              pageDeliveryEligible = false;
            }
          }
          pageRecord = {
          page_index: pageIndex,
          platform,
          query_id: task.query_id || null,
          search_query: searchQuery,
          target_good: task.target_good || null,
          expected_search_url: task.expected_search_url || null,
          query_binding: queryBinding,
          submitted_query_verified: queryBinding.verified,
          declared_page_number: declared,
          state: resultState,
          title: state.title,
          url: state.url,
          captured_at: new Date().toISOString(),
          result_count: items.length,
          explicit_zero_results: zeroResults,
          delivery_eligible: pageDeliveryEligible,
          item_signature: signature,
          transition,
          lazy_image_hydration: lazyImageHydration,
          visual_capture: visualCapture,
          capture_strategy: visualCapture?.strategy || (pageDeliveryEligible ? null : 'diagnostic_fullpage'),
          image_load_complete: Boolean(
            (lazyImageHydration?.acceptable ?? true)
            && (visualCapture?.acceptable ?? !visualCaptureError)
          ),
          artifact_dir: null,
          artifacts: {},
          errors: [
            ...(lazyImageHydrationError
              ? [{ stage: 'lazy_image_hydration', message: lazyImageHydrationError }]
              : []),
            ...(visualCaptureError
              ? [{ stage: 'viewport_tile_capture', message: visualCaptureError }]
              : []),
            ...(visualCapture && !visualCapture.acceptable
              ? [{ stage: 'visual_capture_quality', message: 'stitched screenshot did not pass painted-image validation' }]
              : []),
          ],
        };
        const artifactRoot = pageDeliveryEligible ? successfulDir : diagnosticsDir;
        const artifactDir = path.join(
          artifactRoot, platform, queryDirName, `page-${String(pageIndex).padStart(2, '0')}`,
        );
          saved = await savePageArtifacts(activePage, runDir, artifactDir, pageRecord, items, {
            precomputedScreenshotPath: stagedScreenshotPath,
            backendState: screenshotBackendState,
            protocolTimeoutMs: browserProtocolTimeoutMs,
            pdfTimeoutMs: pdfCaptureTimeoutMs,
          });
          pageRecord.artifact_save_lock_wait_ms = artifactLease.waited_ms;
        } finally {
          await artifactLease.release();
          await updateLiveProgress({ artifact_lock_held: false });
        }
        if (screenshotStagingDir) {
          await fs.rm(screenshotStagingDir, { recursive: true, force: true }).catch(() => {});
        }
        const requiredArtifactKeys = ['rendered_dom', 'search_html', 'fullpage', 'mhtml', 'pdf', 'items'];
        const artifactBundleComplete = requiredArtifactKeys.every((key) => {
          const record = saved.artifacts?.[key];
          const minimum = key === 'items' ? 2 : 1000;
          return record && Number(record.size_bytes || 0) >= minimum && /^[a-f0-9]{64}$/i.test(String(record.sha256 || ''));
        });
        if (pageDeliveryEligible && !artifactBundleComplete) {
          pageDeliveryEligible = false;
          pageRecord.delivery_eligible = false;
          pageRecord.errors.push({
            stage: 'artifact_bundle',
            message: 'required HTML/image/MHTML/PDF artifact missing, too small, or unhashed',
          });
          const diagnosticTarget = path.join(
            diagnosticsDir, platform, queryDirName, `page-${String(pageIndex).padStart(2, '0')}`,
          );
          await fs.rm(diagnosticTarget, { recursive: true, force: true }).catch(() => {});
          await fs.mkdir(path.dirname(diagnosticTarget), { recursive: true });
          await fs.rename(artifactDir, diagnosticTarget);
          const oldPrefix = `${saved.artifact_dir}/`;
          const newRelative = relativeToRun(runDir, diagnosticTarget);
          for (const record of Object.values(saved.artifacts || {})) {
            if (record?.path?.startsWith(oldPrefix)) {
              record.path = `${newRelative}/${record.path.slice(oldPrefix.length)}`;
            }
          }
          saved.artifact_dir = newRelative;
          pageRecord.artifact_dir = newRelative;
          pageRecord.artifacts = saved.artifacts;
          await fs.writeFile(
            path.join(diagnosticTarget, 'metadata.json'),
            `${JSON.stringify(pageRecord, null, 2)}\n`, 'utf8',
          );
        } else {
          pageRecord.artifact_dir = saved.artifact_dir;
          pageRecord.artifacts = saved.artifacts;
        }
        run.page_runs.push(pageRecord);
        if (!run.artifact_dir && pageDeliveryEligible) run.artifact_dir = saved.artifact_dir;

        if (pageDeliveryEligible && resultState === 'normal') {
          for (let rank = 0; rank < items.length; rank += 1) {
            globalRank += 1;
            report.items.push({
              ...items[rank], platform, platform_label: platformConfig.label,
              query_id: task.query_id || null, query: searchQuery,
              target_good: task.target_good || null,
              provider: 'platform-browser', rank: globalRank,
              result_page_index: pageIndex,
              declared_page_number: declared,
            });
          }
        }

        run.final_state = resultState;
        run.final_url = state.url;
        run.title = state.title;
        if (resultState === 'zero_results') {
          run.pagination_status = includeZeroResults ? 'zero_results' : 'incomplete';
          break;
        }
        if (!pageDeliveryEligible) {
          run.errors.push({
            stage: 'pagination', page_index: pageIndex,
            message: transition.reason || resultState,
          });
          break;
        }
        if (pageIndex === pagesPerQuery) {
          run.pagination_status = 'complete';
          break;
        }

        previousPage = pageRecord;
        transition = await advanceToNextPage(activePage, platformConfig, timeoutMs);
        if (!transition.attempted) {
          run.errors.push({ stage: 'pagination', page_index: pageIndex + 1, message: transition.reason });
          break;
        }
      }
      run.pages_captured = run.page_runs.filter((item) => item.delivery_eligible).length;
      run.result_count = run.page_runs.reduce((total, item) => total + Number(item.result_count || 0), 0);
      run.delivery_eligible = run.page_runs.some((item) => item.delivery_eligible);
      run.filing_eligible = ['complete', 'zero_results'].includes(run.pagination_status);
    } catch (error) {
      run.final_state = 'error';
      run.errors.push({ stage: 'fatal', message: String(error?.message || error) });
    } finally {
      if (activePage !== page && !activePage.isClosed()) {
        ownedPages.add(activePage);
        if (!ownedPages.has(page) || originalPages.has(page)) {
          throw new Error('refusing_to_close_non_owned_original_page');
        }
        await page.close().catch(() => {});
        page = activePage;
        platformWorkPages.set(platform, page);
      }
    }
    const triggerState = [
      ...[...run.page_runs].reverse().map((item) => item.state),
      run.home_state, run.initial_state, run.final_state,
    ].find(isBlockingState);
    if (triggerState) {
      const triggeredAt = new Date().toISOString();
      const triggerPage = [...run.page_runs].reverse().find((item) => isBlockingState(item.state))
        || [...run.page_runs].reverse().find((item) => item.state === 'search_not_submitted')
        || run.page_runs.at(-1)
        || null;
      const lastAutomationObservation = {
        state: triggerState,
        page_state: triggerPage?.state || null,
        observed_at_utc: triggerPage?.captured_at || triggeredAt,
        url: sanitizedObservationUrl(triggerPage?.url),
        title: triggerPage?.title || null,
        evidence_dir: triggerPage?.artifact_dir || null,
        query_id: run.query_id || null,
      };
      if (isHumanVerificationState(triggerState)) {
        verificationBlockedPlatforms.add(platform);
        report.manual_interaction_required = true;
        const verificationTargetId = cdpEndpoint && page && !page.isClosed()
          ? await targetIdForPage(page)
          : null;
        rateLimitState.platforms[platform] = {
          ...(rateLimitState.platforms[platform] || {}),
          cooldown_until: null,
          post_batch_not_before: null,
          cooldown_triggered_at: null,
          cooldown_minutes_applied: null,
          cooldown_tier_min_strike_count: null,
          trigger_state: triggerState,
          trigger_query_id: run.query_id,
          triggered_at_utc: triggeredAt,
          trigger_evidence_dir: lastAutomationObservation.evidence_dir,
          last_automation_observation: lastAutomationObservation,
          manual_verification_required: true,
          platform_freeze_observed: false,
          platform_reported_wait_seconds: null,
          automatic_retry_allowed: false,
          verification_target_id: verificationTargetId,
          requests_since_break: 0,
        };
        report.verification_events.push({
          platform, query_id: run.query_id, trigger_state: triggerState,
          manual_verification_required: true,
          platform_freeze_observed: false,
          platform_reported_wait_seconds: null,
          automatic_retry_allowed: false,
          verification_target_id: verificationTargetId,
        });
      } else {
        const riskState = applyRiskStrike(
          rateLimitState.platforms[platform] || {},
          RISK_CIRCUIT_BREAKER_POLICY,
          { platform, query_id: run.query_id, trigger_state: triggerState },
          { now_ms: Date.parse(triggeredAt) },
        );
        rateLimitState.platforms[platform] = {
          ...riskState,
          trigger_state: triggerState,
          trigger_query_id: run.query_id,
          triggered_at_utc: triggeredAt,
          trigger_evidence_dir: lastAutomationObservation.evidence_dir,
          last_automation_observation: lastAutomationObservation,
          manual_verification_required: false,
          platform_freeze_observed: false,
          platform_reported_wait_seconds: null,
          automatic_retry_allowed: false,
          requests_since_break: 0,
        };
        report.rate_limit_events.push({
          platform, query_id: run.query_id, trigger_state: triggerState,
          strike_count: riskState.strike_count,
          cooldown_tier_min_strike_count: riskState.cooldown_tier_min_strike_count,
          cooldown_minutes_applied: riskState.cooldown_minutes_applied,
          cooldown_triggered_at: riskState.cooldown_triggered_at,
          cooldown_until: riskState.cooldown_until,
          probe_required_after_cooldown: true,
          internal_safety_timer: true,
          platform_freeze_observed: false,
          platform_reported_wait_seconds: null,
          automatic_retry_allowed: false,
        });
      }
      await persistRateLimitState();
      if (cdpEndpoint && page && !page.isClosed() && isHumanVerificationState(triggerState)) {
        await page.bringToFront().catch(() => {});
        preservedVerificationPages.add(page);
        run.manual_verification_tab_kept_open = true;
        run.verification_page = {
          platform, query_id: run.query_id, state: triggerState,
          url: page.url(), title: run.title || null,
          target_id: await targetIdForPage(page),
        };
        page = await newOwnedPage();
        platformWorkPages.set(platform, page);
      }
    } else {
      const eligibleSuccessPage = run.page_runs.find((item) => (
        item.delivery_eligible === true && ['normal', 'zero_results'].includes(item.state)
      ));
      if (eligibleSuccessPage && Number(rateLimitState.platforms[platform]?.strike_count || 0) > 0) {
        const priorStrikeCount = Number(rateLimitState.platforms[platform].strike_count || 0);
        rateLimitState.platforms[platform] = resolvePacketEligibleSuccess(
          rateLimitState.platforms[platform],
          RISK_CIRCUIT_BREAKER_POLICY,
          { query_id: run.query_id, page_state: eligibleSuccessPage.state },
        );
        report.risk_recovery_events.push({
          platform,
          query_id: run.query_id,
          page_state: eligibleSuccessPage.state,
          packet_eligible: true,
          strategy: 'decrement_strike_on_packet_eligible_normal_or_zero',
          strike_count_before: priorStrikeCount,
          strike_count_after: rateLimitState.platforms[platform].strike_count,
          risk_probe: run.risk_probe === true,
        });
        await persistRateLimitState();
      } else if (run.risk_probe) {
        const probeState = run.page_runs.at(-1)?.state || run.final_state || 'inconclusive';
        rateLimitState.platforms[platform] = rearmInconclusiveProbe(
          rateLimitState.platforms[platform],
          RISK_CIRCUIT_BREAKER_POLICY,
          { query_id: run.query_id, page_state: probeState },
        );
        inconclusiveRiskProbePlatforms.add(platform);
        run.risk_probe_inconclusive = true;
        run.risk_probe_rearmed_until = rateLimitState.platforms[platform].cooldown_until;
        report.risk_recovery_events.push({
          platform,
          query_id: run.query_id,
          page_state: probeState,
          packet_eligible: false,
          strategy: 'same_tier_rearm_after_inconclusive_probe',
          strike_count: rateLimitState.platforms[platform].strike_count,
          cooldown_minutes_applied: rateLimitState.platforms[platform].cooldown_minutes_applied,
          cooldown_until: rateLimitState.platforms[platform].cooldown_until,
          risk_probe: true,
        });
        await persistRateLimitState();
      }
    }
    rateLimitState.platforms[platform] = {
      ...(rateLimitState.platforms[platform] || {}),
      last_task_finished_at: new Date().toISOString(),
    };
    await persistRateLimitState();
    await markTaskProgressComplete(run);
  }
  if (args['query-id'].length !== 1) {
    for (const platform of platforms) {
      const policy = ratePolicy(platform);
      const postBatchMinimum = Number(policy.post_batch_cooldown_min_minutes || 0);
      const postBatchMaximum = Number(policy.post_batch_cooldown_max_minutes || 0);
      if (!postBatchMaximum) continue;
      const attemptedRuns = report.platform_runs.filter((item) => (
        item.platform === platform && !String(item.final_state || '').startsWith('deferred_')
      ));
      if (!attemptedRuns.length) continue;
      const blockerObserved = attemptedRuns.some((item) => (
        [item.home_state, item.initial_state, item.final_state].some(isBlockingState)
        || (item.page_runs || []).some((pageRun) => isBlockingState(pageRun.state))
      ));
      if (blockerObserved) continue;
      const appliedCooldownMinutes = randomBetween(postBatchMinimum, postBatchMaximum);
      rateLimitState.platforms[platform] = {
        ...(rateLimitState.platforms[platform] || {}),
        post_batch_not_before: new Date(
          Date.now() + appliedCooldownMinutes * 60_000,
        ).toISOString(),
        post_batch_cooldown_minutes_applied: appliedCooldownMinutes,
      };
    }
    await persistRateLimitState();
  }
} finally {
  if (cdpEndpoint) {
    for (const candidate of ownedPages) {
      if (!candidate.isClosed() && !preservedVerificationPages.has(candidate)) {
        await candidate.close().catch(() => {});
      }
    }
    const verificationPage = [...preservedVerificationPages].filter((candidate) => !candidate.isClosed()).at(-1);
    if (verificationPage) await verificationPage.bringToFront().catch(() => {});
  } else {
    await context.close().catch(() => {});
  }
}

const reportPath = path.join(runDir, 'discovery', 'assisted-sales-results.json');
const jsonlPath = path.join(runDir, 'discovery', 'assisted-sales-results.jsonl');
if (args['merge-existing'] === true && existsSync(reportPath)) {
  const existing = JSON.parse(await fs.readFile(reportPath, 'utf8'));
  const replaced = new Set(platforms);
  const replacedQueries = new Set(args['query-id']);
  const selectedForRetry = (item) => replacedQueries.size
    ? replacedQueries.has(item.query_id)
    : replaced.has(item.platform);
  const keyFor = (item) => `${String(item.platform || '')}::${String(item.query_id || '')}`;
  const currentByKey = new Map(report.platform_runs.map((item) => [keyFor(item), item]));
  const preservedKeys = new Set();
  const retainedRuns = [];
  for (const prior of existing.platform_runs || []) {
    if (!selectedForRetry(prior)) {
      retainedRuns.push(prior);
      continue;
    }
    const key = keyFor(prior);
    const replacement = currentByKey.get(key);
    if (!replacement) {
      // A partial retry must never erase a previously captured planned query.
      retainedRuns.push(prior);
      preservedKeys.add(key);
      continue;
    }
    if (prior.delivery_eligible === true && replacement.delivery_eligible !== true) {
      retainedRuns.push({
        ...prior,
        preserved_after_non_delivery_retry: true,
        retry_observations: [
          ...(prior.retry_observations || []),
          {
            observed_at: new Date().toISOString(),
            final_state: replacement.final_state || null,
            delivery_eligible: false,
            errors: replacement.errors || [],
          },
        ],
      });
      preservedKeys.add(key);
    }
  }
  const retainedItems = (existing.items || []).filter((item) => {
    if (!selectedForRetry(item)) return true;
    const key = keyFor(item);
    return preservedKeys.has(key) || !currentByKey.has(key);
  });
  const freshRuns = report.platform_runs.filter((item) => !preservedKeys.has(keyFor(item)));
  const freshItems = report.items.filter((item) => !preservedKeys.has(keyFor(item)));
  report.platform_runs = [...retainedRuns, ...freshRuns];
  report.items = [...retainedItems, ...freshItems];
  report.platforms_requested = [...new Set([
    ...(existing.platforms_requested || []), ...report.platforms_requested,
  ])];
  report.incremental_merge = true;
}
report.finished_at = new Date().toISOString();
report.query_count = report.platform_runs.length;
report.result_count = report.items.length;
report.delivery_eligible_query_count = report.platform_runs.filter((item) => item.delivery_eligible).length;
const fullPlannedTasks = taskSource === 'manual-capture-queue'
  ? (manualQueue.items || [])
  : (plan.items || []);
const deliveryCoverage = summarizePlatformEligibility(
  fullPlannedTasks, report.platform_runs, 'delivery_eligible',
);
report.platforms_with_any_delivery_eligible_query = deliveryCoverage.platforms_with_any;
report.platforms_with_any_delivery_eligible_query_count = deliveryCoverage.platforms_with_any.length;
report.delivery_platforms = deliveryCoverage.complete_platforms;
report.delivery_platform_count = report.delivery_platforms.length;
report.filing_eligible_query_count = report.platform_runs.filter((item) => item.filing_eligible).length;
const filingCoverage = summarizePlatformEligibility(
  fullPlannedTasks, report.platform_runs, 'filing_eligible',
);
report.platforms_with_any_filing_eligible_query = filingCoverage.platforms_with_any;
report.platforms_with_any_filing_eligible_query_count = filingCoverage.platforms_with_any.length;
report.filing_eligible_platforms = filingCoverage.complete_platforms;
report.filing_eligible_platform_count = report.filing_eligible_platforms.length;
report.failed_query_count = report.platform_runs.length - report.delivery_eligible_query_count;
report.execution_finished = true;
report.matrix_complete = Boolean(
  fullPlannedTasks.length
  && report.platform_runs.length === fullPlannedTasks.length
  && report.delivery_eligible_query_count === fullPlannedTasks.length
);
report.verification_tabs_preserved = report.platform_runs.filter(
  (item) => item.manual_verification_tab_kept_open === true,
).map((item) => item.verification_page);
await writeTextAtomic(reportPath, `${JSON.stringify(report, null, 2)}\n`);
await writeTextAtomic(jsonlPath, report.items.map((item) => JSON.stringify(item)).join('\n') + (report.items.length ? '\n' : ''));
await updateLiveProgress({
  state: 'finished',
  stage: 'finished',
  stage_deadline_at: null,
  artifact_lock_held: false,
  active_task: null,
  processed_task_count: report.platform_runs.filter((value) => value.final_state).length,
  completed_task_count: report.delivery_eligible_query_count,
  finished_at: report.finished_at,
});
console.log(JSON.stringify({
  execution_finished: true,
  matrix_complete: report.matrix_complete,
  manual_interaction_required: report.manual_interaction_required,
  result_count: report.result_count,
  query_count: report.query_count,
  delivery_eligible_query_count: report.delivery_eligible_query_count,
  delivery_platform_count: report.delivery_platform_count,
  filing_eligible_query_count: report.filing_eligible_query_count,
  filing_eligible_platform_count: report.filing_eligible_platform_count,
  pages_per_query_requested: report.pages_per_query_requested,
  platform_runs: report.platform_runs.map(({ platform, query_id, target_good, search_query, initial_state, final_state, result_count, delivery_eligible, filing_eligible, pages_captured, pages_requested, pagination_status }) => ({
    platform, query_id, target_good, search_query, initial_state, final_state, result_count,
    delivery_eligible, filing_eligible, pages_captured, pages_requested, pagination_status,
  })),
  output: reportPath,
}, null, 2));
if (cdpEndpoint) {
  await new Promise((resolve) => process.stdout.write('', resolve));
  process.exit(0);
}
