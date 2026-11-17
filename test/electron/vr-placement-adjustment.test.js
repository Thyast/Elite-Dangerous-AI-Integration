import assert from 'node:assert/strict';
import test from 'node:test';
import { createVrPlacementAdjustment, getOverlayPlacement, handleVrPlacementRequest } from '../../electron/vr-placement-adjustment.js';
import { isLookingAtOverlay } from '../../electron/vr-gaze-zoom.js';

function harness(overrides = {}, readHeadPose = () => ({
  position: { x: 3, y: 1.7, z: 5 }, direction: { x: -1, y: 0.4, z: 0 },
})) {
  const options = {
    vrAnchor: 'world', vrContent: 'interface', vrSizeMeters: 0.9,
    vrHorizontalOffset: 0.2, vrVerticalOffset: 0.1, vrDistanceOffset: 0,
    vrTiltDegrees: 0, vrYawDegrees: 0, ...overrides,
  };
  const placement = getOverlayPlacement(
    options.vrAnchor, options.vrSizeMeters, options.vrHorizontalOffset, options.vrVerticalOffset,
    options.vrDistanceOffset, options.vrTiltDegrees, options.vrYawDegrees,
  );
  const original = structuredClone(placement);
  const calls = [];
  let accept = true;
  const adjustment = createVrPlacementAdjustment({
    options, placement, readHeadPose,
    controller: {
      setPlacement(value) { calls.push(['placement', value]); return accept; },
      setSizeMeters(value) { calls.push(['size', value]); return true; },
      setCurvature(value) { calls.push(['curvature', value]); return true; },
    },
    gazeZoom: {
      setPaused(value) { calls.push(['pause', value]); },
      setReductionFactor(value) { calls.push(['factor', value]); },
      setReducedOpacity(value) { calls.push(['opacity', value]); },
      setAppearance(size, curvature) { calls.push(['appearance', size, curvature]); },
    },
  });

  return {
    adjustment, placement, original, calls, options,
    handle: (operation, values = {}) => adjustment.handle({ operation, session_id: 'session', ...values }),
    reject: () => { accept = false; },
    accept: () => { accept = true; },
  };
}

const shipProfile = {
    overlay_vr_horizontal_offset: 0.5, overlay_vr_vertical_offset: -0.2,
    overlay_vr_distance_offset: 0.3, overlay_vr_tilt_degrees: 10, overlay_vr_yaw_degrees: -15,
    overlay_vr_size_meters: 1.2, overlay_vr_curvature: 0.1,
    overlay_vr_gaze_reduction_factor: 1, overlay_vr_gaze_reduced_opacity: 0.25,
  };

  test('ship profiles update placement and appearance without changing the tracking origin or recentering', async () => {
    const h = harness();
    h.adjustment.initializeHeight({ position: { y: 1.8 } });
    const result = await h.handle('apply_profile', { config: shipProfile });
    assert.deepEqual(result, { active: false, config: shipProfile });
    assert.ok(Math.abs(h.placement.position.y - (1.8 - 0.2 - 720 / 1280 / 2)) < 1e-12);
    assert.equal(h.placement.position.x, 0);
    assert.equal(h.placement.position.z, -2.3);
    const settings = h.adjustment.getSettings();
    assert.equal(settings.vrSizeMeters, 1.2);
    assert.equal(settings.vrGazeReductionFactor, 1);
    assert.deepEqual(h.calls.at(-1), ['pause', false]);
    h.handle('begin');
    h.handle('move', { axis: 'vertical', delta: 0.1 });
    await assert.rejects(h.handle('apply_profile', { config: shipProfile }), /Finish or cancel/);
    h.handle('cancel');
    assert.equal(h.adjustment.getSettings().vrVerticalOffset, -0.2);
  });

  test('invalid ship profiles and runtime rejection leave live settings unchanged', async () => {
    const h = harness();
    const original = h.adjustment.getSettings();
    for (const config of [{}, { ...shipProfile, overlay_vr_gaze_reduction_factor: 0.5 }, { ...shipProfile, overlay_vr_vertical_offset: NaN }]) {
      await assert.rejects(h.handle('apply_profile', { config }), /Invalid VR/);
      assert.deepEqual(h.adjustment.getSettings(), original);
    }
    h.reject();
    await assert.rejects(h.handle('apply_profile', { config: shipProfile }), /rejected/);
    assert.deepEqual(h.adjustment.getSettings(), original);
    assert.deepEqual(h.placement, h.original);
  });

  test('live appearance changes survive cancellation of the separate position transaction', () => {
    const h = harness({ vrCurvature: 0 });
    h.handle('begin');
    const result = h.handle('set_appearance', { size: 1.5, curvature: 0.25 });
    assert.deepEqual(result.previous, { size: 0.9, curvature: 0 });
    h.handle('move', { axis: 'vertical', delta: 0.1 });
    h.handle('cancel');
    assert.equal(h.adjustment.getSettings().vrSizeMeters, 1.5);
    assert.equal(h.adjustment.getSettings().vrCurvature, 0.25);
    assert.deepEqual(h.placement, h.original);
    assert.throws(() => h.handle('set_appearance', { size: 0.05, curvature: 0 }), /between/);
  });
