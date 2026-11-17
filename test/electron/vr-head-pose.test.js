import assert from 'node:assert/strict';
import test from 'node:test';
import { createOptionalVrGazeZoom, createVrHeadPoseReader, createVrHeightInitialization } from '../../electron/vr-head-pose.js';
import { createVrPlacementAdjustment, getOverlayPlacement } from '../../electron/vr-placement-adjustment.js';

const options = { vrGazeZoom: true, vrAnchor: 'world', vrSizeMeters: 1, vrCurvature: 0, vrGazeReductionFactor: 5 };
const placement = { mode: 'world', position: { x: 0, y: 1.4, z: -2 }, rotation: { x: 0, y: 0, z: 0, w: 1 } };

function setup(t, overrides = {}) {
  const reader = { read: () => null, close: t.mock.fn() };
  const settings = {
    controller: { setSizeMeters: () => true }, options, placement,
    logger: { error: t.mock.fn(), warn() {}, info() {} },
    createReader: t.mock.fn(async () => reader),
    ...overrides,
  };
  return { reader, settings };
}

test('keeps gaze disabled without loading either native pose reader', async t => {
  const h = setup(t, { options: { ...options, vrGazeZoom: false } });
  const result = await createOptionalVrGazeZoom(h.settings);
  assert.deepEqual(result.state, { status: 'disabled' });
  assert.equal(h.settings.createReader.mock.callCount(), 0);
});

test('full-window factor 1 bypasses tracking; enabling and disabling live creates and releases the reader', async t => {
  const h = setup(t, { options: { ...options, mode: 'vr-full', vrGazeReductionFactor: 1 } });
  const result = await createOptionalVrGazeZoom(h.settings);
  t.after(() => result.controller.close());
  assert.equal(result.state.status, 'disabled');
  assert.equal(h.settings.createReader.mock.callCount(), 0);
  await result.controller.setReductionFactor(5);
  assert.equal(result.controller.getState().status, 'active');
  assert.equal(h.settings.createReader.mock.callCount(), 1);
  await result.controller.setReductionFactor(1);
  assert.equal(result.controller.getState().status, 'disabled');
  assert.equal(h.reader.close.mock.callCount(), 1);
  await assert.rejects(result.controller.setReductionFactor(0.5), /at least 1/);
  await result.controller.setReductionFactor(9);
  assert.equal(h.settings.createReader.mock.callCount(), 2);
});

test('full-window tracking failure remains explicit and a live factor command can retry', async t => {
  const h = setup(t, { options: { ...options, mode: 'vr-full' } });
  let available = false;
  h.settings.createReader = async () => {
    if (!available) throw new Error('Tracking failed');
    return h.reader;
  };
  const result = await createOptionalVrGazeZoom(h.settings);
  t.after(() => result.controller.close());
  assert.equal(result.state.status, 'unavailable');
  await assert.rejects(result.controller.setReductionFactor(5), /Tracking failed/);
  available = true;
  await result.controller.setReductionFactor(5);
  assert.equal(result.controller.getState().status, 'active');
});

test('the same zoom controller accepts either backend and closes tracking on disposal', async t => {
  for (const backend of ['openvr', 'openxr']) {
    const h = setup(t);
    h.settings.controller.getSelectedBackend = () => backend;
    const result = await createOptionalVrGazeZoom(h.settings);
    assert.equal(result.state.status, 'active');
    assert.equal(h.settings.createReader.mock.calls[0].arguments[0], h.settings.controller);
    result.controller.close();
    assert.equal(h.reader.close.mock.callCount(), 1);
  }
});

test('unrecoverable OpenXR reader errors produce an explicit unavailable state, not an overlay creation failure', async t => {
  const h = setup(t, { createReader: async () => { throw new Error('Cannot open OpenXR gaze data (5).'); } });
  const result = await createOptionalVrGazeZoom(h.settings);
  assert.deepEqual(result.state, { status: 'unavailable', message: 'Cannot open OpenXR gaze data (5).' });
  assert.equal(result.controller, null);
  assert.equal(h.settings.logger.error.mock.callCount(), 1);
});

