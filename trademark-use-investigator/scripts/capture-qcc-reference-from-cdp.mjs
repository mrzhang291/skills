#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import {
  buildQccRegistrationSearchUrl,
  isExactQccBrandUrl as isExactBrandUrl,
  rankQccBrandLinkRecords,
} from './qcc-search-support.mjs';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright-core')); }
catch { ({ chromium } = require('playwright')); }

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

function requireLoopbackEndpoint(raw) {
  let parsed;
  try { parsed = new URL(String(raw || '')); } catch { throw new Error('Invalid --cdp-endpoint URL'); }
  if (!['http:', 'https:'].includes(parsed.protocol)
      || !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(parsed.hostname)
      || !parsed.port || parsed.username || parsed.password
      || !['', '/'].includes(parsed.pathname) || parsed.search || parsed.hash) {
    throw new Error('--cdp-endpoint must be an HTTP(S) loopback origin with an explicit port');
  }
  return parsed.toString().replace(/\/$/, '');
}

function normalized(value) {
  return String(value || '').replace(/\s+/g, '');
}

function sha256Bytes(value) {
  return createHash('sha256').update(value).digest('hex');
}

async function pageContainsIdentity(page, registrationNumber, markName, owner) {
  const expected = [registrationNumber, markName, owner].map((value) => normalized(value));
  return page.waitForFunction((values) => {
    const text = String(document.body?.innerText || '').replace(/\s+/g, '');
    return values.every((value) => text.includes(value));
  }, expected, { timeout: 10000 }).then(() => true).catch(() => false);
}

async function exactBrandLinks(page, registrationNumber, markName, owner) {
  const values = await page.locator('a[href*="/brandDetail/"]').evaluateAll((links) => links.map((link, index) => ({
    href: link.href || '',
    index,
    contextText: String((link.closest('tr,li,[class*="item"],[class*="row"]') || link.parentElement)?.innerText || ''),
  })))
    .catch(() => []);
  return rankQccBrandLinkRecords(values, registrationNumber, markName, owner, 8);
}

async function waitForQccSearchOutcome(page, registrationNumber) {
  await page.waitForFunction((registration) => {
    const text = String(document.body?.innerText || '').replace(/\s+/g, '');
    const target = String(registration || '').replace(/\s+/g, '');
    const zeroResult = /暂无(?:相关)?(?:数据|结果)|未找到|没有找到|无相关结果/.test(text);
    return (target && text.includes(target)) || zeroResult;
  }, registrationNumber, { timeout: 15000 }).catch(() => {});
}

async function locateBrandDetailFromQccSearch(context, registrationNumber, markName, owner) {
  const searchUrl = buildQccRegistrationSearchUrl(registrationNumber);
  // Never repurpose a user's QCC login, verification, or detail tab.  The
  // fallback search owns a fresh page and may close only that page.
  const searchPage = await context.newPage();
  try {
    await searchPage.bringToFront().catch(() => {});
    // Always submit the registration number explicitly.  Pre-existing QCC
    // links may be defaults or results from a previous query.
    await searchPage.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await waitForQccSearchOutcome(searchPage, registrationNumber);
    let links = await exactBrandLinks(searchPage, registrationNumber, markName, owner);
    if (!links.length) {
      const inputs = searchPage.locator([
        'input[placeholder*="商标"]', 'input[placeholder*="请输入"]',
        'input[type="search"]', 'input[type="text"]',
      ].join(','));
      const count = Math.min(await inputs.count().catch(() => 0), 12);
      for (let index = 0; index < count; index += 1) {
        const input = inputs.nth(index);
        if (!await input.isVisible().catch(() => false)) continue;
        await input.fill(registrationNumber).catch(() => {});
        await input.press('Enter').catch(() => {});
        await waitForQccSearchOutcome(searchPage, registrationNumber);
        links = await exactBrandLinks(searchPage, registrationNumber, markName, owner);
        if (links.length || isExactBrandUrl(searchPage.url())) break;
      }
    }
    if (isExactBrandUrl(searchPage.url())
        && await pageContainsIdentity(searchPage, registrationNumber, markName, owner)) {
      return searchPage;
    }
    for (const link of links) {
      const detailPage = await searchPage.context().newPage();
      try {
        await detailPage.goto(link, { waitUntil: 'domcontentloaded', timeout: 20000 });
        if (await pageContainsIdentity(detailPage, registrationNumber, markName, owner)) return detailPage;
      } catch {}
      await detailPage.close().catch(() => {});
    }
  } finally {
    if (!searchPage.isClosed() && !isExactBrandUrl(searchPage.url())) {
      await searchPage.close().catch(() => {});
    }
  }
  return null;
}

