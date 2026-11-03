import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const source = fileURLToPath(new URL('.', import.meta.url));
const root = path.resolve(source, '..', '..');
const output = path.join(root, 'build', 'openxr-gaze');
const compiler = process.env.ZIG || path.join(root, 'build', 'openxr-gaze-tools', 'ziglang', 'zig.exe');
const sdkCommit = '75c53b6e853dc12c7b3c771edc9c9c841b15faaa';

if (process.platform !== 'win32' || process.arch !== 'x64') {
  console.log('The OpenXR gaze layer is Windows x64 only; skipping its build.');
  process.exit(0);
}
try {
  await fs.access(compiler);
} catch (error) {
  throw new Error(
    'OpenXR gaze compiler missing. Run: python -m pip install --target build\\openxr-gaze-tools -r native\\openxr-gaze\\requirements.txt',
    { cause: error },
  );
}
await fs.mkdir(path.join(output, 'sdk', 'openxr'), { recursive: true });
const marker = path.join(output, 'sdk', 'revision');
const revision = await fs.readFile(marker, 'utf8').catch(error => {
  if (error.code === 'ENOENT') return '';
  throw error;
});
for (const name of ['openxr.h', 'openxr_platform_defines.h', 'openxr_loader_negotiation.h']) {
  const destination = path.join(output, 'sdk', 'openxr', name);
  if (revision === sdkCommit && await fs.access(destination).then(() => true, error => {
    if (error.code === 'ENOENT') return false;
    throw error;
  })) continue;
  const response = await fetch(`https://raw.githubusercontent.com/KhronosGroup/OpenXR-SDK/${sdkCommit}/include/openxr/${name}`, { signal: AbortSignal.timeout(30000) });
  if (!response.ok) throw new Error(`OpenXR SDK download failed: ${name} (HTTP ${response.status}).`);
  await fs.writeFile(destination, Buffer.from(await response.arrayBuffer()));
}
const license = path.join(output, 'OPENXR-SDK-LICENSE');
const hasLicense = await fs.access(license).then(() => true, error => {
  if (error.code === 'ENOENT') return false;
  throw error;
});
if (revision !== sdkCommit || !hasLicense) {
  const response = await fetch(`https://raw.githubusercontent.com/KhronosGroup/OpenXR-SDK/${sdkCommit}/LICENSE`, { signal: AbortSignal.timeout(30000) });
  if (!response.ok) throw new Error(`OpenXR SDK license download failed (HTTP ${response.status}).`);
  await fs.writeFile(license, Buffer.from(await response.arrayBuffer()));
}
await fs.writeFile(marker, sdkCommit);

function compile(input, destination, extra = []) {
  const result = spawnSync(compiler, [
    'cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O2',
    '-DWIN32_LEAN_AND_MEAN', '-I', path.join(output, 'sdk'),
    ...extra, path.join(source, input), '-o', path.join(output, destination),
  ], { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`OpenXR gaze compilation failed: ${input} (${result.status}).`);
}
compile('layer.c', 'covas_openxr_gaze.dll', ['-shared']);
console.log(`Built ${path.join(output, 'covas_openxr_gaze.dll')}`);
if (process.argv.includes('--test')) {
  compile('layer_test.c', 'layer_test.exe', ['-UNDEBUG']);
  const result = spawnSync(path.join(output, 'layer_test.exe'), [
    path.join(output, 'covas_openxr_gaze.dll'),
  ], { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`OpenXR gaze native tests failed (${result.status}).`);
}