test('preserves a waiting reader and exposes its live state instead of claiming tracking is active', async t => {
  let state = { status: 'waiting', message: 'Waiting for the game.' };
  const h = setup(t);
  h.reader.getState = () => state;
  const result = await createOptionalVrGazeZoom(h.settings);
  t.after(() => result.controller?.close());
  assert.deepEqual(result.state, state);
  assert.notEqual(result.controller, null);
  state = { status: 'active' };
  assert.deepEqual(result.controller.getState(), state);
});

test('head anchoring is explicitly unavailable and failed zoom setup releases its reader', async t => {
  const h = setup(t, { options: { ...options, vrAnchor: 'head' } });
  const result = await createOptionalVrGazeZoom(h.settings);
  assert.equal(result.state.status, 'unavailable');
  assert.match(result.state.message, /World Locked/);
  assert.equal(h.settings.createReader.mock.callCount(), 0);
  const invalid = setup(t, { options: { ...options, vrGazeReductionFactor: 0 } });
  assert.equal((await createOptionalVrGazeZoom(invalid.settings)).state.status, 'unavailable');
  assert.equal(invalid.reader.close.mock.callCount(), 1);
});

test('passes the configured reduced opacity to adaptive sizing and rejects invalid values without failing the overlay', async t => {
  const setOpacity = t.mock.fn(() => true);
  const h = setup(t, { options: { ...options, vrGazeReducedOpacity: 0.3 }, setOpacity });
  const result = await createOptionalVrGazeZoom(h.settings);
  assert.equal(result.state.status, 'active');
  result.controller.setReducedOpacity(0.6);
  result.controller.close();
  assert.equal(setOpacity.mock.callCount(), 0);
  const invalid = setup(t, { options: { ...options, vrGazeReducedOpacity: 0 }, setOpacity });
  const failed = await createOptionalVrGazeZoom(invalid.settings);
  assert.deepEqual(failed.state, { status: 'unavailable', message: 'The reduced VR window opacity must be between 10% and 100%.' });
  assert.equal(invalid.reader.close.mock.callCount(), 1);
});

test('unsupported backends are rejected by the pose selector', async () => {
  await assert.rejects(createVrHeadPoseReader({ getSelectedBackend: () => 'mock' }), /mock VR backend/);
});

test('OpenXR session modes with a different world-space contract are explicitly unavailable', async () => {
  await assert.rejects(createVrHeadPoseReader({
    getSelectedBackend: () => 'openxr',
    getRuntimeInfo: () => ({ openxrMode: 'overlay-session' }),
  }), /application-integration mode/);
});

function heightSetup(t, overrides = {}) {
  let pose = null;
  let tick;
  const options = {
    mode: 'vr-full', vrAnchor: 'world', vrGazeZoom: false, vrContent: 'interface', vrSizeMeters: 0.9,
    vrHorizontalOffset: 0.2, vrVerticalOffset: -0.3, vrDistanceOffset: 0.2,
    vrTiltDegrees: 10, vrYawDegrees: 15, ...overrides,
  };
  const placement = getOverlayPlacement(options.vrAnchor, options.vrSizeMeters, options.vrHorizontalOffset,
    options.vrVerticalOffset, options.vrDistanceOffset, options.vrTiltDegrees, options.vrYawDegrees);
  const original = structuredClone(placement);
  const controller = { setPlacement: t.mock.fn(() => true) };
  const adjustment = createVrPlacementAdjustment({ controller, options, placement });
  const reader = { read: t.mock.fn(() => pose), close: t.mock.fn() };
  const settings = {
    controller, options, adjustment, logger: { info: t.mock.fn(), error: t.mock.fn() },
    createReader: t.mock.fn(async () => reader),
    setIntervalFn: t.mock.fn(callback => { tick = callback; return 42; }),
    clearIntervalFn: t.mock.fn(),
  };
  return { settings, reader, placement, original, setPose: value => { pose = value; }, tick: () => tick() };
}