const args = parseArgs(process.argv.slice(2));
const cdpEndpoint = requireLoopbackEndpoint(args['cdp-endpoint']);
const runDir = path.resolve(String(args['run-dir'] || ''));
let brandUrl = String(args['brand-url'] || '').trim();
const registrationNumber = String(args['registration-number'] || '').trim();
const markName = String(args['mark-name'] || '').trim();
const owner = String(args.owner || '').trim();
const expectedBrowser = String(args['browser-product'] || '').trim().toLowerCase();
if (!runDir || !registrationNumber || !markName || !owner) {
  throw new Error('--run-dir, --registration-number, --mark-name and --owner are required');
}
if (!['edge', 'chrome'].includes(expectedBrowser)) {
  throw new Error('--browser-product must explicitly be edge or chrome');
}

if (brandUrl && !isExactBrandUrl(brandUrl)) throw new Error('--brand-url must be an exact QCC brandDetail URL');

const versionResponse = await fetch(`${cdpEndpoint}/json/version`);
if (!versionResponse.ok) throw new Error(`CDP version probe failed: HTTP ${versionResponse.status}`);
const cdpVersion = await versionResponse.json();
const detectedBrowserProduct = String(cdpVersion.Browser || '').trim();
const product = detectedBrowserProduct.toLowerCase();
const browserProductMatches = expectedBrowser === 'edge'
  ? (product.includes('edg/') || product.includes('microsoft edge'))
  : (product.includes('chrome/') && !product.includes('edg/'));
if (!browserProductMatches) {
  throw new Error(`CDP browser product mismatch: expected ${expectedBrowser}, got ${detectedBrowserProduct || 'unknown'}`);
}

