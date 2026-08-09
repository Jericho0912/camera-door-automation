const ts = () => new Date().toISOString();
const emit = (level, msg, meta) =>
  console.log(JSON.stringify({ ts: ts(), level, msg, ...(meta ?? {}) }));

export const log = {
  info: (msg, meta) => emit('info', msg, meta),
  warn: (msg, meta) => emit('warn', msg, meta),
  error: (msg, meta) => emit('error', msg, meta),
};