test('keeps the existing world/head placement and quaternion contract', () => {
  const world = getOverlayPlacement('world', 0.9);
  assert.deepEqual(world.position, { x: -0.5, y: 1.4 - (720 / 1280) / 2, z: -2 });
  const head = getOverlayPlacement('head', 0.9, 0.3, -0.2, 0.4, 30, 40);
  assert.deepEqual(head.position, { x: 0.3, y: -0.2, z: -1.5 });
  const pitch = 30 * Math.PI / 360;
  const yaw = 40 * Math.PI / 360;
  assert.deepEqual(head.rotation, {
    x: Math.cos(yaw) * Math.sin(pitch), y: Math.sin(yaw) * Math.cos(pitch),
    z: -Math.sin(yaw) * Math.sin(pitch), w: Math.cos(yaw) * Math.cos(pitch),
  });
});

test('previews movements synchronously and suspends zoom without changing saved options', () => {
  for (const vrAnchor of ['head', 'world']) {
    const h = harness({ vrAnchor });
    const begin = h.handle('begin');
    assert.equal(begin.active, true);
    assert.deepEqual(h.calls, [['pause', true]]);
    const moved = h.handle('move', { axis: 'vertical', delta: 0.05 });
    assert.equal(moved.config.overlay_vr_vertical_offset, 0.15);
    assert.ok(Math.abs(h.placement.position.y - h.original.position.y - 0.05) < 1e-12);
    assert.equal(h.options.vrVerticalOffset, 0.1);
    assert.equal(h.calls.filter(([kind]) => kind === 'placement').length, 1);
  }
});

test('initializes only world height from tracking once, preserving saved offsets on restart', () => {
  for (const height of [0, 1.1, 1.8]) {
    for (const offset of [-0.4, 0, 0.35]) {
      const h = harness({ vrVerticalOffset: offset, vrTiltDegrees: 15, vrYawDegrees: -10 });
      const settings = h.adjustment.getSettings();
      const pose = { position: { x: 9, y: height, z: -4 }, direction: { x: 1, y: 0.5, z: 0 } };
      assert.equal(h.adjustment.initializeHeight(pose), true);
      assert.ok(Math.abs(h.placement.position.y - (height + offset - 720 / 1280 / 2)) < 1e-12);
      assert.equal(h.placement.position.x, h.original.position.x);
      assert.equal(h.placement.position.z, h.original.position.z);
      assert.deepEqual(h.placement.rotation, h.original.rotation);
      assert.deepEqual(h.adjustment.getSettings(), settings);
      const initialized = structuredClone(h.placement);
      h.adjustment.initializeHeight({ position: { y: height + 0.5 } });
      assert.deepEqual(h.placement, initialized);
      assert.equal(h.calls.length, 1);
      h.handle('begin');
      h.handle('move', { axis: 'vertical', delta: 0.1 });
      h.handle('commit');
      h.handle('finish');
      const restarted = harness(h.adjustment.getSettings());
      restarted.adjustment.initializeHeight(pose);
      assert.deepEqual(restarted.placement, h.placement);
      restarted.handle('begin');
      restarted.handle('move', { axis: 'vertical', delta: 0.1 });
      restarted.handle('cancel');
      assert.deepEqual(restarted.placement, h.placement);
    }
  }
});

