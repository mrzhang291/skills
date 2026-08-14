import { Buffer } from 'node:buffer';
import { TextDecoder } from 'node:util';

export function normalizeSearchQuery(value) {
  return String(value || '').replace(/[+＋]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function percentEncodedBytes(value) {
  const bytes = [];
  const source = String(value || '').replace(/\+/g, ' ');
  for (let index = 0; index < source.length;) {
    const match = source.slice(index).match(/^%([0-9a-f]{2})/i);
    if (match) {
      bytes.push(Number.parseInt(match[1], 16));
      index += 3;
      continue;
    }
    const codePoint = source.codePointAt(index);
    const character = String.fromCodePoint(codePoint);
    bytes.push(...Buffer.from(character, 'utf8'));
    index += character.length;
  }
  return Uint8Array.from(bytes);
}

export function decodedRawQueryValues(urlValue, requestedKey) {
  let rawQuery = '';
  try {
    rawQuery = String(urlValue || '').split('?', 2)[1]?.split('#', 1)[0] || '';
  } catch {
    return [];
  }
  const values = [];
  for (const pair of rawQuery.split('&')) {
    if (!pair) continue;
    const separator = pair.indexOf('=');
    const rawKey = separator >= 0 ? pair.slice(0, separator) : pair;
    const rawValue = separator >= 0 ? pair.slice(separator + 1) : '';
    let key = rawKey;
    try { key = decodeURIComponent(rawKey.replace(/\+/g, '%20')); } catch {}
    if (key !== requestedKey) continue;
    const candidates = [];
    try {
      candidates.push({ value: decodeURIComponent(rawValue.replace(/\+/g, '%20')), encoding: 'utf-8' });
    } catch {}
    try {
      candidates.push({
        value: new TextDecoder('gb18030', { fatal: true }).decode(percentEncodedBytes(rawValue)),
        encoding: 'gb18030',
      });
    } catch {}
    for (const candidate of candidates) {
      if (!values.some((item) => item.value === candidate.value)) values.push(candidate);
    }
  }
  return values;
}

export function summarizePlatformEligibility(plannedTasks, platformRuns, eligibilityField) {
  const expected = new Map();
  for (const task of plannedTasks || []) {
    const platform = String(task.platform || task.target_platform || '');
    const queryId = String(task.task_id || task.query_id || '');
    if (!platform || !queryId) continue;
    if (!expected.has(platform)) expected.set(platform, new Set());
    expected.get(platform).add(queryId);
  }
  const eligible = new Map();
  for (const run of platformRuns || []) {
    if (run?.[eligibilityField] !== true) continue;
    const platform = String(run.platform || '');
    const queryId = String(run.query_id || '');
    if (!platform || !queryId) continue;
    if (!eligible.has(platform)) eligible.set(platform, new Set());
    eligible.get(platform).add(queryId);
  }
  const platformsWithAny = [...eligible.keys()].filter((platform) => expected.has(platform));
  const completePlatforms = [...expected.entries()]
    .filter(([platform, queryIds]) => (
      queryIds.size > 0 && [...queryIds].every((queryId) => eligible.get(platform)?.has(queryId))
    ))
    .map(([platform]) => platform);
  return { platforms_with_any: platformsWithAny, complete_platforms: completePlatforms };
}

export function canonicalPlatformSearchUrl(platform, query) {
  const normalized = normalizeSearchQuery(query);
  if (!normalized) throw new Error('search_query_cannot_be_empty');
  if (/%[0-9a-f]{2}/i.test(normalized)) throw new Error('search_query_must_be_raw_text_not_preencoded');
  const encoded = encodeURIComponent(normalized);
  const templates = {
    taobao: `https://s.taobao.com/search?q=${encoded}`,
    jd: `https://search.jd.com/Search?keyword=${encoded}&enc=utf-8`,
    '1688': `https://s.1688.com/selloffer/offer_search.htm?keywords=${encoded}`,
  };
  return templates[platform] || null;
}

export function canonicalTaobaoSearchUrl(query) {
  return canonicalPlatformSearchUrl('taobao', query);
}

export function normalizedSearchRoute(value) {
  try {
    const parsed = new URL(String(value || ''));
    const pathname = parsed.pathname.replace(/\/{2,}/g, '/').replace(/\/$/, '') || '/';
    return `${parsed.hostname.toLowerCase()}${pathname.toLowerCase()}`;
  } catch {
    return '';
  }
}

export async function verifyQueryBinding(page, platformConfig, requestedQuery, expectedSearchUrl) {
  const normalizedRequested = normalizeSearchQuery(requestedQuery);
  const actualUrl = page.url();
  const expectedRoute = normalizedSearchRoute(expectedSearchUrl);
  const actualRoute = normalizedSearchRoute(actualUrl);
  let expectedQueryKey = null;
  let expectedQueryValue = null;
  let actualQueryValue = null;
  let actualQueryEncoding = null;
  try {
    const expected = new URL(expectedSearchUrl);
    for (const [key, value] of expected.searchParams.entries()) {
      if (normalizeSearchQuery(value) === normalizedRequested) {
        expectedQueryKey = key;
        expectedQueryValue = value;
        break;
      }
    }
    if (expectedQueryKey) {
      const candidates = decodedRawQueryValues(actualUrl, expectedQueryKey);
      const matching = candidates.find(
        (item) => normalizeSearchQuery(item.value) === normalizedRequested,
      ) || null;
      actualQueryValue = matching?.value || candidates[0]?.value || null;
      actualQueryEncoding = matching?.encoding || candidates[0]?.encoding || null;
    }
  } catch {}
  const visibleInputs = [];
  for (const selector of platformConfig.searchSelectors || []) {
    const matches = page.locator(selector);
    const count = await matches.count().catch(() => 0);
    for (let index = 0; index < Math.min(count, 3); index += 1) {
      const candidate = matches.nth(index);
      if (!await candidate.isVisible().catch(() => false)) continue;
      const value = await candidate.inputValue().catch(() => '');
      visibleInputs.push({ selector, value });
    }
  }
  const urlQueryMatches = actualQueryValue !== null
    && normalizeSearchQuery(actualQueryValue) === normalizedRequested;
  const matchingInput = visibleInputs.find(
    (item) => normalizeSearchQuery(item.value) === normalizedRequested,
  ) || null;
  const routeMatches = Boolean(expectedRoute && actualRoute === expectedRoute);
  const verifiedBy = [
    ...(urlQueryMatches ? ['url_query'] : []),
    ...(matchingInput ? ['visible_input'] : []),
  ];
  return {
    schema_version: '1.0',
    requested_query: requestedQuery,
    normalized_requested_query: normalizedRequested,
    expected_search_url: expectedSearchUrl || null,
    expected_route: expectedRoute || null,
    actual_url: actualUrl,
    actual_route: actualRoute || null,
    route_matches: routeMatches,
    url_query_key: expectedQueryKey,
    expected_url_query_value: expectedQueryValue,
    actual_url_query_value: actualQueryValue,
    actual_url_query_encoding: actualQueryEncoding,
    url_query_matches: urlQueryMatches,
    visible_input_selector: matchingInput?.selector || null,
    visible_input_value: matchingInput?.value || null,
    visible_input_matches: Boolean(matchingInput),
    verified_by: verifiedBy,
    verified: routeMatches && verifiedBy.length > 0,
  };
}