const browser = await chromium.connectOverCDP(cdpEndpoint);
try {
  const contexts = browser.contexts();
  const context = contexts[0];
  if (!context) throw new Error('No browser context is available at the CDP endpoint');
  const pages = contexts.flatMap((value) => value.pages());
  let resolutionMode = 'already_open_exact_brand_detail';
  let page = brandUrl
    ? (pages.find((candidate) => candidate.url() === brandUrl)
      || pages.find((candidate) => candidate.url().startsWith(brandUrl)))
    : null;
  if (!page && brandUrl) {
    const directPage = await context.newPage();
    resolutionMode = 'known_exact_brand_detail_direct_navigation';
    await directPage.goto(brandUrl, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await directPage.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
    if (isExactBrandUrl(directPage.url())
        && await pageContainsIdentity(directPage, registrationNumber, markName, owner)) {
      page = directPage;
    } else {
      await directPage.bringToFront().catch(() => {});
      throw new Error(
        'The known exact QCC brandDetail URL did not resolve to the expected identity; '
        + 'complete the visible QCC login/verification and resume without using a generic search tab',
      );
    }
  }
  if (!page) {
    const brandPages = pages.filter((candidate) => isExactBrandUrl(candidate.url()));
    for (const candidate of brandPages) {
      await candidate.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
      const text = await candidate.locator('body').innerText({ timeout: 5000 }).catch(() => '');
      if ([registrationNumber, markName, owner].every((value) => normalized(text).includes(normalized(value)))) {
        page = candidate;
        brandUrl = candidate.url();
        break;
      }
    }
  }
  if (!page) {
    page = await locateBrandDetailFromQccSearch(context, registrationNumber, markName, owner);
    if (page) {
      brandUrl = page.url();
      resolutionMode = 'automatic_qcc_trademark_search';
    }
  }
  if (!page) throw new Error('No matching QCC brandDetail could be resolved automatically from the attached Edge/Chrome');
  if (!brandUrl) brandUrl = page.url();
  if (!isExactBrandUrl(brandUrl)) throw new Error('The matched QCC tab is not an exact brandDetail URL');
  await page.bringToFront();
  await page.waitForLoadState('domcontentloaded', { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1500);

  const finalUrl = page.url();
  if (!isExactBrandUrl(finalUrl)) throw new Error('The final live QCC page is not an exact brandDetail URL');
  if (brandUrl !== finalUrl) {
    throw new Error(`QCC source/final URL mismatch: ${brandUrl} != ${finalUrl}`);
  }

  const visibleText = await page.locator('body').innerText({ timeout: 20000 });
  for (const [label, expected] of [
    ['registration number', registrationNumber],
    ['mark name', markName],
    ['owner', owner],
  ]) {
    if (!normalized(visibleText).includes(normalized(expected))) {
      throw new Error(`The live QCC page does not contain the expected ${label}`);
    }
  }

  const candidates = await page.locator('img[src*="trademark-img.qcc.com"]').evaluateAll((images) => images.map((image, index) => {
    const rect = image.getBoundingClientRect();
    return {
      index,
      src: image.currentSrc || image.src || '',
      top: rect.top + window.scrollY,
      left: rect.left + window.scrollX,
      width: rect.width,
      height: rect.height,
      naturalWidth: image.naturalWidth,
      naturalHeight: image.naturalHeight,
      className: image.className || '',
    };
  }).filter((item) => item.src && item.naturalWidth >= 100 && item.naturalHeight >= 100));
  candidates.sort((left, right) => left.top - right.top || left.left - right.left || left.index - right.index);
  const selected = candidates.find((item) => String(item.className).includes('task-img')) || candidates[0];
  if (!selected || !selected.src.startsWith('https://trademark-img.qcc.com/')) {
    throw new Error('The main QCC trademark image was not found in the rendered page');
  }

  const response = await page.context().request.get(selected.src, {
    headers: { Referer: brandUrl },
    timeout: 30000,
  });
  if (!response.ok()) throw new Error(`QCC trademark image request failed: HTTP ${response.status()}`);
  const contentType = String(response.headers()['content-type'] || '').toLowerCase();
  if (!contentType.startsWith('image/')) throw new Error(`QCC trademark response is not an image: ${contentType}`);
  const imageBytes = await response.body();
  if (imageBytes.length < 1000) throw new Error('QCC trademark image payload is unexpectedly small');

  const referenceDir = path.join(runDir, 'reference');
  await fs.mkdir(referenceDir, { recursive: true });
  const htmlPath = path.join(referenceDir, 'qcc-live-page.html');
  const imageExtension = contentType.includes('png') ? 'png' : 'jpg';
  const imagePath = path.join(referenceDir, `qcc-live-image.${imageExtension}`);
  const metadataPath = path.join(referenceDir, 'qcc-live-capture.json');
  const htmlBytes = Buffer.from(await page.content(), 'utf8');
  await fs.writeFile(htmlPath, htmlBytes);
  await fs.writeFile(imagePath, imageBytes);
  const metadata = {
    schema_version: '1.0',
    record_type: 'qcc_live_dom_capture',
    source_url: brandUrl,
    final_url: finalUrl,
    page_title: await page.title(),
    captured_at: new Date().toISOString(),
    cdp_attach_mode: true,
    cdp_endpoint: cdpEndpoint,
    cdp_endpoint_loopback: true,
    cdp_browser: detectedBrowserProduct,
    expected_browser_product: expectedBrowser,
    browser_product_validated: true,
    identity_validated: true,
    identity_validation: {
      exact_brand_url: true,
      source_final_url_match: true,
      registration_number: true,
      mark_name: true,
      owner: true,
    },
    resolution_mode: resolutionMode,
    registration_number: registrationNumber,
    name: markName,
    owner,
    image_url: selected.src,
    has_image: true,
    image_content_type: contentType,
    image_natural_width: selected.naturalWidth,
    image_natural_height: selected.naturalHeight,
    html_file: path.relative(runDir, htmlPath).replaceAll('\\', '/'),
    html_bytes: htmlBytes.length,
    html_sha256: sha256Bytes(htmlBytes),
    image_file: path.relative(runDir, imagePath).replaceAll('\\', '/'),
    image_bytes: imageBytes.length,
    image_sha256: sha256Bytes(imageBytes),
  };
  await fs.writeFile(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`, 'utf8');
  process.stdout.write(`${JSON.stringify({ ok: true, htmlPath, imagePath, metadataPath, sourceUrl: brandUrl }, null, 2)}\n`);
} catch (error) {
  console.error(String(error?.stack || error));
  process.exit(1);
}
// End only this CDP client process; the signed-in Edge/Chrome belongs to the employee.
process.exit(0);
