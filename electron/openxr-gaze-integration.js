import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const registryKey = 'HKCU\\Software\\Khronos\\OpenXR\\1\\ApiLayers\\Implicit';
const readRegistration = `
$ErrorActionPreference = 'Stop'
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\\Khronos\\OpenXR\\1\\ApiLayers\\Implicit')
try {
  $registered = $null -ne $key -and $key.GetValueNames() -contains $env:COVAS_GAZE_MANIFEST
  $enabled = $registered -and $key.GetValueKind($env:COVAS_GAZE_MANIFEST) -eq [Microsoft.Win32.RegistryValueKind]::DWord -and $key.GetValue($env:COVAS_GAZE_MANIFEST) -eq 0
  @{ registered = $registered; enabled = $enabled } | ConvertTo-Json -Compress
} finally { if ($key) { $key.Dispose() } }
`;

export function createOpenXrGazeIntegration({
  userDataDirectory, binaryPath, platform = process.platform, exec = execFileSync,
  getHostProcessId = () => 0,
}) {
  const directory = path.join(userDataDirectory, 'openxr-gaze');
  const manifest = path.join(directory, 'covas_openxr_gaze.json');
  const options = { windowsHide: true, encoding: 'utf8' };
  let restartHostProcessId = null;
  function restartRequired() {
    if (restartHostProcessId !== null && getHostProcessId() !== restartHostProcessId) {
      restartHostProcessId = null;
    }
    return restartHostProcessId !== null;
  }
  function readRegistry() {
    const registration = JSON.parse(exec('powershell.exe', [
      '-NoProfile', '-NonInteractive', '-Command', readRegistration,
    ], { ...options, env: { ...process.env, COVAS_GAZE_MANIFEST: manifest } }));
    if (typeof registration.registered !== 'boolean' || typeof registration.enabled !== 'boolean') {
      throw new Error('Cannot read the OpenXR gaze integration registration.');
    }
    return registration;
  }
  function getStatus() {
    if (platform !== 'win32') return { supported: false, available: false, installed: false, enabled: false, restartRequired: false };
    const registration = readRegistry();
    let installed = false;
    let enabled = false;
    if (registration.registered && fs.existsSync(manifest)) {
      const data = JSON.parse(fs.readFileSync(manifest, 'utf8'));
      installed = data.api_layer?.name === 'XR_APILAYER_COVAS_NEXT_gaze'
        && typeof data.api_layer.library_path === 'string'
        && fs.existsSync(data.api_layer.library_path);
      enabled = installed && registration.enabled
        && data.file_format_version === '1.0.0'
        && data.api_layer.api_version === '1.0'
        && data.api_layer.implementation_version === '1'
        && data.api_layer.disable_environment === 'COVAS_NEXT_DISABLE_OPENXR_GAZE';
    }
    return { supported: true, available: fs.existsSync(binaryPath), installed, enabled, restartRequired: restartRequired() };
  }
  return {
    getStatus,
    install() {
      if (platform !== 'win32') throw new Error('The OpenXR gaze integration is available on Windows x64 only.');
      if (!fs.existsSync(binaryPath)) {
        throw new Error('OpenXR gaze component missing. Build it with npm run build:vr-gaze, or repair the COVAS:NEXT installation.');
      }
      const previous = getStatus();
      const previousLibrary = previous.installed ? JSON.parse(fs.readFileSync(manifest, 'utf8')).api_layer.library_path : null;
      const hostProcessId = getHostProcessId();
      const binary = fs.readFileSync(binaryPath);
      const hash = crypto.createHash('sha256').update(binary).digest('hex').slice(0, 16);
      fs.mkdirSync(directory, { recursive: true });
      const library = path.join(directory, `covas_openxr_gaze_${hash}.dll`);
      // Versioned DLLs can be updated without overwriting a library loaded by the game.
      if (!fs.existsSync(library)) fs.writeFileSync(library, binary, { flag: 'wx' });
      const temporary = `${manifest}.${crypto.randomUUID()}.tmp`;
      fs.writeFileSync(temporary, JSON.stringify({
        file_format_version: '1.0.0',
        api_layer: {
          name: 'XR_APILAYER_COVAS_NEXT_gaze',
          library_path: library,
          api_version: '1.0',
          implementation_version: '1',
          description: 'Local headset-direction tracking for COVAS:NEXT gaze zoom',
          disable_environment: 'COVAS_NEXT_DISABLE_OPENXR_GAZE',
        },
      }, null, 2));
      try {
        fs.renameSync(temporary, manifest);
      } finally {
        if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
      }
      if (previous.installed && (!previous.enabled || previousLibrary !== library)
        && hostProcessId > 0 && readRegistry().enabled) restartHostProcessId = hostProcessId;
      exec('reg.exe', ['add', registryKey, '/v', manifest, '/t', 'REG_DWORD', '/d', '0', '/f'], options);
      const status = getStatus();
      if (!status.enabled) throw new Error('The OpenXR gaze integration was not enabled.');
      if ((!previous.enabled || previousLibrary !== library) && hostProcessId > 0) restartHostProcessId = hostProcessId;
      return { ...status, restartRequired: restartRequired() };
    },
    uninstall() {
      if (platform !== 'win32') throw new Error('The OpenXR gaze integration is available on Windows x64 only.');
      const registration = readRegistry();
      const hostProcessId = getHostProcessId();
      if (registration.registered) exec('reg.exe', ['delete', registryKey, '/v', manifest, '/f'], options);
      if (registration.registered && hostProcessId > 0) restartHostProcessId = hostProcessId;
      const status = getStatus();
      if (status.enabled || status.installed) throw new Error('The OpenXR gaze integration is still registered.');
      return status;
    },
  };
}
