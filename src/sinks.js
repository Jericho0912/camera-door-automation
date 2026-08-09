import { Client } from '@notionhq/client';
import { config } from './config.js';
import { log } from './log.js';

const notion = new Client({ auth: config.notion.token });

function titleFor(session, known) {
  if (session.kind === 'gap') {
    const mins = Math.round((session.gap_to - session.gap_from) / 60);
    return `⚠️ Logger offline — ${mins} min gap`;
  }
  if (session.kind === 'unlock_no_camera') return 'Unlock with no one on camera';

  const parts = [];
  if (known.length) parts.push(known.join(', '));
  if (session.count_unknown > 0) {
    parts.push(`${session.count_unknown} unknown`);
    if (!known.length) parts.unshift('no known faces');
  }
  const suffix = session.has_unlock ? '' : ' (no unlock)';
  return `Entry — ${parts.join(', ') || 'no one identified'}${suffix}`;
}

/**
 * Property names must match the Notion database exactly. Change them here if
 * yours differ — this is the only place they appear.
 */
export async function createNotionRow(session, known, clipUrl) {
  const props = {
    Name: { title: [{ text: { content: titleFor(session, known) } }] },
    Date: { date: { start: new Date(session.opened_at * 1000).toISOString() } },
    'People entered': {
      rich_text: [{ text: { content: known.join(', ') || '—' } }],
    },
    Unknown: { number: session.count_unknown },
  };
  if (clipUrl) props['Video recording'] = { url: clipUrl };
  if (session.count_unknown > 0 || session.kind === 'gap') {
    props.Status = { select: { name: 'Flagged' } };
  }

  const page = await notion.pages.create({
    parent: { database_id: config.notion.databaseId },
    properties: props,
  });

  if (session.kind === 'gap') {
    await notion.blocks.children.append({
      block_id: page.id,
      children: [{
        object: 'block',
        type: 'callout',
        callout: {
          rich_text: [{ text: { content:
            'The logger was not running for this window. Any entries during it were not captured. ' +
            'Continuous SD-card footage on the camera covers this period.' } }],
          icon: { emoji: '⚠️' },
        },
      }],
    });
  } else if (!session.has_unlock && session.count_total > 0) {
    await notion.blocks.children.append({
      block_id: page.id,
      children: [{
        object: 'block',
        type: 'callout',
        callout: {
          rich_text: [{ text: { content:
            'No unlock event matched this entry — door held open, an exit, or a tailgate after the window closed.' } }],
          icon: { emoji: '⚠️' },
        },
      }],
    });
  }

  return page.id;
}

/**
 * Alert only when a session contains an unknown face.
 * `text` is what shows on a phone's lock screen — Block Kit content does not.
 */
export async function postSlackAlert(session, known, { snapshotUrl, clipUrl, notionUrl }) {
  if (!config.slack.enabled) return null;

  const when = new Date(session.opened_at * 1000)
    .toLocaleString('en-PH', { timeZone: 'Asia/Manila', dateStyle: 'medium', timeStyle: 'short' });
  const summary =
    `⚠️ Unknown person at the door — ${session.count_unknown} unknown of ${session.count_total} · ${when}`;

  const blocks = [{
    type: 'section',
    text: {
      type: 'mrkdwn',
      text:
        `*⚠️ Unknown person at the door*\n${when}\n` +
        `*${session.count_unknown}* unknown of *${session.count_total}* · ` +
        `recognised: ${known.join(', ') || 'none'}\n` +
        `Unlock: ${session.has_unlock ? 'matched' : '_no unlock event_'}`,
    },
  }];

  // Slack's servers fetch this URL themselves, so it must be publicly
  // reachable — a presigned S3 link works, a Tailscale hostname does not.
  if (snapshotUrl) {
    blocks.push({ type: 'image', image_url: snapshotUrl, alt_text: 'Snapshot at the door' });
  }

  const elements = [];
  if (clipUrl) {
    elements.push({
      type: 'button',
      text: { type: 'plain_text', text: '▶ Watch clip' },
      url: clipUrl,
      style: 'primary',
    });
  }
  if (notionUrl) {
    elements.push({ type: 'button', text: { type: 'plain_text', text: 'Open log' }, url: notionUrl });
  }
  if (elements.length) blocks.push({ type: 'actions', elements });

  const res = await fetch('https://slack.com/api/chat.postMessage', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${config.slack.botToken}`,
      'Content-Type': 'application/json; charset=utf-8',
    },
    body: JSON.stringify({ channel: config.slack.channel, text: summary, blocks }),
    signal: AbortSignal.timeout(15_000),
  });

  const body = await res.json();
  // Slack returns HTTP 200 with ok:false on application errors.
  if (!body.ok) throw new Error(`Slack error: ${body.error}`);
  log.info('slack alert sent', { session: session.id, ts: body.ts });
  return body.ts;
}