test('height initialization waits for tracking and does not interfere with head anchoring or voice adjustments', () => {
  const h = harness();
  assert.equal(h.adjustment.initializeHeight(null), false);
  assert.deepEqual(h.placement, h.original);
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  const draft = structuredClone(h.placement);
  assert.equal(h.adjustment.initializeHeight({ position: { y: 1.8 } }), false);
  assert.deepEqual(h.placement, draft);
  h.handle('cancel');
  assert.equal(h.adjustment.initializeHeight({ position: { y: 1.8 } }), true);
  assert.ok(Math.abs(h.placement.position.y - (1.8 + 0.1 - 720 / 1280 / 2)) < 1e-12);
  for (const y of [NaN, Infinity, '1.8', undefined]) {
    const invalid = harness();
    assert.throws(() => invalid.adjustment.initializeHeight({ position: { y } }), /valid headset height/);
    assert.deepEqual(invalid.placement, invalid.original);
  }
  const rejected = harness();
  rejected.reject();
  assert.throws(() => rejected.adjustment.initializeHeight({ position: { y: 1.8 } }), /rejected/);
  assert.deepEqual(rejected.placement, rejected.original);
  rejected.accept();
  assert.equal(rejected.adjustment.initializeHeight({ position: { y: 1.8 } }), true);
  const head = harness({ vrAnchor: 'head' });
  assert.equal(head.adjustment.initializeHeight(null), true);
  assert.deepEqual(head.placement, head.original);
  assert.deepEqual(head.calls, []);
});

test('explicit recenter wins over pending height initialization and can still recenter an initialized window', async () => {
  let resolvePose;
  const h = harness({}, () => new Promise(resolve => { resolvePose = resolve; }));
  const pending = h.handle('recenter');
  assert.equal(h.adjustment.initializeHeight({ position: { y: 2 } }), false);
  resolvePose({ position: { x: 3, y: 1.6, z: 4 }, direction: { x: -1, y: 0, z: 0 } });
  await pending;
  const recentered = structuredClone(h.placement);
  assert.equal(h.adjustment.initializeHeight({ position: { y: 2 } }), true);
  assert.deepEqual(h.placement, recentered);
  const initialized = harness();
  initialized.adjustment.initializeHeight({ position: { y: 1.2 } });
  await initialized.handle('recenter');
  assert.ok(Math.abs(initialized.placement.position.y - (1.7 + 0.1 - 720 / 1280 / 2)) < 1e-12);
  assert.ok(Math.abs(initialized.placement.position.x - 1) < 1e-12);
});

test('cancel restores exact translation/rotation and resumes zoom; commit waits for persistence', () => {
  const h = harness();
  h.handle('begin');
  h.handle('move', { axis: 'pitch', delta: 15 });
  h.handle('move', { axis: 'yaw', delta: -10 });
  const prepared = h.handle('commit');
  assert.equal(prepared.active, true);
  assert.equal(prepared.config.overlay_vr_tilt_degrees, 15);
  assert.equal(h.calls.filter(([kind, value]) => kind === 'pause' && !value).length, 0);
  assert.throws(() => h.handle('move', { axis: 'horizontal', delta: 0.1 }), /being saved/);
  const cancelled = h.handle('cancel');
  assert.equal(cancelled.active, false);
  assert.deepEqual(h.placement, h.original);
  assert.deepEqual(h.calls.at(-1), ['pause', false]);
  h.handle('begin');
  h.handle('move', { axis: 'distance', delta: -0.1 });
  assert.throws(() => h.handle('finish'), /Confirm/);
  h.handle('commit');
  const confirmed = h.handle('finish');
  assert.equal(confirmed.active, false);
  assert.equal(confirmed.config.overlay_vr_distance_offset, -0.1);
  assert.equal(h.placement.position.z, -1.9);
});

