function boundedRandomInteger(minimum, maximum, random) {
  const lower = Number(minimum);
  const upper = Number(maximum);
  return Math.floor(lower + random() * (upper - lower + 1));
}

function parsedTimestamp(value) {
  const parsed = Date.parse(String(value || ''));
  return Number.isFinite(parsed) ? parsed : null;
}

export function computeQueryPacing(policy, platformState, options = {}) {
  const nowMs = Number.isFinite(options.nowMs) ? options.nowMs : Date.now();
  const random = typeof options.random === 'function' ? options.random : Math.random;
  const requestsSinceBreak = Math.max(0, Number(platformState?.requests_since_break || 0));
  const batchBreak = Boolean(policy?.batch_size && requestsSinceBreak >= policy.batch_size);
  const preferredAnchorValue = policy?.delay_from_task_finish
    ? platformState?.last_task_finished_at
    : platformState?.last_request_at;
  const fallbackAnchorValue = preferredAnchorValue || platformState?.last_request_at;
  const anchorMs = parsedTimestamp(fallbackAnchorValue);
  const initialSettle = anchorMs === null && Number(policy?.initial_settle_max_ms || 0) > 0;
  const intervalTargetMs = initialSettle
    ? boundedRandomInteger(policy.initial_settle_min_ms, policy.initial_settle_max_ms, random)
    : batchBreak
      ? boundedRandomInteger(policy.batch_pause_min_ms, policy.batch_pause_max_ms, random)
      : boundedRandomInteger(policy.min_delay_ms, policy.max_delay_ms, random);
  const elapsedMs = anchorMs === null ? (initialSettle ? 0 : intervalTargetMs) : Math.max(0, nowMs - anchorMs);
  return {
    batch_break: batchBreak,
    wait_ms: Math.max(0, intervalTargetMs - elapsedMs),
    interval_target_ms: intervalTargetMs,
    anchor_at: anchorMs === null ? null : new Date(anchorMs).toISOString(),
    anchor_kind: initialSettle
      ? 'initial_settle'
      : policy?.delay_from_task_finish && platformState?.last_task_finished_at
        ? 'last_task_finished_at'
        : 'last_request_at',
    next_requests_since_break: batchBreak ? 1 : requestsSinceBreak + 1,
  };
}
