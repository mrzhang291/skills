export function roundRobinByKey(items, keyOf, preferredOrder = []) {
  const buckets = new Map();
  const discoveredOrder = [];
  for (const item of items || []) {
    const key = String(keyOf(item) || '');
    if (!buckets.has(key)) {
      buckets.set(key, []);
      discoveredOrder.push(key);
    }
    buckets.get(key).push(item);
  }
  const order = [
    ...preferredOrder.map(String).filter((key) => buckets.has(key)),
    ...discoveredOrder.filter((key) => !preferredOrder.map(String).includes(key)),
  ];
  const scheduled = [];
  let remaining = [...buckets.values()].reduce((total, bucket) => total + bucket.length, 0);
  while (remaining > 0) {
    for (const key of order) {
      const bucket = buckets.get(key);
      if (!bucket?.length) continue;
      scheduled.push(bucket.shift());
      remaining -= 1;
    }
  }
  return scheduled;
}
