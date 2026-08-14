const QCC_SEARCH_PATHS = new Set([
  '/web_searchBrand',
  '/web/search',
  '/web/search/trademark',
]);

function compact(value) {
  return String(value || '').replace(/\s+/g, '');
}

export function isExactQccBrandUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && parsed.hostname === 'www.qcc.com'
      && !parsed.port && !parsed.username && !parsed.password
      && !parsed.search && !parsed.hash
      && /^\/brandDetail\/[a-f0-9]{32}\.html$/i.test(parsed.pathname);
  } catch { return false; }
}

export function isQccSearchUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.hostname === 'www.qcc.com' && QCC_SEARCH_PATHS.has(parsed.pathname);
  } catch { return false; }
}

export function buildQccRegistrationSearchUrl(registrationNumber) {
  const value = String(registrationNumber || '').trim();
  if (!value) throw new Error('registration number is required for QCC search');
  return `https://www.qcc.com/web_searchBrand?searchKey=${encodeURIComponent(value)}&sbSearchType=2`;
}

export function rankQccBrandLinkRecords(records, registrationNumber, markName, owner, limit = 8) {
  const identity = [registrationNumber, markName, owner].map(compact);
  const unique = new Map();
  for (const [fallbackIndex, raw] of (records || []).entries()) {
    const item = raw && typeof raw === 'object' ? raw : {};
    const href = String(item.href || '');
    if (!isExactQccBrandUrl(href) || unique.has(href)) continue;
    const text = compact(item.contextText);
    const score = identity.reduce((total, value, position) => (
      total + (value && text.includes(value) ? [8, 4, 2][position] : 0)
    ), 0);
    unique.set(href, {
      href,
      score,
      index: Number.isInteger(item.index) ? item.index : fallbackIndex,
    });
  }
  return [...unique.values()]
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, Math.max(0, Number(limit) || 0))
    .map((item) => item.href);
}
