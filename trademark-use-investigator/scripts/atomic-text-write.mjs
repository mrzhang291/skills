import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';

const TRANSIENT_RENAME_ERROR_CODES = new Set(['EPERM', 'EBUSY', 'EACCES']);
const MAX_RENAME_RETRY_BUDGET_MS = 2_000;
const DEFAULT_INITIAL_RETRY_DELAY_MS = 25;
const DEFAULT_MAX_RETRY_DELAY_MS = 400;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function boundedNumber(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(minimum, parsed));
}

function attachSecondaryError(primaryError, property, secondaryError) {
  if (!primaryError || (typeof primaryError !== 'object' && typeof primaryError !== 'function')) return;
  try {
    Object.defineProperty(primaryError, property, {
      configurable: true,
      enumerable: false,
      value: secondaryError,
    });
  } catch {
    // The original error may be frozen. It must still remain the thrown error.
  }
}

export function isTransientRenameError(error) {
  return TRANSIENT_RENAME_ERROR_CODES.has(String(error?.code || '').toUpperCase());
}

/**
 * Retry attempts start only before one absolute deadline. A single rename
 * system call is not cancellable and may itself return after that deadline.
 */
export async function renameWithTransientRetry(sourcePath, targetPath, options = {}) {
  const fileSystem = options.fileSystem || fs;
  const now = options.now || (() => performance.now());
  const sleep = options.sleep || delay;
  const random = options.random || Math.random;
  const retryBudgetMs = boundedNumber(
    options.retryBudgetMs,
    MAX_RENAME_RETRY_BUDGET_MS,
    0,
    MAX_RENAME_RETRY_BUDGET_MS,
  );
  const initialDelayMs = boundedNumber(
    options.initialDelayMs,
    DEFAULT_INITIAL_RETRY_DELAY_MS,
    1,
    MAX_RENAME_RETRY_BUDGET_MS,
  );
  const maximumDelayMs = boundedNumber(
    options.maximumDelayMs,
    DEFAULT_MAX_RETRY_DELAY_MS,
    initialDelayMs,
    MAX_RENAME_RETRY_BUDGET_MS,
  );
  const deadlineAt = now() + retryBudgetMs;
  let retryIndex = 0;

  while (true) {
    try {
      await fileSystem.rename(sourcePath, targetPath);
      return;
    } catch (error) {
      if (!isTransientRenameError(error)) throw error;

      const remainingMs = deadlineAt - now();
      if (remainingMs <= 0) throw error;

      const exponentialDelayMs = initialDelayMs * (2 ** retryIndex);
      const randomValue = Number(random());
      const normalizedRandom = Number.isFinite(randomValue)
        ? Math.min(1, Math.max(0, randomValue))
        : 0.5;
      const jitterMultiplier = 0.75 + (normalizedRandom * 0.5);
      const jitteredDelayMs = Math.max(
        1,
        Math.round(exponentialDelayMs * jitterMultiplier),
      );
      // Clamp after jitter so a high jitter value cannot exceed either cap.
      const retryDelayMs = Math.min(maximumDelayMs, remainingMs, jitteredDelayMs);

      try {
        await sleep(retryDelayMs);
      } catch (sleepError) {
        attachSecondaryError(error, 'retryWaitError', sleepError);
        throw error;
      }
      // A scheduler may wake us after the requested delay. Never initiate a
      // new rename at or beyond the absolute retry deadline.
      if (now() >= deadlineAt) throw error;
      retryIndex += 1;
    }
  }
}

export async function writeTextAtomic(targetPath, content, options = {}) {
  const fileSystem = options.fileSystem || fs;
  const temporaryPath = options.temporaryPath
    || `${targetPath}.${randomUUID()}.tmp`;
  let primaryError = null;

  try {
    await fileSystem.writeFile(temporaryPath, content, 'utf8');
    await renameWithTransientRetry(temporaryPath, targetPath, {
      fileSystem,
      now: options.now,
      sleep: options.sleep,
      random: options.random,
      retryBudgetMs: options.retryBudgetMs,
      initialDelayMs: options.initialDelayMs,
      maximumDelayMs: options.maximumDelayMs,
    });
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    try {
      await fileSystem.unlink(temporaryPath);
    } catch (cleanupError) {
      if (cleanupError?.code !== 'ENOENT' && primaryError) {
        attachSecondaryError(primaryError, 'cleanupError', cleanupError);
      }
      // A successful rename consumes the temporary path. Cleanup must never
      // replace either that committed success or the original write error.
    }
  }
}
