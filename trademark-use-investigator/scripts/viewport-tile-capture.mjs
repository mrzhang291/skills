#!/usr/bin/env node

import fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const LAZY_ATTRIBUTES = ['data-src', 'data-lazy-src', 'data-ks-lazyload', 'data-original', 'data-img'];
const MIN_CAPTURE_WALL_TIMEOUT_MS = 15_000;
const MAX_CAPTURE_WALL_TIMEOUT_MS = 90_000;
const PROCESS_TERMINATION_GRACE_MS = 2_000;
const NON_RETRYABLE_CAPTURE_CODES = new Set([
  'EACCES', 'EISDIR', 'EMFILE', 'ENFILE', 'ENOENT', 'ENOSPC', 'ENOTDIR', 'EPERM', 'EROFS',
]);
const NON_RETRYABLE_CAPTURE_MESSAGE = /(?:page|target|browser|context)\s+(?:has been\s+)?closed|execution context (?:was )?destroyed|frame was detached|browser disconnected|permission denied|no space left|read-only file system/i;

export function isRecoverableViewportCaptureError(error) {
  if (!error) return false;
  const code = String(error.code || '').toUpperCase();
  const operation = String(error.operation || '');
  const message = [
    error.message, error.playwright_error, error.cdp_error, error.cause?.message,
  ].filter(Boolean).join('; ');
  if (NON_RETRYABLE_CAPTURE_CODES.has(code) || NON_RETRYABLE_CAPTURE_MESSAGE.test(message)) {
    return false;
  }
  if (code === 'BROWSER_OPERATION_TIMEOUT') {
    return /^(?:playwright_viewport_screenshot|cdp_session_create|cdp_page_capture_screenshot)$/.test(operation);
  }
  if (code === 'VIEWPORT_SCREENSHOT_FAILED') return true;
  return /viewport screenshot failed in Playwright and CDP|CDP Page\.captureScreenshot returned no data|(?:playwright_viewport_screenshot|cdp_session_create|cdp_page_capture_screenshot)_timeout_after_/i.test(message);
}

export function normalizeCaptureWallTimeoutMs(value) {
  const requested = Number(value);
  if (!Number.isFinite(requested) || requested <= 0) return MAX_CAPTURE_WALL_TIMEOUT_MS;
  return Math.max(MIN_CAPTURE_WALL_TIMEOUT_MS, Math.min(MAX_CAPTURE_WALL_TIMEOUT_MS, requested));
}

