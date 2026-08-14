/** Pure helpers for the per-platform sales risk circuit breaker. */

function timeMs(value) {
  const parsed = Date.parse(String(value || ''));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function randomIntegerInclusive(minimum, maximum, random = Math.random) {
  const unit = Math.max(0, Math.min(0.999999999999, Number(random())));
  return Math.floor(minimum + unit * (maximum - minimum + 1));
}

export function validateRiskCircuitBreakerPolicy(policy) {
  if (!policy || typeof policy !== 'object') {
    throw new Error('runtime-policy.json must define sales_risk_circuit_breaker');
  }
  const riskWindowMinutes = Number(policy.risk_window_minutes);
  const successDecayStrikes = Number(policy.success_decay_strikes);
  const historyLimit = Number(policy.max_history_entries);
  const toleranceSeconds = Number(policy.legacy_match_tolerance_seconds);
  if (!Number.isFinite(riskWindowMinutes) || riskWindowMinutes <= 0) {
    throw new Error('sales_risk_circuit_breaker.risk_window_minutes must be positive');
  }
  if (!Number.isInteger(successDecayStrikes) || successDecayStrikes <= 0) {
    throw new Error('sales_risk_circuit_breaker.success_decay_strikes must be a positive integer');
  }
  if (!Number.isInteger(historyLimit) || historyLimit <= 0) {
    throw new Error('sales_risk_circuit_breaker.max_history_entries must be a positive integer');
  }
  if (!Number.isFinite(toleranceSeconds) || toleranceSeconds < 0) {
    throw new Error('sales_risk_circuit_breaker.legacy_match_tolerance_seconds must be non-negative');
  }
  if (!Array.isArray(policy.tiers) || !policy.tiers.length) {
    throw new Error('sales_risk_circuit_breaker.tiers must be a non-empty array');
  }
  let priorStrike = 0;
  for (const tier of policy.tiers) {
    const strike = Number(tier?.min_strike_count);
    const minimum = Number(tier?.min_minutes);
    const maximum = Number(tier?.max_minutes);
    if (!Number.isInteger(strike) || strike <= priorStrike
        || !Number.isInteger(minimum) || minimum < 0
        || !Number.isInteger(maximum) || maximum < minimum) {
      throw new Error('sales_risk_circuit_breaker contains an invalid or unordered tier');
    }
    priorStrike = strike;
  }
  if (Number(policy.tiers[0].min_strike_count) !== 1) {
    throw new Error('sales_risk_circuit_breaker first tier must start at strike 1');
  }
  return policy;
}

export function riskTierForStrike(policy, strikeCount) {
  validateRiskCircuitBreakerPolicy(policy);
  const count = Math.max(1, Number(strikeCount) || 1);
  return [...policy.tiers]
    .reverse()
    .find((tier) => count >= Number(tier.min_strike_count));
}

function appendHistory(state, event, policy) {
  const history = Array.isArray(state.strike_history) ? [...state.strike_history] : [];
  history.push(event);
  return history.slice(-Number(policy.max_history_entries));
}

export function applyRiskStrike(platformState, policy, event, options = {}) {
  validateRiskCircuitBreakerPolicy(policy);
  const nowMs = Number(options.now_ms ?? Date.now());
  const random = options.random || Math.random;
  const previous = { ...(platformState || {}) };
  const previousCount = Math.max(0, Number(previous.strike_count || 0));
  const lastRiskMs = timeMs(previous.last_risk_triggered_at);
  const insideWindow = previousCount > 0
    && Number.isFinite(lastRiskMs)
    && nowMs - lastRiskMs <= Number(policy.risk_window_minutes) * 60_000;
  const strikeCount = insideWindow ? previousCount + 1 : 1;
  const tier = riskTierForStrike(policy, strikeCount);
  const appliedMinutes = randomIntegerInclusive(
    Number(tier.min_minutes), Number(tier.max_minutes), random,
  );
  const triggeredAt = new Date(nowMs).toISOString();
  const cooldownUntil = new Date(nowMs + appliedMinutes * 60_000).toISOString();
  const historyEvent = {
    event: 'risk_strike',
    platform: event.platform || null,
    query_id: event.query_id || null,
    trigger_state: event.trigger_state || null,
    triggered_at_utc: triggeredAt,
    strike_count: strikeCount,
    cooldown_tier_min_strike_count: Number(tier.min_strike_count),
    cooldown_minutes_applied: appliedMinutes,
    cooldown_until: cooldownUntil,
  };
  return {
    ...previous,
    strike_count: strikeCount,
    risk_window_started_at: insideWindow
      ? (previous.risk_window_started_at || previous.last_risk_triggered_at)
      : triggeredAt,
    last_risk_triggered_at: triggeredAt,
    cooldown_triggered_at: triggeredAt,
    cooldown_minutes_applied: appliedMinutes,
    cooldown_tier_min_strike_count: Number(tier.min_strike_count),
    cooldown_until: cooldownUntil,
    probe_required: true,
    probe_attempted_at: null,
    probe_attempted_for_cooldown_until: null,
    strike_history: appendHistory(previous, historyEvent, policy),
  };
}

export function riskGate(platformState, nowMs = Date.now()) {
  const state = platformState || {};
  const cooldownUntilMs = timeMs(state.cooldown_until);
  if (Number.isFinite(cooldownUntilMs) && cooldownUntilMs > nowMs) {
    return {
      allow_request: false,
      reason: 'cooldown_active',
      cooldown_until: state.cooldown_until,
      risk_probe: false,
    };
  }
  if (state.probe_required === true && Number(state.strike_count || 0) > 0) {
    const probeToken = String(state.cooldown_until || state.last_risk_triggered_at || 'risk-probe');
    if (state.probe_attempted_for_cooldown_until === probeToken) {
      return {
        allow_request: false,
        reason: 'probe_already_attempted',
        cooldown_until: state.cooldown_until || null,
        risk_probe: false,
        probe_token: probeToken,
      };
    }
    return {
      allow_request: true,
      reason: 'expired_cooldown_probe',
      cooldown_until: state.cooldown_until || null,
      risk_probe: true,
      probe_token: probeToken,
    };
  }
  return { allow_request: true, reason: 'normal', risk_probe: false };
}

export function markRiskProbeAttempt(platformState, gate, queryId, nowMs = Date.now()) {
  if (!gate?.risk_probe || !gate.probe_token) return { ...(platformState || {}) };
  return {
    ...(platformState || {}),
    probe_attempted_at: new Date(nowMs).toISOString(),
    probe_attempted_for_cooldown_until: gate.probe_token,
    probe_query_id: queryId || null,
  };
}

export function resolvePacketEligibleSuccess(platformState, policy, event, nowMs = Date.now()) {
  validateRiskCircuitBreakerPolicy(policy);
  const previous = { ...(platformState || {}) };
  const before = Math.max(0, Number(previous.strike_count || 0));
  if (!before) return previous;
  const after = Math.max(0, before - Number(policy.success_decay_strikes));
  const resolvedAt = new Date(nowMs).toISOString();
  return {
    ...previous,
    strike_count: after,
    risk_window_started_at: after ? previous.risk_window_started_at : null,
    cooldown_until: null,
    cooldown_triggered_at: null,
    cooldown_minutes_applied: null,
    cooldown_tier_min_strike_count: null,
    probe_required: false,
    probe_attempted_at: null,
    probe_attempted_for_cooldown_until: null,
    probe_query_id: null,
    last_success_resolution: {
      strategy: 'decrement_strike_on_packet_eligible_normal_or_zero',
      resolved_at_utc: resolvedAt,
      query_id: event.query_id || null,
      page_state: event.page_state || null,
      strike_count_before: before,
      strike_count_after: after,
    },
    strike_history: appendHistory(previous, {
      event: 'packet_eligible_success',
      query_id: event.query_id || null,
      page_state: event.page_state || null,
      resolved_at_utc: resolvedAt,
      strike_count_before: before,
      strike_count_after: after,
    }, policy),
  };
}

export function rearmInconclusiveProbe(platformState, policy, event, options = {}) {
  validateRiskCircuitBreakerPolicy(policy);
  const previous = { ...(platformState || {}) };
  const nowMs = Number(options.now_ms ?? Date.now());
  const random = options.random || Math.random;
  const strikeCount = Math.max(1, Number(previous.strike_count || 1));
  const tier = riskTierForStrike(policy, strikeCount);
  const appliedMinutes = randomIntegerInclusive(
    Number(tier.min_minutes), Number(tier.max_minutes), random,
  );
  const triggeredAt = new Date(nowMs).toISOString();
  const cooldownUntil = new Date(nowMs + appliedMinutes * 60_000).toISOString();
  return {
    ...previous,
    cooldown_triggered_at: triggeredAt,
    cooldown_minutes_applied: appliedMinutes,
    cooldown_tier_min_strike_count: Number(tier.min_strike_count),
    cooldown_until: cooldownUntil,
    probe_required: true,
    probe_attempted_at: null,
    probe_attempted_for_cooldown_until: null,
    probe_query_id: null,
    last_probe_resolution: {
      strategy: 'same_tier_rearm_after_inconclusive_probe',
      query_id: event.query_id || null,
      page_state: event.page_state || null,
      rearmed_at_utc: triggeredAt,
      cooldown_minutes_applied: appliedMinutes,
      cooldown_until: cooldownUntil,
    },
    strike_history: appendHistory(previous, {
      event: 'inconclusive_probe_rearmed',
      query_id: event.query_id || null,
      page_state: event.page_state || null,
      rearmed_at_utc: triggeredAt,
      strike_count: strikeCount,
      cooldown_minutes_applied: appliedMinutes,
      cooldown_until: cooldownUntil,
    }, policy),
  };
}

export function migrateLegacyFirstStrike(platformState, platformPolicy, policy, options = {}) {
  validateRiskCircuitBreakerPolicy(policy);
  const previous = { ...(platformState || {}) };
  const nowMs = Number(options.now_ms ?? Date.now());
  const random = options.random || Math.random;
  const legacyMinutes = Number(platformPolicy?.legacy_fixed_cooldown_minutes);
  const triggeredMs = timeMs(previous.triggered_at_utc);
  const cooldownMs = timeMs(previous.cooldown_until);
  const durationMinutes = (cooldownMs - triggeredMs) / 60_000;
  const hasPriorEvidence = Number(previous.strike_count || 0) > 0
    || Array.isArray(previous.strike_history)
    || Boolean(previous.last_risk_triggered_at)
    || Boolean(previous.risk_window_started_at)
    || previous.cooldown_minutes_applied != null
    || previous.cooldown_tier_min_strike_count != null;
  const toleranceMinutes = Number(policy.legacy_match_tolerance_seconds) / 60;
  const legacyMatch = Number.isFinite(legacyMinutes) && legacyMinutes > 0
    && Number.isFinite(triggeredMs) && Number.isFinite(cooldownMs)
    && Math.abs(durationMinutes - legacyMinutes) <= toleranceMinutes;
  if (hasPriorEvidence || !legacyMatch || !previous.trigger_state) {
    return { state: previous, migrated: false };
  }
  const tier = riskTierForStrike(policy, 1);
  const appliedMinutes = randomIntegerInclusive(
    Number(tier.min_minutes), Number(tier.max_minutes), random,
  );
  const clampedUntilMs = Math.min(cooldownMs, triggeredMs + appliedMinutes * 60_000);
  const clampedUntil = new Date(clampedUntilMs).toISOString();
  const migratedAt = new Date(nowMs).toISOString();
  const state = {
    ...previous,
    strike_count: 1,
    risk_window_started_at: new Date(triggeredMs).toISOString(),
    last_risk_triggered_at: new Date(triggeredMs).toISOString(),
    cooldown_triggered_at: new Date(triggeredMs).toISOString(),
    cooldown_minutes_applied: appliedMinutes,
    cooldown_tier_min_strike_count: Number(tier.min_strike_count),
    cooldown_until: clampedUntil,
    probe_required: true,
    probe_attempted_at: null,
    probe_attempted_for_cooldown_until: null,
    legacy_cooldown_migration: {
      migration: 'fixed_first_strike_clamped_to_ladder',
      migrated_at_utc: migratedAt,
      original_cooldown_until: previous.cooldown_until,
      legacy_fixed_cooldown_minutes: legacyMinutes,
      cooldown_minutes_applied: appliedMinutes,
      clamped_cooldown_until: clampedUntil,
    },
  };
  state.strike_history = appendHistory(state, {
    event: 'legacy_first_strike_migrated',
    migrated_at_utc: migratedAt,
    trigger_state: previous.trigger_state,
    query_id: previous.trigger_query_id || null,
    strike_count: 1,
    cooldown_minutes_applied: appliedMinutes,
    cooldown_until: clampedUntil,
  }, policy);
  return { state, migrated: true };
}