test('accepts and confirms distance offsets down to -1.5 metres for both anchors', () => {
  for (const vrAnchor of ['head', 'world']) {
    for (const [offset, headZ, worldZ] of [[-1.5, -0.1, -0.5], [-1, -0.1, -1], [1.5, -2.6, -3.5]]) {
      const h = harness({ vrAnchor });
      h.handle('begin');
      const moved = h.handle('move', { axis: 'distance', delta: offset });
      assert.equal(moved.config.overlay_vr_distance_offset, offset);
      assert.ok(Math.abs(h.placement.position.z - (vrAnchor === 'head' ? headZ : worldZ)) < 1e-12);
      h.handle('commit');
      const confirmed = h.handle('finish');
      assert.equal(confirmed.config.overlay_vr_distance_offset, offset);
      assert.equal(h.adjustment.getSettings().vrDistanceOffset, offset);
      const restored = harness(h.adjustment.getSettings());
      assert.deepEqual(restored.placement, h.placement);
    }
  }
});

test('rejects distance offsets outside -1.5 to 1.5 metres without moving the window', () => {
  for (const offset of [-1.500001, 1.500001]) {
    const h = harness();
    h.handle('begin');
    assert.throws(
      () => h.handle('move', { axis: 'distance', delta: offset }),
      /VR distance must remain between -1\.5 and 1\.5 metres/,
    );
    assert.deepEqual(h.placement, h.original);
    assert.equal(h.adjustment.getSettings().vrDistanceOffset, 0);
  }
});

test('rejects unsupported axes, invalid increments, exceeded limits and native failures explicitly', () => {
  const h = harness();
  assert.throws(() => h.handle('move', { axis: 'vertical', delta: 1 }), /No matching/);
  h.handle('begin');
  for (const [axis, delta] of [['roll', 5], ['__proto__', 1], ['vertical', NaN], ['vertical', 0], ['yaw', Infinity]]) {
    assert.throws(() => h.handle('move', { axis, delta }), /Invalid/);
  }
  for (const [axis, delta] of [['horizontal', 2], ['vertical', -2], ['distance', 2], ['pitch', 91], ['yaw', -91]]) {
    assert.throws(() => h.handle('move', { axis, delta }), /between/);
  }
  h.reject();
  assert.throws(() => h.handle('move', { axis: 'vertical', delta: 0.1 }), /rejected/);
  assert.deepEqual(h.placement, h.original);
  assert.throws(() => h.handle('cancel'), /rejected/);
  h.accept();
  assert.equal(h.handle('cancel').active, false);
});

test('sessions isolate stale commands and permit recovery after a missing finish acknowledgement', () => {
  const h = harness();
  h.handle('begin');
  assert.equal(h.handle('begin').active, true);
  assert.throws(() => h.handle('begin', { session_id: 'other' }), /already active/);
  assert.throws(() => h.handle('cancel', { session_id: 'other' }), /matching/);
  h.handle('move', { axis: 'horizontal', delta: 0.05 });
  h.handle('commit');
  h.handle('finish');
  assert.equal(h.handle('finish').active, false);
  assert.equal(h.handle('cancel').active, false);
  assert.deepEqual(h.placement, h.original);
});

