#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { randomUUID } from 'node:crypto';

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function readOwner(lockPath) {
  try {
    return JSON.parse(await fs.readFile(lockPath, 'utf8'));
  } catch {
    return null;
  }
}

function ownerProcessIsAlive(owner) {
  const pid = Number(owner?.pid || 0);
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== 'ESRCH';
  }
}

async function removeIfStale(lockPath, staleMs) {
  try {
    const stat = await fs.stat(lockPath);
    const ageMs = Date.now() - stat.mtimeMs;
    const owner = await readOwner(lockPath);
    if (owner && ownerProcessIsAlive(owner)) return false;
    const recoveryAgeMs = owner ? 3_000 : staleMs;
    if (ageMs <= recoveryAgeMs) return false;
    const currentStat = await fs.stat(lockPath);
    const currentOwner = await readOwner(lockPath);
    if (owner?.token) {
      if (currentOwner?.token !== owner.token) return false;
    } else if (currentStat.mtimeMs !== stat.mtimeMs) {
      return false;
    }
    await fs.rm(lockPath, { force: true });
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return true;
    return false;
  }
}

export async function acquireArtifactLock(lockPath, options = {}) {
  if (!lockPath) {
    return { waited_ms: 0, release: async () => {} };
  }
  const timeoutMs = Math.max(1_000, Number(options.timeoutMs || 180_000));
  const staleMs = Math.max(timeoutMs, Number(options.staleMs || 300_000));
  const pollMs = Math.max(25, Number(options.pollMs || 150));
  const absolute = path.resolve(lockPath);
  const token = randomUUID();
  const started = Date.now();
  await fs.mkdir(path.dirname(absolute), { recursive: true });

  while (true) {
    try {
      const handle = await fs.open(absolute, 'wx');
      await handle.writeFile(`${JSON.stringify({ token, pid: process.pid, acquired_at: new Date().toISOString() })}\n`);
      await handle.close();
      const heartbeat = setInterval(() => {
        const now = new Date();
        fs.utimes(absolute, now, now).catch(() => {});
      }, Math.min(5_000, Math.max(1_000, Math.floor(staleMs / 4))));
      heartbeat.unref?.();
      let released = false;
      return {
        waited_ms: Date.now() - started,
        async release() {
          if (released) return;
          released = true;
          clearInterval(heartbeat);
          const owner = await readOwner(absolute);
          if (owner?.token === token) await fs.rm(absolute, { force: true });
        },
      };
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      await removeIfStale(absolute, staleMs);
      if (Date.now() - started >= timeoutMs) {
        throw new Error(`artifact_capture_lock_timeout:${absolute}`);
      }
      await delay(Math.min(pollMs, Math.max(1, timeoutMs - (Date.now() - started))));
    }
  }
}

export async function withArtifactLock(lockPath, callback, options = {}) {
  const lease = await acquireArtifactLock(lockPath, options);
  try {
    return await callback(lease);
  } finally {
    await lease.release();
  }
}
