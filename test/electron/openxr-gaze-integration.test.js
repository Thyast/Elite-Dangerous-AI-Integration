import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createOpenXrGazeIntegration } from '../../electron/openxr-gaze-integration.js';

function harness(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'covas-openxr-gaze-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const binary = path.join(directory, 'source.dll');
  fs.writeFileSync(binary, 'test-native-binary');
  let registered = false;
  const calls = [];
  const exec = (command, args, options) => {
    calls.push([command, args, options]);
    if (command === 'powershell.exe') return JSON.stringify({ registered, enabled: registered });
    assert.equal(command, 'reg.exe');
    assert.equal(args[1], 'HKCU\\Software\\Khronos\\OpenXR\\1\\ApiLayers\\Implicit');
    assert.ok(args[3].startsWith(directory));
    registered = args[0] === 'add';
    return '';
  };
  const integration = createOpenXrGazeIntegration({
    userDataDirectory: directory, binaryPath: binary, platform: 'win32', exec, ...overrides,
  });
  return { directory, binary, integration, calls, manifest: path.join(directory, 'openxr-gaze', 'covas_openxr_gaze.json') };
}

test('installs only a per-user API layer and preserves the selected OpenXR runtime', t => {
  const h = harness(t);
  assert.equal(h.integration.getStatus().installed, false);
  assert.equal(h.integration.install().enabled, true);
  const manifest = JSON.parse(fs.readFileSync(h.manifest, 'utf8'));
  assert.equal(manifest.api_layer.name, 'XR_APILAYER_COVAS_NEXT_gaze');
  assert.equal(manifest.api_layer.implementation_version, '1');
  assert.equal(manifest.api_layer.disable_environment, 'COVAS_NEXT_DISABLE_OPENXR_GAZE');
  assert.equal(fs.readFileSync(manifest.api_layer.library_path, 'utf8'), 'test-native-binary');
  assert.ok(h.calls.every(([_command, args]) => !args.join(' ').includes('ActiveRuntime')));
  assert.equal(h.integration.uninstall().enabled, false);
  assert.ok(fs.existsSync(manifest.api_layer.library_path));
});

test('updates use a new DLL filename without overwriting the version loaded by the game', t => {
  const h = harness(t);
  h.integration.install();
  const old = JSON.parse(fs.readFileSync(h.manifest, 'utf8')).api_layer.library_path;
  fs.writeFileSync(h.binary, 'updated-native-binary');
  h.integration.install();
  const current = JSON.parse(fs.readFileSync(h.manifest, 'utf8')).api_layer.library_path;
  assert.notEqual(old, current);
  assert.equal(fs.readFileSync(old, 'utf8'), 'test-native-binary');
  assert.equal(fs.readFileSync(current, 'utf8'), 'updated-native-binary');
  assert.ok(!fs.readdirSync(path.dirname(h.manifest)).some(name => name.endsWith('.tmp')));
});

test('missing native components and registry failures are explicit errors', t => {
  const h = harness(t);
  fs.unlinkSync(h.binary);
  assert.equal(h.integration.getStatus().available, false);
  assert.throws(() => h.integration.install(), /component missing/);
  const failed = harness(t, { exec: () => { throw new Error('Registry access denied'); } });
  assert.throws(() => failed.integration.install(), /Registry access denied/);
});

test('unsupported platforms do not read or write the registry', t => {
  const h = harness(t, { platform: 'linux' });
  assert.deepEqual(h.integration.getStatus(), { supported: false, available: false, installed: false, enabled: false, restartRequired: false });
  assert.throws(() => h.integration.install(), /Windows x64 only/);
  assert.equal(h.calls.length, 0);
});

test('restart notices follow effective changes to a connected host, not installation presence', t => {
  let host = 101;
  const h = harness(t, { getHostProcessId: () => host });
  assert.equal(h.integration.getStatus().restartRequired, false);
  assert.equal(h.integration.install().restartRequired, true);
  assert.equal(h.integration.getStatus().restartRequired, true);
  host = 202;
  assert.equal(h.integration.getStatus().restartRequired, false);
  assert.equal(h.integration.install().restartRequired, false);
  fs.writeFileSync(h.binary, 'new-tracking-version');
  assert.equal(h.integration.install().restartRequired, true);
  host = 0;
  assert.equal(h.integration.getStatus().restartRequired, false);
  assert.equal(h.integration.uninstall().restartRequired, false);
  assert.equal(h.integration.install().restartRequired, false);
  host = 303;
  assert.equal(h.integration.getStatus().restartRequired, false);
  assert.equal(h.integration.uninstall().restartRequired, true);
  host = 0;
  assert.equal(h.integration.uninstall().restartRequired, false);
});

test('an unchanged installation on reopening the settings has no restart notice', t => {
  const h = harness(t, { getHostProcessId: () => 101 });
  h.integration.install();
  const reopened = createOpenXrGazeIntegration({
    userDataDirectory: h.directory, binaryPath: h.binary, platform: 'win32',
    exec: (command, args, options) => {
      if (command === 'powershell.exe') return JSON.stringify({ registered: true, enabled: true });
      return '';
    },
    getHostProcessId: () => 101,
  });
  assert.equal(reopened.getStatus().restartRequired, false);
  assert.equal(reopened.install().restartRequired, false);
});

test('an invalid numeric implementation version is not enabled and repairing it requires a host restart', t => {
  let host = 0;
  const h = harness(t, { getHostProcessId: () => host });
  h.integration.install();
  const manifest = JSON.parse(fs.readFileSync(h.manifest, 'utf8'));
  const library = manifest.api_layer.library_path;
  manifest.api_layer.implementation_version = 1;
  fs.writeFileSync(h.manifest, JSON.stringify(manifest));
  host = 101;
  assert.equal(h.integration.getStatus().installed, true);
  assert.equal(h.integration.getStatus().enabled, false);
  assert.equal(h.integration.getStatus().restartRequired, false);
  const repaired = h.integration.install();
  assert.equal(repaired.enabled, true);
  assert.equal(repaired.restartRequired, true);
  const updated = JSON.parse(fs.readFileSync(h.manifest, 'utf8'));
  assert.equal(updated.api_layer.implementation_version, '1');
  assert.equal(updated.api_layer.library_path, library);
  host = 202;
  assert.equal(h.integration.getStatus().restartRequired, false);
});

test('a repaired manifest marks a restart even if rewriting its already-enabled registration fails', t => {
  const h = harness(t);
  h.integration.install();
  const manifest = JSON.parse(fs.readFileSync(h.manifest, 'utf8'));
  manifest.api_layer.implementation_version = 1;
  fs.writeFileSync(h.manifest, JSON.stringify(manifest));
  const integration = createOpenXrGazeIntegration({
    userDataDirectory: h.directory, binaryPath: h.binary, platform: 'win32',
    getHostProcessId: () => 101,
    exec: command => {
      if (command === 'powershell.exe') return JSON.stringify({ registered: true, enabled: true });
      throw new Error('Registry access denied');
    },
  });
  assert.throws(() => integration.install(), /Registry access denied/);
  assert.equal(integration.getStatus().enabled, true);
  assert.equal(integration.getStatus().restartRequired, true);
});