export async function withOperationTimeout(operation, timeoutMs, label = 'browser_operation', onTimeout = null) {
  const boundedMs = Math.max(100, Number(timeoutMs || 0));
  let timer = null;
  const work = Promise.resolve().then(() => (
    typeof operation === 'function' ? operation() : operation
  ));
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      if (typeof onTimeout === 'function') {
        Promise.resolve().then(onTimeout).catch(() => {});
      }
      const error = new Error(`${label}_timeout_after_${boundedMs}ms`);
      error.code = 'BROWSER_OPERATION_TIMEOUT';
      error.operation = label;
      error.timeout_ms = boundedMs;
      reject(error);
    }, boundedMs);
  });
  try {
    return await Promise.race([work, deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function terminateChildBounded(child, graceMs = PROCESS_TERMINATION_GRACE_MS) {
  const boundedGraceMs = Math.max(100, Number(graceMs || 0));
  if (!child || child.exitCode != null || child.signalCode != null) {
    return {
      pid: child?.pid || null, kill_requested: false, exited: true,
      exit_code: child?.exitCode ?? null, signal: child?.signalCode ?? null,
      grace_ms: boundedGraceMs, error: null,
    };
  }
  let closeListener = null;
  let graceTimer = null;
  const closed = new Promise((resolve) => {
    closeListener = (exitCode, signal) => resolve({ exited: true, exitCode, signal });
    child.once('close', closeListener);
  });
  let killRequested = false;
  let killError = null;
  try {
    killRequested = child.kill();
  } catch (error) {
    killError = String(error?.message || error || 'child.kill failed');
  }
  const graceExpired = new Promise((resolve) => {
    graceTimer = setTimeout(() => resolve({ exited: false, exitCode: null, signal: null }), boundedGraceMs);
  });
  let outcome = await Promise.race([closed, graceExpired]);
  if (graceTimer) clearTimeout(graceTimer);
  if (!outcome.exited && (child.exitCode != null || child.signalCode != null)) {
    outcome = { exited: true, exitCode: child.exitCode, signal: child.signalCode };
  }
  if (!outcome.exited && closeListener) child.removeListener('close', closeListener);
  return {
    pid: child.pid || null,
    kill_requested: killRequested,
    exited: outcome.exited,
    exit_code: outcome.exitCode,
    signal: outcome.signal,
    grace_ms: boundedGraceMs,
    error: killError || (outcome.exited ? null : `child_did_not_exit_after_${boundedGraceMs}ms`),
  };
}

export async function runProcessBounded(command, args, timeoutMs, onProcess = null) {
  const boundedMs = Math.max(100, Number(timeoutMs || 0));
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(command, args, {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch (error) {
      reject(error);
      return;
    }
    if (typeof onProcess === 'function') onProcess(child);
    let stdout = '';
    let stderr = '';
    let timer = null;
    let settled = false;
    let terminating = false;
    const finish = (error, result = null, retainProcess = false) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (!retainProcess && typeof onProcess === 'function') onProcess(null);
      if (error) reject(error);
      else resolve(result);
    };
    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');
    child.stdout?.on('data', (chunk) => { stdout += chunk; });
    child.stderr?.on('data', (chunk) => { stderr += chunk; });
    child.once('error', (error) => {
      if (!terminating) finish(error);
    });
    child.once('close', (status, signal) => {
      if (!terminating) finish(null, { status, signal, stdout, stderr });
    });
    timer = setTimeout(async () => {
      if (settled) return;
      terminating = true;
      const error = new Error(`tile_stitch_helper_timeout_after_${boundedMs}ms`);
      error.code = 'BROWSER_OPERATION_TIMEOUT';
      error.operation = 'tile_stitch_helper';
      error.timeout_ms = boundedMs;
      const cleanup = await terminateChildBounded(child);
      error.process_cleanup = cleanup;
      if (!cleanup.exited) {
        error.cleanup_error = cleanup.error;
        error.cleanup_process = child;
      }
      finish(error, null, !cleanup.exited);
    }, boundedMs);
  });
}

function tagViewportScreenshotError(error, operation) {
  const tagged = error instanceof Error ? error : new Error(String(error || 'viewport screenshot failed'));
  if (!tagged.code) tagged.code = 'VIEWPORT_SCREENSHOT_FAILED';
  if (!tagged.operation) tagged.operation = operation;
  return tagged;
}

async function detachSessionBounded(session) {
  if (!session) return;
  await withOperationTimeout(() => session.detach(), 2_000, 'cdp_session_detach').catch(() => {});
}

async function prepareViewport(page, targetY, hideFloating, delayMs) {
  await page.evaluate(({ y, hide, lazyAttributes }) => {
    document.documentElement.style.setProperty('scroll-behavior', 'auto', 'important');
    const root = document.scrollingElement || document.documentElement;
    window.scrollTo(0, Math.max(0, y));
    for (const img of document.images) {
      const rect = img.getBoundingClientRect();
      if (rect.bottom < -200 || rect.top > window.innerHeight + 200) continue;
      img.loading = 'eager';
      const current = String(img.currentSrc || img.getAttribute('src') || '').trim();
      if (!current || /^data:image\/(?:gif|svg\+xml);base64,/i.test(current)) {
        const intended = lazyAttributes.map((name) => img.getAttribute(name))
          .find((value) => value && !/^data:image\/(?:gif|svg\+xml);base64,/i.test(value));
        if (intended) img.setAttribute('src', intended);
      }
      const lazySrcset = img.getAttribute('data-srcset');
      if (lazySrcset && !img.getAttribute('srcset')) img.setAttribute('srcset', lazySrcset);
    }
    for (const element of document.querySelectorAll('body *')) {
      const position = getComputedStyle(element).position;
      if (position !== 'fixed' && position !== 'sticky' && position !== '-webkit-sticky') continue;
      const rect = element.getBoundingClientRect();
      if (rect.height <= 0 || rect.height >= window.innerHeight * 0.45) continue;
      if (!element.hasAttribute('data-tmui-original-visibility')) {
        element.setAttribute('data-tmui-original-visibility', element.style.visibility || '');
      }
      if (hide) element.style.setProperty('visibility', 'hidden', 'important');
      else element.style.visibility = element.getAttribute('data-tmui-original-visibility') || '';
    }
    root.scrollTop = Math.max(0, y);
  }, { y: targetY, hide: hideFloating, lazyAttributes: LAZY_ATTRIBUTES });
  await page.waitForTimeout(delayMs);
  await page.evaluate(async ({ lazyAttributes, hide }) => {
    for (const img of document.images) {
      const rect = img.getBoundingClientRect();
      if (rect.bottom < -100 || rect.top > window.innerHeight + 100) continue;
      img.loading = 'eager';
      const current = String(img.currentSrc || img.getAttribute('src') || '').trim();
      if (!current || /^data:image\/(?:gif|svg\+xml);base64,/i.test(current)) {
        const intended = lazyAttributes.map((name) => img.getAttribute(name))
          .find((value) => value && !/^data:image\/(?:gif|svg\+xml);base64,/i.test(value));
        if (intended) img.setAttribute('src', intended);
      }
    }
    if (hide) {
      for (const element of document.querySelectorAll('body *')) {
        const position = getComputedStyle(element).position;
        if (position !== 'fixed' && position !== 'sticky' && position !== '-webkit-sticky') continue;
        const rect = element.getBoundingClientRect();
        if (rect.height <= 0 || rect.height >= window.innerHeight * 0.45) continue;
        if (!element.hasAttribute('data-tmui-original-visibility')) {
          element.setAttribute('data-tmui-original-visibility', element.style.visibility || '');
        }
        element.style.setProperty('visibility', 'hidden', 'important');
      }
    }
    const visible = [...document.images].filter((img) => {
      const rect = img.getBoundingClientRect();
      return rect.width >= 40 && rect.height >= 35 && rect.bottom > 0 && rect.top < window.innerHeight;
    });
    await Promise.race([
      Promise.allSettled(visible.map((img) => img.decode?.().catch(() => undefined))),
      new Promise((resolve) => setTimeout(resolve, 2500)),
    ]);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, { lazyAttributes: LAZY_ATTRIBUTES, hide: hideFloating }).catch(() => {});
}

async function observeViewport(page, index) {
  return page.evaluate(({ tileIndex }) => {
    const root = document.scrollingElement || document.documentElement;
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 1;
    const paintBoxes = [];
    const sourceKeys = [];
    let substantive = 0;
    let unresolved = 0;
    for (const img of document.images) {
      const rect = img.getBoundingClientRect();
      const visible = rect.width >= 40 && rect.height >= 35 && rect.right > 0 && rect.left < viewportWidth
        && rect.bottom > 0 && rect.top < viewportHeight;
      if (!visible) continue;
      substantive += 1;
      const loaded = img.complete && img.naturalWidth > 1 && img.naturalHeight > 1;
      if (!loaded) {
        unresolved += 1;
        continue;
      }
      const source = String(img.currentSrc || img.src || '').trim();
      sourceKeys.push(source || `img:${tileIndex}:${paintBoxes.length}`);
      paintBoxes.push({
        x: Math.max(0, rect.left), y: Math.max(0, rect.top),
        width: Math.min(viewportWidth, rect.right) - Math.max(0, rect.left),
        height: Math.min(viewportHeight, rect.bottom) - Math.max(0, rect.top),
        kind: 'img',
      });
    }
    for (const element of document.querySelectorAll('body *')) {
      const rect = element.getBoundingClientRect();
      if (rect.width < 60 || rect.height < 45 || rect.width > viewportWidth * 0.8 || rect.height > viewportHeight * 0.8) continue;
      if (rect.right <= 0 || rect.left >= viewportWidth || rect.bottom <= 0 || rect.top >= viewportHeight) continue;
      const background = getComputedStyle(element).backgroundImage || '';
      const match = background.match(/url\(["']?([^"')]+)["']?\)/i);
      if (!match) continue;
      sourceKeys.push(match[1]);
      paintBoxes.push({
        x: Math.max(0, rect.left), y: Math.max(0, rect.top),
        width: Math.min(viewportWidth, rect.right) - Math.max(0, rect.left),
        height: Math.min(viewportHeight, rect.bottom) - Math.max(0, rect.top),
        kind: 'background',
      });
      if (paintBoxes.length >= 180) break;
    }
    return {
      index: tileIndex,
      scroll_y_css: window.scrollY || root.scrollTop || 0,
      viewport_width_css: viewportWidth,
      viewport_height_css: viewportHeight,
      document_height_css: Math.max(root.scrollHeight, document.body?.scrollHeight || 0),
      paint_boxes: paintBoxes.slice(0, 180),
      paint_source_keys: sourceKeys.slice(0, 300),
      visible_substantive_images: substantive,
      visible_unresolved_images: unresolved,
    };
  }, { tileIndex: index });
}

async function restorePage(page) {
  await page.evaluate(() => {
    for (const element of document.querySelectorAll('[data-tmui-original-visibility]')) {
      element.style.visibility = element.getAttribute('data-tmui-original-visibility') || '';
      element.removeAttribute('data-tmui-original-visibility');
    }
    window.scrollTo(0, 0);
  }).catch(() => {});
}

export async function captureViewportScreenshot(page, outputPath, options = {}) {
  const timeout = Math.max(3000, Math.min(30000, Number(options.timeoutMs || 15000)));
  const protocolTimeout = Math.max(3_000, Math.min(30_000, Number(options.protocolTimeoutMs || timeout)));
  const backendState = options.backendState && typeof options.backendState === 'object'
    ? options.backendState : null;
  if (backendState?.forceCdp === true) {
    let session = null;
    try {
      session = await withOperationTimeout(
        () => page.context().newCDPSession(page), Math.min(protocolTimeout, 5_000), 'cdp_session_create',
      );
      const captured = await withOperationTimeout(
        () => session.send('Page.captureScreenshot', {
          format: 'png', fromSurface: true, captureBeyondViewport: false,
        }),
        protocolTimeout,
        'cdp_page_capture_screenshot',
        () => detachSessionBounded(session),
      );
      if (!captured?.data) throw new Error('CDP Page.captureScreenshot returned no data');
      await fs.writeFile(outputPath, Buffer.from(captured.data, 'base64'));
      return {
        method: 'cdp_page_capture_screenshot', playwright_error: null,
        playwright_bypassed_after_prior_failure: true,
      };
    } catch (error) {
      throw tagViewportScreenshotError(error, 'cdp_page_capture_screenshot');
    } finally {
      await detachSessionBounded(session);
    }
  }
  try {
    await page.screenshot({
      path: outputPath, fullPage: false, animations: 'disabled', caret: 'hide', timeout,
    });
    return { method: 'playwright_viewport_screenshot', playwright_error: null };
  } catch (playwrightError) {
    let session = null;
    try {
      session = await withOperationTimeout(
        () => page.context().newCDPSession(page), Math.min(protocolTimeout, 5_000), 'cdp_session_create',
      );
      const captured = await withOperationTimeout(
        () => session.send('Page.captureScreenshot', {
          format: 'png', fromSurface: true, captureBeyondViewport: false,
        }),
        protocolTimeout,
        'cdp_page_capture_screenshot',
        () => detachSessionBounded(session),
      );
      if (!captured?.data) throw new Error('CDP Page.captureScreenshot returned no data');
      await fs.writeFile(outputPath, Buffer.from(captured.data, 'base64'));
      if (backendState) {
        backendState.forceCdp = true;
        backendState.switched_at = new Date().toISOString();
        backendState.reason = 'playwright_failed_cdp_succeeded';
      }
      return {
        method: 'cdp_page_capture_screenshot',
        playwright_error: String(playwrightError?.message || playwrightError || ''),
      };
    } catch (cdpError) {
      const error = new Error(
        `viewport screenshot failed in Playwright and CDP: ${String(playwrightError?.message || playwrightError)}; `
        + `${String(cdpError?.message || cdpError)}`,
      );
      error.code = cdpError?.code || 'VIEWPORT_SCREENSHOT_FAILED';
      error.operation = cdpError?.operation || 'viewport_screenshot_fallback';
      error.playwright_error = String(playwrightError?.message || playwrightError || '');
      error.cdp_error = String(cdpError?.message || cdpError || '');
      error.cause = cdpError;
      throw error;
    } finally {
      await detachSessionBounded(session);
    }
  }
}

async function captureOnce(page, outputPath, options) {
  const startedAtMs = Date.now();
  const maxTiles = Math.max(2, Math.min(80, Number(options.maxTiles || 50)));
  const delayMs = Math.max(180, Math.min(1500, Number(options.delayMs || 360)));
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'tmui-viewport-tiles-'));
  const tiles = [];
  let nextY = 0;
  let stableBottom = 0;
  let lastHeight = 0;
  try {
    await page.addStyleTag({ content: '*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition:none!important;caret-color:transparent!important}' }).catch(() => {});
    for (let index = 0; index < maxTiles; index += 1) {
      await prepareViewport(page, nextY, index > 0, delayMs);
      let observation = await observeViewport(page, index);
      if (observation.visible_unresolved_images > 0) {
        await page.waitForTimeout(Math.max(500, delayMs));
        await prepareViewport(page, observation.scroll_y_css, index > 0, delayMs);
        observation = await observeViewport(page, index);
      }
      const tilePath = path.join(tempDir, `tile-${String(index).padStart(3, '0')}.png`);
      const screenshot = await captureViewportScreenshot(page, tilePath, {
        timeoutMs: options.screenshotTimeoutMs,
        protocolTimeoutMs: options.protocolTimeoutMs,
        backendState: options.backendState,
      });
      tiles.push({ ...observation, path: tilePath, screenshot });

      const bottom = observation.scroll_y_css + observation.viewport_height_css;
      const atBottom = bottom >= observation.document_height_css - 4;
      stableBottom = atBottom && observation.document_height_css === lastHeight ? stableBottom + 1 : 0;
      lastHeight = observation.document_height_css;
      if (atBottom && (stableBottom >= 1 || index > 0)) break;
      const proposed = Math.min(
        Math.max(0, observation.document_height_css - observation.viewport_height_css),
        observation.scroll_y_css + observation.viewport_height_css,
      );
      if (proposed <= observation.scroll_y_css + 2) break;
      nextY = proposed;
    }
  } catch (error) {
    await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
    throw error;
  } finally {
    await restorePage(page);
  }

  try {
    const manifestPath = path.join(tempDir, 'tiles.json');
    const manifest = {
      document_height_css: Math.max(...tiles.map((tile) => tile.document_height_css)),
      expected_item_count: Number(options.expectedItemCount || 0),
      visible_substantive_images: tiles.reduce((sum, tile) => sum + tile.visible_substantive_images, 0),
      visible_unresolved_images: tiles.reduce((sum, tile) => sum + tile.visible_unresolved_images, 0),
      paint_source_keys: [...new Set(tiles.flatMap((tile) => tile.paint_source_keys || []))],
      tiles,
    };
    await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    const python = options.python || process.env.PYTHON_EXECUTABLE || (process.platform === 'win32' ? 'python' : 'python3');
    const helper = path.join(SCRIPT_DIR, 'stitch-viewport-tiles.py');
    const captureDeadlineAtMs = Number(options.captureDeadlineAtMs || 0);
    const stitchTimeoutMs = captureDeadlineAtMs > 0
      ? Math.max(100, captureDeadlineAtMs - Date.now())
      : normalizeCaptureWallTimeoutMs(options.wallTimeoutMs);
    const result = await runProcessBounded(
      python,
      [helper, '--manifest', manifestPath, '--output', outputPath],
      stitchTimeoutMs,
      options.onStitchProcess,
    );
    let quality = null;
    try { quality = JSON.parse(String(result.stdout || '').trim()); } catch {}
    if (!quality) {
      quality = {
        strategy: 'viewport_tile_stitch_v1', acceptable: false,
        error: String(result.stderr || result.stdout || 'tile stitch helper failed').trim(),
      };
    }
    quality.helper_exit_code = result.status;
    quality.output_created = existsSync(outputPath);
    const screenshotRecords = tiles.map((tile) => tile.screenshot || {});
    quality.capture_duration_ms = Date.now() - startedAtMs;
    quality.screenshot_backend_counts = screenshotRecords.reduce((counts, record) => {
      const method = String(record.method || 'unknown');
      counts[method] = Number(counts[method] || 0) + 1;
      return counts;
    }, {});
    quality.playwright_failure_count = screenshotRecords.filter((record) => record.playwright_error).length;
    quality.playwright_bypass_count = screenshotRecords.filter(
      (record) => record.playwright_bypassed_after_prior_failure === true,
    ).length;
    quality.first_playwright_error = screenshotRecords.find((record) => record.playwright_error)?.playwright_error || null;
    quality.screenshot_backend_switched_at = options.backendState?.switched_at || null;
    quality.screenshot_backend_switch_reason = options.backendState?.reason || null;
    return quality;
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
  }
}

export async function captureViewportTiles(page, outputPath, options = {}) {
  const wallTimeoutMs = normalizeCaptureWallTimeoutMs(options.wallTimeoutMs);
  const captureDeadlineAtMs = Date.now() + wallTimeoutMs;
  let activeStitchProcess = null;
  const captureOptions = {
    ...options,
    captureDeadlineAtMs,
    onStitchProcess: (child) => { activeStitchProcess = child; },
  };
  try {
    return await withOperationTimeout(async () => {
    let quality;
    try {
      quality = await captureOnce(page, outputPath, captureOptions);
    } catch (firstError) {
      if (options.retry === false || !isRecoverableViewportCaptureError(firstError)
          || Date.now() >= captureDeadlineAtMs || page.isClosed?.()) {
        throw firstError;
      }
      const firstAttemptError = {
        name: String(firstError?.name || 'Error'),
        message: String(firstError?.message || firstError || ''),
        code: firstError?.code || null,
        operation: firstError?.operation || null,
        timeout_ms: Number(firstError?.timeout_ms || 0) || null,
        recoverable: true,
      };
      const persistentBackendState = options.backendState && typeof options.backendState === 'object'
        ? options.backendState : null;
      const retryBackendState = {
        ...(persistentBackendState || {}),
        forceCdp: true,
        switched_at: persistentBackendState?.switched_at || null,
        reason: persistentBackendState?.reason || 'same_page_retry_temporary_cdp',
      };
      try {
        const retry = await captureOnce(page, outputPath, {
          ...captureOptions,
          backendState: retryBackendState,
          retry: false,
          delayMs: Math.max(700, Number(options.delayMs || 360) * 2),
        });
        retry.retried_after_capture_error = true;
        retry.same_page_retry = true;
        retry.same_page_retry_forced_cdp = true;
        retry.first_attempt_error = firstAttemptError;
        const retrySucceeded = Boolean(retry.acceptable && retry.output_created);
        retry.backend_state_persisted_after_retry = false;
        if (retrySucceeded && persistentBackendState && persistentBackendState.forceCdp !== true) {
          persistentBackendState.forceCdp = true;
          persistentBackendState.switched_at = new Date().toISOString();
          persistentBackendState.reason = 'same_page_retry_cdp_succeeded';
          retry.backend_state_persisted_after_retry = true;
          retry.screenshot_backend_switched_at = persistentBackendState.switched_at;
          retry.screenshot_backend_switch_reason = persistentBackendState.reason;
        }
        quality = retry;
      } catch (retryError) {
        const error = new Error(
          `viewport tile capture failed after one same-page retry: `
          + `first=${firstAttemptError.message}; retry=${String(retryError?.message || retryError || '')}`,
        );
        error.code = 'VIEWPORT_TILE_CAPTURE_RETRY_EXHAUSTED';
        error.first_attempt_error = firstAttemptError;
        error.retry_error = String(retryError?.message || retryError || '');
        throw error;
      }
      return quality;
    }
    if (!quality.acceptable && options.retry !== false
        && Date.now() < captureDeadlineAtMs && !page.isClosed?.()) {
      const retry = await captureOnce(page, outputPath, {
        ...captureOptions, retry: false,
        delayMs: Math.max(700, Number(options.delayMs || 360) * 2),
      });
      retry.retried_after_visual_failure = true;
      retry.first_attempt = quality;
      quality = retry;
    }
    return quality;
    }, wallTimeoutMs, 'viewport_tile_capture', async () => {
      const cleanup = [];
      if (activeStitchProcess && activeStitchProcess.exitCode == null) {
        cleanup.push(terminateChildBounded(activeStitchProcess));
      }
      if (!page.isClosed?.()) {
        cleanup.push(page.close({ runBeforeUnload: false }).catch(() => {}));
      }
      await Promise.allSettled(cleanup);
    });
  } finally {
    // runProcessBounded deliberately retains the handle when its first bounded
    // termination attempt cannot confirm exit.  Make one final bounded attempt
    // before returning so a failed Windows kill cannot silently orphan the
    // stitch helper after the capture promise rejects early.
    if (activeStitchProcess
        && activeStitchProcess.exitCode == null
        && activeStitchProcess.signalCode == null) {
      await terminateChildBounded(activeStitchProcess).catch(() => {});
    }
  }
}

function parseCli(argv) {
  const output = {};
  for (let index = 0; index < argv.length; index += 1) {
    if (!argv[index].startsWith('--')) continue;
    output[argv[index].slice(2)] = argv[index + 1];
    index += 1;
  }
  return output;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const args = parseCli(process.argv.slice(2));
  if (!args.url || !args.output || !args['browser-executable']) {
    console.error('Usage: viewport-tile-capture.mjs --url <url> --output <png> --browser-executable <path>');
    process.exit(2);
  }
  const require = createRequire(import.meta.url);
  let chromium;
  try { ({ chromium } = require('playwright-core')); } catch { ({ chromium } = require('playwright')); }
  const browser = await chromium.launch({ headless: true, executablePath: args['browser-executable'] });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
    await page.goto(args.url, { waitUntil: 'domcontentloaded' });
    const result = await captureViewportTiles(page, path.resolve(args.output), {
      expectedItemCount: Number(args['expected-item-count'] || 0),
      python: args.python,
    });
    console.log(JSON.stringify(result));
    process.exitCode = result.acceptable ? 0 : 3;
  } finally {
    await browser.close();
  }
}
