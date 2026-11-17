import assert from 'node:assert/strict';
import test from 'node:test';
import koffi from 'koffi';
import { createOpenVrHeadPoseReader, getOpenVrLibraryPath } from '../../electron/openvr-head-pose.js';

const poseType = koffi.type('CovasTrackedDevicePose');
const callbackType = koffi.proto(
  process.platform === 'win32' ? '__stdcall' : '__cdecl',
  'CovasTestReadPoses', 'void', ['int32_t', 'float', 'void *', 'uint32_t'],
);

test('OpenVR pose layout matches the public 80-byte ABI', () => {
  assert.equal(koffi.sizeof(poseType), 80);
  assert.equal(koffi.offsetof(poseType, 'valid'), 76);
  assert.equal(koffi.offsetof(poseType, 'connected'), 77);
  assert.throws(() => getOpenVrLibraryPath('darwin'), /requires SteamVR/);
  assert.throws(() => getOpenVrLibraryPath('win32', 'arm64'), /requires SteamVR/);
});

test('reads standing-space HMD poses through the real native function-pointer ABI', t => {
  let valid = true;
  let connected = true;
  let trackingResult = 200;
  let unloaded = 0;
  const callback = koffi.register((origin, prediction, output, count) => {
    assert.equal(origin, 1);
    assert.equal(prediction, 0);
    assert.equal(count, 1);
    koffi.encode(output, poseType, {
      matrix: [0, 0, 1, 1.2, 0, 1, 0, 1.4, -1, 0, 0, 2.3],
      velocity: [0, 0, 0], angularVelocity: [0, 0, 0],
      trackingResult, valid, connected,
    });
  }, koffi.pointer(callbackType));
  t.after(() => koffi.unregister(callback));
  const table = Buffer.alloc(12 * koffi.sizeof('void *'));
  koffi.encode(table, 11 * koffi.sizeof('void *'), 'void *', callback);
  t.mock.method(koffi, 'load', () => ({
    func(signature) {
      assert.equal(signature, 'void *VR_GetGenericInterface(const char *, _Out_ int32_t *)');
      return (version, error) => {
        assert.equal(version, 'FnTable:IVRSystem_023');
        error[0] = 0;
        return table;
      };
    },
    unload: () => unloaded++,
  }));
  const reader = createOpenVrHeadPoseReader();
  const pose = reader.read();
  assert.ok(Math.abs(pose.position.x - 1.2) < 0.00001);
  assert.ok(Math.abs(pose.position.y - 1.4) < 0.00001);
  assert.ok(Math.abs(pose.position.z - 2.3) < 0.00001);
  assert.deepEqual(pose.direction, { x: -1, y: -0, z: -0 });
  valid = false;
  assert.equal(reader.read(), null);
  valid = true;
  connected = false;
  assert.equal(reader.read(), null);
  connected = true;
  trackingResult = 201;
  assert.equal(reader.read(), null);
  reader.close();
  reader.close();
  assert.equal(unloaded, 1);
  assert.throws(() => reader.read(), /already been closed/);
});

test('reports initialization failure and releases its library without initializing or shutting down VR', t => {
  let unloaded = 0;
  t.mock.method(koffi, 'load', () => ({
    func: () => (_version, error) => { error[0] = 109; return null; },
    unload: () => unloaded++,
  }));
  assert.throws(createOpenVrHeadPoseReader, /OpenVR error 109/);
  assert.equal(unloaded, 1);
});

test('loads the packaged OpenVR DLL without starting a VR session', {
  skip: process.platform !== 'win32' || process.arch !== 'x64',
}, () => {
  const library = koffi.load(getOpenVrLibraryPath());
  try {
    const token = library.func('uint32_t VR_GetInitToken()');
    const before = token();
    assert.throws(createOpenVrHeadPoseReader, /headset tracking is unavailable/);
    assert.equal(token(), before);
  } finally {
    library.unload();
  }
});
