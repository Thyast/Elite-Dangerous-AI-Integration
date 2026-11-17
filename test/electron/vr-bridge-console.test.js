import test from 'node:test';
import assert from 'node:assert/strict';
import { installVrBridgeConsoleFilter } from '../../electron/vr-bridge-console.js';

const PREFIX = 'Failed to submit Windows frame to VR bridge:';
const WAITING = 'Waiting for a D3D11 or D3D12 OpenXR application to connect to the API-layer companion.';

function setup() {
  const calls = [];
  const target = {
    error: (...args) => calls.push(['error', ...args]),
    log: (...args) => calls.push(['log', ...args]),
  };
  let time = 0;
  const uninstall = installVrBridgeConsoleFilter(target, { now: () => time });
  return { calls, target, uninstall, advance: ms => { time += ms; } };
}

test('the waiting state before the game starts is logged once as information', () => {
  const h = setup();
  for (let i = 0; i < 500; i += 1) {
    h.target.error(PREFIX, WAITING);
    h.advance(16);
  }
  assert.deepEqual(h.calls, [['log', `VR overlay: ${WAITING}`]]);
});

test('the waiting state is reported again after the bridge was quiet, e.g. when the game restarts', () => {
  const h = setup();
  h.target.error(PREFIX, WAITING);
  h.advance(31_000);
  h.target.error(PREFIX, WAITING);
  assert.equal(h.calls.length, 2);
});

test('real submission errors stay errors, are deduplicated and summarized', () => {
  const h = setup();
  h.target.error(PREFIX, 'Device lost');
  for (let i = 0; i < 10; i += 1) {
    h.advance(100);
    h.target.error(PREFIX, 'Device lost');
  }
  assert.deepEqual(h.calls, [['error', PREFIX, 'Device lost']]);
  for (let i = 0; i < 6; i += 1) {
    h.advance(10_000);
    h.target.error(PREFIX, 'Device lost');
  }
  assert.deepEqual(h.calls[1], ['error', `${PREFIX} Device lost (repeated 16 more times)`]);
  assert.equal(h.calls.length, 2);
  h.target.error(PREFIX, 'Other failure');
  assert.deepEqual(h.calls[2], ['error', PREFIX, 'Other failure']);
});

test('unrelated errors pass through and uninstall restores console.error', () => {
  const h = setup();
  const original = h.target.error;
  h.target.error('Something else', 42);
  h.target.error('Something else', 42);
  assert.equal(h.calls.length, 2);
  h.uninstall();
  assert.notEqual(h.target.error, original);
  h.target.error('after');
  assert.deepEqual(h.calls.at(-1), ['error', 'after']);
});
