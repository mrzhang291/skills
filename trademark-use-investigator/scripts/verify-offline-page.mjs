#!/usr/bin/env node

import fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

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

function split(value) {
  return String(value || '').split('|||').map((item) => item.trim()).filter(Boolean);
}

function normalize(value) {
  return String(value || '').normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
}

function shingles(value, size = 3, maxChars = 100000) {
  const text = normalize(value).slice(0, maxChars);
  const result = new Set();
  for (let index = 0; index <= text.length - size; index += 1) result.add(text.slice(index, index + size));
  return result;
}

function retention(online, offline) {
  const original = shingles(online);
  if (!original.size) return 1;
  const archived = shingles(offline);
  let matched = 0;
  for (const value of original) if (archived.has(value)) matched += 1;
  return matched / original.size;
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

let chromium;
try {
  ({ chromium } = require('playwright-core'));
} catch {
  ({ chromium } = require('playwright'));
}

const args = parseArgs(process.argv.slice(2));
if (!args.html || !args.output) {
  console.error('Usage: verify-offline-page.mjs --html <singlefile.html> --output <offline-validation.json> [--online-text file]');
  process.exit(1);
}

const htmlPath = path.resolve(args.html);
const outputPath = path.resolve(args.output);
const screenshotPath = path.resolve(args.screenshot || path.join(path.dirname(outputPath), 'offline.png'));
const browserExecutable = findBrowserExecutable(args['browser-executable']);
const expectedAll = split(args['expected-text-all']);
const expectedAny = split(args['expected-text-any']);
const onlineText = args['online-text'] && existsSync(path.resolve(args['online-text']))
  ? await fs.readFile(path.resolve(args['online-text']), 'utf8') : '';
const onlineImages = Math.max(0, Number.parseInt(args['online-substantive-images'] || '0', 10));

const report = {
  schema_version: '2.0',
  record_type: 'offline_replay_validation',
  html: htmlPath,
  checked_at: new Date().toISOString(),
  ok: false,
  load_succeeded: false,
  external_request_count: 0,
  external_requests: [],
  visible_text_chars: 0,
  online_visible_text_chars: onlineText.length,
  text_retention: 0,
  substantive_images_online: onlineImages,
  substantive_images_offline: 0,
  loaded_substantive_images_offline: 0,
  image_preservation_rate: onlineImages ? 0 : 1,
  expected_all_present: false,
  expected_any_present: expectedAny.length === 0,
  errors: [],
};

let browser;
try {
  if (!existsSync(htmlPath)) throw new Error(`Offline HTML not found: ${htmlPath}`);
  if (!browserExecutable) throw new Error('Chrome/Edge executable not found');
  browser = await chromium.launch({ executablePath: browserExecutable, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.setOffline(true);
  await context.route(/https?:\/\//i, async (route) => {
    const url = route.request().url();
    report.external_requests.push(url);
    await route.abort();
  });
  const page = await context.newPage();
  try {
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'domcontentloaded', timeout: 30000 });
  } catch (error) {
    report.errors.push({ stage: 'navigation_timeout_nonfatal', message: String(error?.message || error) });
  }
  await page.waitForTimeout(800);
  const values = await page.evaluate(() => {
    const text = document.body?.innerText || '';
    const images = [...document.images].map((img) => {
      const rect = img.getBoundingClientRect();
      const substantive = rect.width >= 80 && rect.height >= 60;
      return { substantive, loaded: img.complete && img.naturalWidth > 0 && img.naturalHeight > 0 };
    });
    return { title: document.title, text, images };
  });
  report.load_succeeded = true;
  report.title = values.title;
  report.visible_text_chars = values.text.length;
  report.text_retention = retention(onlineText, values.text);
  const substantive = values.images.filter((item) => item.substantive);
  report.substantive_images_offline = substantive.length;
  report.loaded_substantive_images_offline = substantive.filter((item) => item.loaded).length;
  report.image_preservation_rate = onlineImages
    ? Math.min(1, report.loaded_substantive_images_offline / onlineImages)
    : 1;
  report.expected_all_present = expectedAll.every((item) => values.text.includes(item));
  report.expected_any_present = expectedAny.length === 0 || expectedAny.some((item) => values.text.includes(item));
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch((error) => {
    report.errors.push({ stage: 'screenshot', message: String(error?.message || error) });
  });
  report.external_request_count = report.external_requests.length;
  const minimumText = onlineText.length < 120 ? Math.min(40, onlineText.length) : 120;
  report.ok = report.load_succeeded
    && report.external_request_count === 0
    && report.visible_text_chars >= minimumText
    && report.text_retention >= (onlineText.length ? 0.65 : 1)
    && report.image_preservation_rate >= 0.8
    && report.expected_all_present
    && report.expected_any_present;
  await context.close();
} catch (error) {
  report.errors.push({ stage: 'offline_replay', message: String(error?.message || error) });
} finally {
  if (browser) await browser.close().catch(() => {});
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
}

console.log(JSON.stringify(report, null, 2));
if (!report.ok) process.exitCode = 3;
