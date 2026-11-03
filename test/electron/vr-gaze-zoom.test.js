import assert from 'node:assert/strict';
import test from 'node:test';
import { createVrGazeZoom, isLookingAtOverlay } from '../../electron/vr-gaze-zoom.js';

const placement = {
  mode: 'world',
  position: { x: 0, y: 1.4, z: -2 },
  rotation: { x: 0, y: 0, z: 0, w: 1 },
};
const centered = {
  position: { x: 0, y: 1.4, z: 0 },
  direction: { x: 0, y: 0, z: -1 },
};
const away = { ...centered, direction: { x: 1, y: 0, z: 0 } };

test('gaze intersects the front of the actual 16:9 rectangle, not the infinite plane', () => {
  assert.equal(isLookingAtOverlay(centered, placement, 1), true);
  assert.equal(isLookingAtOverlay({ ...centered, direction: { x: 0.26, y: 0, z: -1 } }, placement, 1), false);
  assert.equal(isLookingAtOverlay({ ...centered, direction: { x: 0, y: 0.15, z: -1 } }, placement, 1), false);
  assert.equal(isLookingAtOverlay(away, placement, 1), false);
  assert.equal(isLookingAtOverlay({ ...centered, position: { x: 0, y: 1.4, z: -3 } }, placement, 1), false);
  assert.equal(isLookingAtOverlay({
    position: { x: 0, y: 1.4, z: -3 }, direction: { x: 0, y: 0, z: 1 },
  }, placement, 1), false);
});

test('gaze respects world offsets and combined yaw/pitch, including edge-on views', () => {
  const yaw = 0.5;
  const pitch = -0.3;
  const rotation = {
    x: Math.cos(yaw / 2) * Math.sin(pitch / 2),
    y: Math.sin(yaw / 2) * Math.cos(pitch / 2),
    z: -Math.sin(yaw / 2) * Math.sin(pitch / 2),
    w: Math.cos(yaw / 2) * Math.cos(pitch / 2),
  };
  const shifted = { ...placement, position: { x: 2, y: 0.5, z: -3 }, rotation };
  const ray = { ...centered, direction: { x: 2, y: -0.9, z: -3 } };
  assert.equal(isLookingAtOverlay(ray, shifted, 1), true);
  assert.equal(isLookingAtOverlay(centered, shifted, 1), false);
  const edgeOn = { ...placement, rotation: { x: 0, y: Math.SQRT1_2, z: 0, w: Math.SQRT1_2 } };
  assert.equal(isLookingAtOverlay(centered, edgeOn, 1), false);
});

test('gaze intersects curved overlays and rejects curved backfaces and out-of-bounds rays', () => {
  for (const curvature of [0.05, 0.25, 0.5]) {
    assert.equal(isLookingAtOverlay(centered, placement, 1, curvature), true);
    assert.equal(isLookingAtOverlay(away, placement, 1, curvature), false);
    assert.equal(isLookingAtOverlay({
      position: { x: 0, y: 1.4, z: -3 }, direction: { x: 0, y: 0, z: 1 },
    }, placement, 1, curvature), false);
    const angle = curvature * Math.PI * 0.9;
    const radius = 1 / (curvature * 2 * Math.PI);
    const ray = {
      ...centered, direction: { x: radius * Math.sin(angle), y: 0, z: -2 + radius * (1 - Math.cos(angle)) },
    };
    assert.equal(isLookingAtOverlay(ray, placement, 1, curvature), true);
    assert.equal(isLookingAtOverlay({ ...centered, direction: { x: 1, y: 0, z: -1 } }, placement, 1, curvature), false);
  }
});

function harness(overrides = {}) {
  let time = 0;
  let pose = centered;
  let callback;
  const widths = [];
  const errors = [];
  const warnings = [];
  let closed = 0;
  let cleared = 0;
  const reader = {
    read: () => pose,
    close: () => closed++,
    ...overrides.reader,
  };
  const zoom = createVrGazeZoom({
    controller: { setSizeMeters: width => { widths.push(width); return true; }, ...overrides.controller },
    reader, placement, sizeMeters: 1, curvature: 0, reductionFactor: 5,
    logger: { error: (...args) => errors.push(args), warn: (...args) => warnings.push(args), info() {} },
    now: () => time,
    setIntervalFn: fn => { callback = fn; return { unref() {} }; },
    clearIntervalFn: () => cleared++,
    ...overrides.options,
  });
  return {
    zoom, widths, errors, warnings,
    get closed() { return closed; },
    get cleared() { return cleared; },
    setPose(value) { pose = value; },
    advance(ms) {
      for (let elapsed = 0; elapsed < ms; elapsed += 50) {
        time += 50;
        callback();
      }
    },
  };
}

