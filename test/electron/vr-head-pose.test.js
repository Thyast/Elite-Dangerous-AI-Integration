import assert from 'node:assert/strict';
import test from 'node:test';
import { createOptionalVrGazeZoom, createVrHeadPoseReader } from '../../electron/vr-head-pose.js';

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

test('unsupported backends are rejected by the pose selector', async () => {
  await assert.rejects(createVrHeadPoseReader({ getSelectedBackend: () => 'mock' }), /mock VR backend/);
});

test('OpenXR session modes with a different world-space contract are explicitly unavailable', async () => {
  await assert.rejects(createVrHeadPoseReader({
    getSelectedBackend: () => 'openxr',
    getRuntimeInfo: () => ({ openxrMode: 'overlay-session' }),
  }), /application-integration mode/);
});
