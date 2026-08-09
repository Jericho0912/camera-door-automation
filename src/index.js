import { config } from './config.js';
import { db, getMeta, setMeta, nowSec } from './db.js';
import { log } from './log.js';
import { tick } from './pipeline.js';
import { writeGapSession } from './sessions.js';
import { createServer } from './server.js';
import { ping } from './frigate.js';

/**
 * If the heartbeat is stale, we were down. Record it as a visible row so an
 * admin can tell "nobody entered" from "we weren't watching".
 */
function detectGapOnBoot() {
  const last = Number(getMeta('heartbeat', 0));
  if (!last) { setMeta('heartbeat', nowSec()); return; }

  const gap = nowSec() - last;
  if (gap > config.gapThresholdSeconds) writeGapSession(last, nowSec());
  else log.info('clean restart', { downSec: Math.round(gap) });
}

async function main() {
  log.info('starting', { frigate: config.frigate.baseUrl, camera: config.frigate.camera });
  if (!(await ping())) log.warn('Frigate not reachable yet — will keep polling');

  detectGapOnBoot();

  const server = createServer().listen(config.server.port, () =>
    log.info('http listening', { port: config.server.port })
  );

  let running = false;
  const loop = setInterval(async () => {
    if (running) return log.warn('previous tick still running, skipping');
    running = true;
    try { await tick(); }
    catch (err) { log.error('tick failed', { err: err.message, stack: err.stack }); }
    finally { running = false; }
  }, config.poll.intervalMs);

  tick().catch((err) => log.error('initial tick failed', { err: err.message }));

  const shutdown = (sig) => {
    log.info('shutting down', { sig });
    clearInterval(loop);
    server.close(() => { db.close(); process.exit(0); });
    setTimeout(() => process.exit(1), 10_000).unref();
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
}

main().catch((err) => { log.error('fatal', { err: err.message, stack: err.stack }); process.exit(1); });