test('initializes late world tracking with zoom disabled and stops reading after the first valid height', async t => {
  const h = heightSetup(t);
  const result = await createVrHeightInitialization(h.settings);
  assert.equal(result.getState().status, 'waiting');
  assert.deepEqual(h.placement, h.original);
  h.tick();
  assert.equal(result.getState().status, 'waiting');
  h.setPose({ position: { x: 100, y: 1.9, z: -20 }, direction: { x: 1, y: 1, z: 0 } });
  h.tick();
  assert.equal(result.getState().status, 'active');
  assert.ok(Math.abs(h.placement.position.y - (1.9 - 0.3 - 720 / 1280 / 2)) < 1e-12);
  assert.equal(h.placement.position.x, h.original.position.x);
  assert.equal(h.placement.position.z, h.original.position.z);
  assert.deepEqual(h.placement.rotation, h.original.rotation);
  assert.equal(h.settings.controller.setPlacement.mock.callCount(), 1);
  const reads = h.reader.read.mock.callCount();
  h.setPose({ position: { y: 2.2 } });
  h.tick();
  result.close();
  assert.equal(h.reader.read.mock.callCount(), reads);
  assert.equal(h.reader.close.mock.callCount(), 1);
  assert.deepEqual(h.settings.clearIntervalFn.mock.calls[0].arguments, [42]);
  assert.equal(h.settings.logger.error.mock.callCount(), 0);
});

test('does not load height tracking for Head Locked or the HUD mode and releases a waiting world reader on close', async t => {
  for (const overrides of [{ vrAnchor: 'head' }, { mode: 'vr', vrContent: 'hud' }]) {
    const head = heightSetup(t, overrides);
    const result = await createVrHeightInitialization(head.settings);
    assert.equal(result.getState().status, 'disabled');
    assert.equal(head.settings.createReader.mock.callCount(), 0);
    assert.equal(head.settings.setIntervalFn.mock.callCount(), 0);
  }
  const world = heightSetup(t);
  const waiting = await createVrHeightInitialization(world.settings);
  waiting.close();
  world.setPose({ position: { y: 1.9 } });
  world.tick();
  assert.equal(waiting.getState().status, 'disabled');
  assert.deepEqual(world.placement, world.original);
  assert.equal(world.reader.close.mock.callCount(), 1);
});

test('reader and placement failures are reported explicitly and clean up instead of claiming initialization', async t => {
  for (const failure of ['create', 'read', 'height', 'placement', 'timer', 'close']) {
    const h = heightSetup(t);
    if (failure === 'create') h.settings.createReader = async () => { throw new Error('Native tracking unavailable'); };
    if (failure === 'read') h.reader.read = () => { throw new Error('Tracking data invalid'); };
    if (failure === 'height') h.setPose({ position: { y: NaN } });
    if (failure === 'placement') {
      h.setPose({ position: { y: 1.9 } });
      h.settings.controller.setPlacement = () => false;
    }
    if (failure === 'timer') h.settings.setIntervalFn = () => { throw new Error('Timer unavailable'); };
    if (failure === 'close') {
      h.setPose({ position: { y: 1.9 } });
      h.reader.close = () => { throw new Error('Native cleanup failed'); };
    }
    const result = await createVrHeightInitialization(h.settings);
    assert.equal(result.getState().status, 'unavailable', failure);
    assert.ok(result.getState().message.length > 0);
    assert.equal(h.settings.logger.error.mock.callCount(), 1);
    if (failure !== 'close') assert.deepEqual(h.placement, h.original);
    result.close();
    if (!['create', 'close'].includes(failure)) assert.equal(h.reader.close.mock.callCount(), 1);
  }
});
