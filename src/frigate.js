import { config } from './config.js';

const base = config.frigate.baseUrl.replace(/\/$/, '');

async function get(path, { asBuffer = false } = {}) {
  const res = await fetch(`${base}${path}`, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) {
    const err = new Error(`Frigate ${res.status} on ${path}`);
    err.status = res.status;
    throw err;
  }
  return asBuffer ? Buffer.from(await res.arrayBuffer()) : res.json();
}

/**
 * List events with start_time > after.
 * Note: Frigate's `after`/`before` filter on start_time, not end_time, which is
 * why the poller keeps its cursor on start_time and finalises separately.
 */
export function listEvents({ after, limit = config.poll.pageSize }) {
  const q = new URLSearchParams({
    after: String(after),
    limit: String(limit),
    include_thumbnails: '0',
  });
  if (config.frigate.camera) q.set('cameras', config.frigate.camera);
  if (config.frigate.label) q.set('labels', config.frigate.label);
  return get(`/api/events?${q}`);
}

export const getEvent = (id) => get(`/api/events/${encodeURIComponent(id)}`);

export const downloadClip = (id) =>
  get(`/api/events/${encodeURIComponent(id)}/clip.mp4`, { asBuffer: true });

export const downloadSnapshot = (id) =>
  get(`/api/events/${encodeURIComponent(id)}/snapshot.jpg`, { asBuffer: true });

export async function ping() {
  try {
    await get('/api/version');
    return true;
  } catch {
    return false;
  }
}