test('starts at full configured size, samples at 20 Hz and divides area by five after looking away', () => {
  let period;
  const h = harness({ options: { setIntervalFn: (_fn, ms) => { period = ms; return 1; } } });
  assert.equal(period, 50);
  h.zoom.close();
  const active = harness();
  active.advance(1500);
  assert.deepEqual(active.widths, []);
  active.setPose(away);
  active.advance(400);
  assert.deepEqual(active.widths, []);
  active.advance(50);
  assert.equal(active.widths.length, 1);
  assert.ok(active.widths[0] > 1 / Math.sqrt(5) && active.widths[0] < 1);
  active.advance(2000);
  assert.equal(active.widths.at(-1), 1 / Math.sqrt(5));
  assert.ok(Math.abs(active.widths.at(-1) ** 2 - 1 / 5) < 1e-12);
  assert.ok(active.widths.every(width => width >= 1 / Math.sqrt(5) && width <= 1));
  const settled = active.widths.length;
  active.advance(1000);
  assert.equal(active.widths.length, settled);
  active.zoom.close();
  assert.equal(active.widths.at(-1), 1);
  active.advance(1000);
  assert.equal(active.widths.length, settled + 1);
});

test('ignores brief looks away and restores full size with a 200 ms entry dwell and hysteresis', () => {
  const h = harness();
  h.setPose(away);
  h.advance(300);
  h.setPose(centered);
  h.advance(200);
  assert.deepEqual(h.widths, []);
  h.setPose(away);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  h.setPose({ ...centered, direction: { x: 0.2, y: 0, z: -1 } });
  h.advance(1000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  h.setPose(centered);
  const count = h.widths.length;
  h.advance(200);
  assert.equal(h.widths.length, count);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1);
  h.setPose({ ...centered, direction: { x: 0.25, y: 0, z: -1 } });
  h.advance(1000);
  assert.equal(h.widths.at(-1), 1);
  h.zoom.close();
});

test('restores 100% size on lost tracking, logs once, and can recover', () => {
  const h = harness();
  assert.deepEqual(h.zoom.getState(), { status: 'active' });
  h.setPose(away);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  h.setPose(null);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1);
  assert.equal(h.warnings.length, 1);
  assert.deepEqual(h.zoom.getState(), { status: 'unavailable', message: 'Headset tracking is unavailable.' });
  h.setPose(away);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  assert.deepEqual(h.zoom.getState(), { status: 'active' });
  h.zoom.close();
  assert.deepEqual(h.zoom.getState(), { status: 'disabled' });
});

test('reports a tracking error, restores size and closes the reader exactly once', () => {
  let fail = false;
  const h = harness({ reader: { read() { if (fail) throw new Error('Tracking failed'); return away; } } });
  h.advance(1500);
  fail = true;
  h.advance(100);
  assert.equal(h.errors.length, 1);
  assert.equal(h.widths.at(-1), 1);
  assert.equal(h.closed, 1);
  assert.equal(h.cleared, 1);
  assert.deepEqual(h.zoom.getState(), { status: 'unavailable', message: 'Tracking failed' });
  h.zoom.close();
  assert.equal(h.closed, 1);
});

test('reports a rejected native size update instead of silently succeeding', () => {
  const h = harness({ controller: { setSizeMeters: () => false } });
  h.setPose(away);
  h.advance(500);
  assert.equal(h.errors.length, 1);
  assert.equal(h.closed, 1);
});

test('area factors support one, five, fractional and larger values, and reject invalid factors', () => {
  for (const reductionFactor of [1, 5, 2.5, 100]) {
    const h = harness({ options: { reductionFactor } });
    h.setPose(away);
    h.advance(2000);
    if (reductionFactor !== 1) assert.equal(h.widths.at(-1), 1 / Math.sqrt(reductionFactor));
    h.zoom.close();
  }
  for (const reductionFactor of [0, 0.99, -5, NaN, Infinity]) {
    assert.throws(() => harness({ options: { reductionFactor } }), /at least 1/);
  }
});

test('pausing restores base size immediately and resumes with a fresh dwell at the updated placement', () => {
  const livePlacement = structuredClone(placement);
  const h = harness({ options: { placement: livePlacement } });
  h.setPose(away);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  h.zoom.setPaused(true);
  assert.equal(h.widths.at(-1), 1);
  const count = h.widths.length;
  h.advance(2000);
  assert.equal(h.widths.length, count);
  assert.equal(h.closed, 0);
  h.setPose(centered);
  livePlacement.position.x = 4;
  h.zoom.setPaused(false);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / Math.sqrt(5));
  livePlacement.position.x = 0;
  const reducedCount = h.widths.length;
  h.advance(200);
  assert.equal(h.widths.length, reducedCount);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1);
  h.zoom.setPaused(true);
  h.zoom.close();
  assert.equal(h.closed, 1);
  assert.equal(h.cleared, 1);
});

test('changes the area factor live, never enlarges beyond configured size, and remains full-size while paused', () => {
  const h = harness();
  h.setPose(away);
  h.advance(2000);
  h.zoom.setReductionFactor(9);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / 3);
  h.zoom.setPaused(true);
  h.zoom.setReductionFactor(25);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1);
  h.zoom.setPaused(false);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1 / 5);
  h.setPose(centered);
  h.advance(2000);
  assert.equal(h.widths.at(-1), 1);
  assert.ok(h.widths.every(width => width <= 1));
  for (const value of [0, -1, NaN, Infinity]) {
    assert.throws(() => h.zoom.setReductionFactor(value), /at least 1/);
  }
  h.zoom.close();
});
