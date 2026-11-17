import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { decodeOpenXrPose } from '../../electron/openxr-head-pose.js';
import { createOptionalVrGazeZoom, createVrHeadPoseReader } from '../../electron/vr-head-pose.js';
import { isLookingAtOverlay } from '../../electron/vr-gaze-zoom.js';

function packet() {
  const data = Buffer.alloc(64);
  [0x4358475a, 1, 64, 2].forEach((value, index) => data.writeUInt32LE(value, index * 4));
  data.writeBigUInt64LE(10000n, 16);
  data.writeUInt32LE(1, 24);
  data.writeUInt32LE(3, 28);
  [0, 1.4, 0, 0, 0, 0, 1].forEach((value, index) => data.writeFloatLE(value, 32 + index * 4));
  return data;
}

test('OpenXR pose produces the same forward ray used by OpenVR and the shared zoom geometry', () => {
  const pose = decodeOpenXrPose(packet(), 10000);
  assert.ok(Math.abs(pose.position.y - 1.4) < 0.00001);
  assert.deepEqual(pose.direction, { x: -0, y: -0, z: -1 });
  assert.equal(isLookingAtOverlay(pose, {
    mode: 'world', position: { x: 0, y: 1.4, z: -2 }, rotation: { x: 0, y: 0, z: 0, w: 1 },
  }, 1), true);
  const rotated = packet();
  rotated.writeFloatLE(Math.SQRT1_2, 48);
  rotated.writeFloatLE(Math.SQRT1_2, 56);
  const ray = decodeOpenXrPose(rotated, 10000);
  assert.ok(Math.abs(ray.direction.x + 1) < 0.00001);
  assert.ok(Math.abs(ray.direction.z) < 0.00001);
});

test('rejects stale or in-progress frames and invalid tracking, but accepts the 500 ms boundary', () => {
  assert.notEqual(decodeOpenXrPose(packet(), 10500), null);
  assert.equal(decodeOpenXrPose(packet(), 10501), null);
  const invalid = packet();
  invalid.writeUInt32LE(0, 24);
  assert.equal(decodeOpenXrPose(invalid, 10000), null);
  invalid.writeUInt32LE(3, 12);
  assert.equal(decodeOpenXrPose(invalid, 10000), null);
  assert.equal(decodeOpenXrPose(Buffer.alloc(64), 10000), null);
});

test('reports incompatible wire layouts and malformed poses instead of silently accepting them', () => {
  for (const [offset, value] of [[0, 1], [4, 2], [8, 80], [28, 1]]) {
    const invalid = packet();
    invalid.writeUInt32LE(value, offset);
    assert.throws(() => decodeOpenXrPose(invalid, 10000), /OpenXR gaze/);
  }
  const invalid = packet();
  invalid.writeFloatLE(NaN, 32);
  assert.throws(() => decodeOpenXrPose(invalid, 10000), /non-finite/);
  invalid.writeFloatLE(0, 32);
  invalid.writeFloatLE(0, 56);
  assert.throws(() => decodeOpenXrPose(invalid, 10000), /rotation/);
  assert.throws(() => decodeOpenXrPose(Buffer.alloc(63), 10000), /packet size/);
  assert.throws(() => decodeOpenXrPose(packet(), 8000), /clock/);
});

const executable = fileURLToPath(new URL('../../build/openxr-gaze/layer_test.exe', import.meta.url));
const library = fileURLToPath(new URL('../../build/openxr-gaze/covas_openxr_gaze.dll', import.meta.url));
const nativeAvailable = process.platform === 'win32' && fs.existsSync(executable) && fs.existsSync(library);

