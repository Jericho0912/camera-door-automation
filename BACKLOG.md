# Backlog

Items worth doing that are not yet scheduled. Each entry describes the
problem, why it matters, and any known constraints.

---

## 1. Single-clip viewer instead of multi-segment HTML

**Problem.** The clip viewer page (`render_player`) creates one `<video>` tag
per recording segment. Frigate stores recordings in short time-based segments
(typically 10 s), so an event spanning 30 s plus pre/post-roll padding overlaps
3-5 separate `.mp4` files. The viewer shows them stacked vertically, which is
awkward - a single continuous playback would be far better.

**Why it is hard.** The multiple files are Frigate's fundamental storage model;
there is no single-clip `.mp4` on disk. Getting one requires either:

- **Server-side concatenation** - `ffmpeg -f concat` on the Mac before upload.
  Pros: the viewer becomes a single `<video>`. Cons: CPU cost on every event,
  doubles the upload size (original segments are still uploaded for archival),
  and the reconciler has no `ffmpeg` dependency today.
- **Client-side chaining** - JavaScript in the viewer page that plays segments
  sequentially, hiding the boundary. Pros: no transcoding, segments are already
  uploaded. Cons: the page needs JS (currently it is a static HTML page served
  straight from S3 with no CSP control), and seek across segment boundaries is
  non-trivial.

**Recorded:** 2026-09-01.

---

## 2. Event snapshot images in Slack summary

**Problem.** Frigate captures and saves a JPEG snapshot of detected persons
(`has_snapshot` in the `event` table). Currently, the Slack summary is text-only
and links to Notion pages. Having a visual snapshot preview of unknown visitors
directly in Slack would allow immediate recognition without clicking into Notion.

**Technical considerations & constraints:**

- **Slack Incoming Webhook limitations:** Standard incoming webhooks
  (`hooks.slack.com`) do not support direct multipart/binary file uploads.
  They only support JSON Block Kit payloads referencing an image URL (`image_url`),
  which must be publicly accessible (e.g. an S3 presigned URL).
- **Bot Token alternative:** Directly uploading binary images to Slack requires the
  Slack Web API (`files.uploadV2`) and a Slack Bot User OAuth Token (`xoxb-...`)
  with `files:write` scope, replacing or complementing the simple webhook model.
- **S3 Snapshot delivery:** If using `image_url` with webhooks, snapshot JPEGs
  must be uploaded to S3 and presigned (similar to video segment clips).
- **Privacy:** Storing visitor face photos on Slack servers permanently retains
  biometric images externally. Opt-in gating (`SLACK_INCLUDE_SNAPSHOTS=false` by
  default) must be preserved.

**Recorded:** 2026-09-01.
