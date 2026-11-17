// @covas-labs/electron-vr logs a console.error on every painted frame it cannot submit.
// Before an OpenXR game starts this is the normal waiting state, so it would flood the logs.
const FRAME_ERROR = /frame to VR bridge:\s*$/;
const WAITING = /^Waiting for /;

export function installVrBridgeConsoleFilter(target = console, { now = Date.now, repeatReportMs = 60_000, resetAfterMs = 30_000 } = {}) {
  const originalError = target.error;
  const originalLog = target.log;
  let last = null;

  function flush() {
    if (last?.suppressed && !last.waiting) {
      originalError.call(target, `${last.prefix} ${last.detail} (repeated ${last.suppressed} more times)`);
    }
  }

  target.error = function vrBridgeConsoleFilter(...args) {
    const [prefix, detailValue] = args;
    if (typeof prefix !== 'string' || !FRAME_ERROR.test(prefix)) {
      return originalError.apply(target, args);
    }
    const detail = String(detailValue ?? 'Unknown error');
    const time = now();
    const key = `${prefix}\u0000${detail}`;
    if (last?.key === key && time - last.seenAt < resetAfterMs) {
      last.seenAt = time;
      last.suppressed += 1;
      if (!last.waiting && time - last.reportedAt >= repeatReportMs) {
        flush();
        last.suppressed = 0;
        last.reportedAt = time;
      }
      return undefined;
    }
    flush();
    const waiting = WAITING.test(detail);
    last = { key, prefix: prefix.trimEnd(), detail, waiting, seenAt: time, reportedAt: time, suppressed: 0 };
    if (waiting) {
      return originalLog.call(target, `VR overlay: ${detail}`);
    }
    return originalError.apply(target, args);
  };

  return () => {
    flush();
    target.error = originalError;
  };
}