async function publisher(t, deferred = false) {
  const child = spawn(executable, [library, deferred ? '--publish-later' : '--publish'], {
    windowsHide: true, stdio: ['pipe', 'pipe', 'inherit'],
  });
  const readline = createInterface({ input: child.stdout });
  const lines = readline[Symbol.asyncIterator]();
  t.after(() => {
    readline.close();
    if (child.exitCode === null && !child.killed) child.kill();
  });
  assert.match((await lines.next()).value, /^READY \d+$/);
  return {
    pid: child.pid,
    async update(command) {
      child.stdin.write(`${command}\n`);
      assert.equal((await lines.next()).value, 'UPDATED');
    },
    async close() {
      const exit = once(child, 'exit');
      child.stdin.end('quit\n');
      assert.equal((await exit)[0], 0);
    },
  };
}

test('reads real cross-process OpenXR layer data and reconnects to a replacement host without SteamVR', {
  skip: !nativeAvailable, timeout: 10000,
}, async t => {
  const first = await publisher(t);
  let host = first.pid;
  const reader = await createVrHeadPoseReader({
    getSelectedBackend: () => 'openxr',
    getRuntimeInfo: () => ({ openxrHostProcessId: host }),
  });
  t.after(() => reader.close());
  assert.equal(reader.read().position.x, 1.25);
  await first.update('rotate');
  assert.ok(Math.abs(reader.read().direction.x + 1) < 0.00001);
  await first.update('invalid');
  assert.equal(reader.read(), null);
  await first.update('valid');
  assert.notEqual(reader.read(), null);
  await delay(600);
  assert.equal(reader.read(), null);
  await first.close();
  assert.equal(reader.read(), null);
  const second = await publisher(t);
  host = second.pid;
  await delay(1100);
  await second.update('valid');
  assert.equal(reader.read().position.x, 1.25);
  reader.close();
  assert.throws(() => reader.read(), /already been closed/);
  await second.close();
});

test('waits for a game started after the OpenXR reader and connects without recreating it', {
  skip: !nativeAvailable, timeout: 10000,
}, async t => {
  let host = 0;
  const reader = await createVrHeadPoseReader({
    getSelectedBackend: () => 'openxr',
    getRuntimeInfo: () => ({ openxrHostProcessId: host }),
  });
  t.after(() => reader.close());
  assert.equal(reader.getState().status, 'waiting');
  assert.equal(reader.read(), null);
  const game = await publisher(t);
  host = game.pid;
  await delay(1100);
  await game.update('valid');
  assert.equal(reader.read().position.x, 1.25);
  assert.deepEqual(reader.getState(), { status: 'active' });
  await game.close();
});

test('adaptive sizing survives a connected game that publishes its first frame after overlay creation', {
  skip: !nativeAvailable, timeout: 10000,
}, async t => {
  const game = await publisher(t, true);
  const widths = [];
  const result = await createOptionalVrGazeZoom({
    controller: {
      getSelectedBackend: () => 'openxr',
      getRuntimeInfo: () => ({ openxrHostProcessId: game.pid }),
      setSizeMeters: width => { widths.push(width); return true; },
    },
    options: { vrGazeZoom: true, vrAnchor: 'world', vrSizeMeters: 1, vrCurvature: 0, vrGazeReductionFactor: 5 },
    placement: { mode: 'world', position: { x: 0, y: 1.4, z: -2 }, rotation: { x: 0, y: 0, z: 0, w: 1 } },
    logger: { error: t.mock.fn(), warn: t.mock.fn(), info() {} },
  });
  t.after(() => result.controller?.close());
  assert.equal(result.state.status, 'waiting');
  assert.notEqual(result.controller, null);
  await delay(150);
  assert.equal(result.controller.getState().status, 'waiting');
  assert.deepEqual(widths, []);
  for (let index = 0; index < 24; index++) await game.update('valid');
  assert.deepEqual(result.controller.getState(), { status: 'active' });
  assert.ok(widths.at(-1) < 1, 'looking away must actually reduce the window after tracking connects');
  await game.update('invalid');
  await delay(100);
  assert.equal(result.controller.getState().status, 'unavailable');
  await game.update('valid');
  await delay(100);
  assert.deepEqual(result.controller.getState(), { status: 'active' });
  result.controller.close();
  await game.close();
});
