import { createRequire } from 'node:module';
import path from 'node:path';
import koffi from 'koffi';

const require = createRequire(import.meta.url);
const trackedPose = koffi.struct('CovasTrackedDevicePose', {
  matrix: koffi.array('float', 12),
  velocity: koffi.array('float', 3),
  angularVelocity: koffi.array('float', 3),
  trackingResult: 'int32_t',
  valid: 'bool',
  connected: 'bool',
});
const readPosesType = koffi.proto(
  process.platform === 'win32' ? '__stdcall' : '__cdecl',
  'CovasGetDeviceToAbsoluteTrackingPose',
  'void',
  ['int32_t', 'float', koffi.out(koffi.pointer(trackedPose)), 'uint32_t'],
);

export function getOpenVrLibraryPath(platform = process.platform, arch = process.arch) {
  const library = platform === 'win32' ? 'openvr_api.dll' : 'libopenvr_api.so';
  if (!['win32', 'linux'].includes(platform) || arch !== 'x64') {
    throw new Error('VR gaze zoom requires SteamVR/OpenVR on Windows or Linux x64.');
  }
  const entry = require.resolve(`@covas-labs/electron-vr-prebuilt-${platform}-${arch}`);
  return path.join(path.dirname(entry), library).replace(/\.asar([\\/])/, '.asar.unpacked$1');
}

export function createOpenVrHeadPoseReader() {
  const library = koffi.load(getOpenVrLibraryPath());
  let closed = false;
  try {
    // Borrow the live overlay's OpenVR session; never initialize or shut it down here.
    const getInterface = library.func('void *VR_GetGenericInterface(const char *, _Out_ int32_t *)');
    const error = [0];
    const system = getInterface('FnTable:IVRSystem_023', error);
    if (!system || error[0] !== 0) {
      throw new Error(`SteamVR headset tracking is unavailable (OpenVR error ${error[0]}).`);
    }
    // Fixed public C ABI for IVRSystem_023: GetDeviceToAbsoluteTrackingPose is entry 11.
    const pointer = koffi.decode(system, 11 * koffi.sizeof('void *'), 'void *');
    if (!pointer) {
      throw new Error('SteamVR does not expose headset tracking.');
    }
    const readPoses = koffi.decode(pointer, readPosesType);
    return {
      read() {
        if (closed) {
          throw new Error('SteamVR headset tracking has already been closed.');
        }
        const poses = [{}];
        // Standing space matches electron-vr's world-locked overlay transforms.
        readPoses(1, 0, poses, 1);
        const pose = poses[0];
        if (!pose.valid || !pose.connected || pose.trackingResult !== 200) {
          return null;
        }
        const m = pose.matrix;
        if (!m.every(Number.isFinite)) {
          throw new Error('SteamVR returned an invalid headset transform.');
        }
        return {
          position: { x: m[3], y: m[7], z: m[11] },
          direction: { x: -m[2], y: -m[6], z: -m[10] },
        };
      },
      close() {
        if (!closed) {
          closed = true;
          library.unload();
        }
      },
    };
  } catch (error) {
    library.unload();
    throw error;
  }
}
