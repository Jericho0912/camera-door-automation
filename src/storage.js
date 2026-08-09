import { S3Client, PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { config } from './config.js';

const s3 = new S3Client({
  region: config.s3.region,
  endpoint: config.s3.endpoint,
  forcePathStyle: config.s3.forcePathStyle,
  credentials: {
    accessKeyId: config.s3.accessKeyId,
    secretAccessKey: config.s3.secretAccessKey,
  },
});

export async function upload(key, body, contentType) {
  await s3.send(new PutObjectCommand({
    Bucket: config.s3.bucket,
    Key: key,
    Body: body,
    ContentType: contentType,
  }));
  return { key, bytes: body.length };
}

/** Mint a short-lived URL at click time, not at write time. */
export function presign(key, expiresIn) {
  return getSignedUrl(
    s3,
    new GetObjectCommand({ Bucket: config.s3.bucket, Key: key }),
    { expiresIn }
  );
}

export function keyFor(eventId, kind) {
  const ext = kind === 'clip' ? 'mp4' : 'jpg';
  const day = new Date(Number(String(eventId).split('.')[0]) * 1000)
    .toISOString().slice(0, 10);
  return `${kind}s/${day}/${eventId}.${ext}`;
}