test('cleanup restores unconfirmed placement and does not revert a confirmed position', () => {
  const h = harness();
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  h.adjustment.close();
  assert.deepEqual(h.placement, h.original);
  h.handle('begin');
  h.handle('move', { axis: 'horizontal', delta: 0.1 });
  h.handle('commit');
  h.handle('finish');
  const final = structuredClone(h.placement);
  h.adjustment.close();
  assert.deepEqual(h.placement, final);
});

test('unavailable GUI, bad request IDs and HUD-only mode are actionable errors', () => {
  assert.throws(() => handleVrPlacementRequest({ request_id: 'test' }, null), /No active VR overlay/);
  assert.throws(() => handleVrPlacementRequest({}, harness().adjustment), /request ID/);
  assert.throws(() => harness({ vrContent: 'hud' }).handle('begin'), /Full Window/);
  const h = harness();
  assert.throws(() => h.adjustment.handle({ operation: 'begin' }), /session ID/);
  assert.equal(handleVrPlacementRequest({
    operation: 'begin', request_id: 'test', session_id: 'session',
  }, h.adjustment).active, true);
});

test('changes area factors live without an adjustment session and preserves them when cancelling placement', () => {
  const h = harness();
  let result = h.adjustment.handle({ operation: 'set_reduction', factor: 9 });
  assert.deepEqual(result, { active: false, reduction_factor: 9, previous_factor: 5 });
  assert.deepEqual(h.calls.at(-1), ['factor', 9]);
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  result = h.handle('set_reduction', { factor: 12.5 });
  assert.equal(result.previous_factor, 9);
  h.handle('cancel');
  assert.deepEqual(h.placement, h.original);
  result = h.handle('set_reduction', { factor: 5 });
  assert.equal(result.previous_factor, 12.5);
  const restored = h.handle('restore_reduction', { factor: 12.5 });
  assert.equal(restored.reduction_factor, 12.5);
  for (const factor of [0, -1, NaN, Infinity, '5']) {
    assert.throws(() => h.handle('set_reduction', { factor }), /at least 1/);
  }
});

test('changes the reduced opacity live and preserves it when cancelling placement', () => {
  const h = harness();
  let result = h.adjustment.handle({ operation: 'set_opacity', opacity: 0.4 });
  assert.deepEqual(result, { active: false, reduced_opacity: 0.4, previous_opacity: 0.25 });
  assert.deepEqual(h.calls.at(-1), ['opacity', 0.4]);
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  result = h.handle('set_opacity', { opacity: 0.1 });
  assert.equal(result.previous_opacity, 0.4);
  h.handle('cancel');
  assert.deepEqual(h.placement, h.original);
  assert.equal(h.adjustment.getSettings().vrGazeReducedOpacity, 0.1);
  const restored = h.handle('restore_opacity', { opacity: 0.4 });
  assert.deepEqual(restored, { active: false, reduced_opacity: 0.4, previous_opacity: 0.1 });
  for (const opacity of [0, 0.05, 1.5, -1, NaN, Infinity, '0.5', undefined]) {
    assert.throws(() => h.handle('set_opacity', { opacity }), /between 10% and 100%/);
  }
  assert.equal(h.adjustment.getSettings().vrGazeReducedOpacity, 0.4);
});

test('reports actual live settings for UI restart notices, including voice previews and factor changes', () => {
  const h = harness();
  const snapshot = h.adjustment.getSettings();
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.2 });
  h.handle('set_reduction', { factor: 9 });
  assert.equal(h.adjustment.getSettings().vrVerticalOffset, 0.3);
  assert.equal(h.adjustment.getSettings().vrGazeReductionFactor, 9);
  assert.equal(snapshot.vrVerticalOffset, 0.1);
  h.handle('cancel');
  assert.equal(h.adjustment.getSettings().vrVerticalOffset, 0.1);
  assert.equal(h.adjustment.getSettings().vrGazeReductionFactor, 9);
});

test('recenters from the current headset position and heading without inheriting head pitch or saving options', async () => {
  const h = harness();
  const settings = h.adjustment.getSettings();
  assert.deepEqual(await h.handle('recenter'), { active: false, recentered: true });
  assert.ok(Math.abs(h.placement.position.x - 1) < 1e-10);
  assert.ok(Math.abs(h.placement.position.y - (1.7 + 0.1 - 720 / 1280 / 2)) < 1e-10);
  assert.ok(Math.abs(h.placement.position.z - 5.3) < 1e-10);
  assert.ok(Math.abs(h.placement.rotation.y - Math.SQRT1_2) < 1e-10);
  assert.ok(Math.abs(h.placement.rotation.w - Math.SQRT1_2) < 1e-10);
  assert.equal(h.placement.rotation.x, 0);
  assert.equal(Math.abs(h.placement.rotation.z), 0);
  assert.deepEqual(h.adjustment.getSettings(), settings);
  assert.ok(isLookingAtOverlay({
    position: { x: 3, y: 1.7, z: 5 }, direction: { x: -1, y: 0, z: 0 },
  }, h.placement, settings.vrSizeMeters));
  const recentered = structuredClone(h.placement);
  await h.handle('recenter');
  assert.deepEqual(h.placement, recentered);
});

test('preserves configured rotation and adjusts/cancels relative to the new origin', async () => {
  const h = harness({ vrTiltDegrees: 15, vrYawDegrees: -10 });
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  await h.handle('recenter');
  assert.equal(h.adjustment.getSettings().vrTiltDegrees, 15);
  const moved = structuredClone(h.placement);
  h.handle('move', { axis: 'horizontal', delta: 0.1 });
  assert.ok(Math.abs(h.placement.position.z - moved.position.z + 0.1) < 1e-10);
  h.handle('cancel');
  assert.ok(Math.abs(h.placement.position.x - 1) < 1e-10);
  assert.ok(Math.abs(h.placement.position.y - moved.position.y + 0.1) < 1e-10);
  assert.deepEqual(h.placement.rotation, moved.rotation);
  assert.deepEqual(h.calls.at(-1), ['pause', false]);
});

test('head-locked overlays need no tracking layer or placement change to recenter', async () => {
  const h = harness({ vrAnchor: 'head', vrContent: 'hud' }, () => { throw new Error('must not read'); });
  assert.deepEqual(await h.handle('recenter'), { active: false, recentered: true });
  assert.deepEqual(h.placement, h.original);
  assert.deepEqual(h.calls, []);
});

test('tracking failures, invalid headings and native rejection do not change placement or origin', async () => {
  for (const pose of [null, { position: { x: NaN, y: 0, z: 0 }, direction: { x: 0, y: 0, z: -1 } },
    { position: { x: 0, y: 0, z: 0 }, direction: { x: 0, y: 1, z: 0 } }]) {
    const h = harness({}, () => pose);
    await assert.rejects(h.handle('recenter'), /pose/);
    assert.deepEqual(h.placement, h.original);
  }
  const h = harness();
  h.reject();
  await assert.rejects(h.handle('recenter'), /rejected/);
  assert.deepEqual(h.placement, h.original);
  h.accept();
  h.handle('begin');
  h.handle('move', { axis: 'vertical', delta: 0.1 });
  assert.equal(h.placement.position.x, h.original.position.x);
});

test('a closed overlay or overlapping request cannot apply a late recenter pose', async () => {
  let publish;
  const h = harness({}, () => new Promise(resolve => { publish = resolve; }));
  const pending = h.handle('recenter');
  assert.throws(() => h.handle('move', { axis: 'vertical', delta: 0.1 }), /in progress/);
  h.adjustment.close();
  publish({ position: { x: 1, y: 1.4, z: 0 }, direction: { x: 0, y: 0, z: -1 } });
  await assert.rejects(pending, /closed/);
  assert.deepEqual(h.placement, h.original);
});
