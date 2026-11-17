import koffi from 'koffi';

const packetSize = 64;

export function decodeOpenXrPose(packet, now) {
  if (packet.length !== packetSize) throw new Error('Invalid OpenXR gaze packet size.');
  const magic = packet.readUInt32LE(0);
  if (magic === 0 || packet.readUInt32LE(12) & 1) return null;
  if (magic !== 0x4358475a || packet.readUInt32LE(4) !== 1 || packet.readUInt32LE(8) !== packetSize) {
    throw new Error('OpenXR gaze protocol mismatch. Update the gaze integration and restart the VR application.');
  }
  const age = now - Number(packet.readBigUInt64LE(16));
  if (!packet.readUInt32LE(24) || age > 500) return null;
  if (age < -1000 || ![2, 3].includes(packet.readUInt32LE(28))) {
    throw new Error('OpenXR gaze returned an invalid clock or reference space.');
  }
  const values = Array.from({ length: 7 }, (_, index) => packet.readFloatLE(32 + index * 4));
  if (!values.every(Number.isFinite)) throw new Error('OpenXR gaze returned a non-finite headset pose.');
  const [px, py, pz, qx, qy, qz, qw] = values;
  const length = Math.hypot(qx, qy, qz, qw);
  if (length < 0.5 || length > 1.5) throw new Error('OpenXR gaze returned an invalid headset rotation.');
  const [x, y, z, w] = [qx, qy, qz, qw].map(value => value / length);
  return {
    position: { x: px, y: py, z: pz },
    direction: { x: -2 * (x * z + w * y), y: -2 * (y * z - w * x), z: -(1 - 2 * (x * x + y * y)) },
  };
}

export function createOpenXrHeadPoseReader(getHostProcessId) {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error('OpenXR gaze tracking is currently supported on Windows x64.');
  }
  const kernel = koffi.load('kernel32.dll');
  const openMapping = kernel.func('OpenFileMappingW', 'void *', ['uint32_t', 'bool', 'str16']);
  const mapView = kernel.func('void *MapViewOfFile(void *, uint32_t, uint32_t, uint32_t, size_t)');
  const unmapView = kernel.func('bool UnmapViewOfFile(void *)');
  const closeHandle = kernel.func('bool CloseHandle(void *)');
  const getError = kernel.func('uint32_t GetLastError()');
  const clock = kernel.func('uint64_t GetTickCount64()');
  let mapping = null;
  let view = null;
  let processId = 0;
  let checkedAt = -Infinity;
  let closed = false;
  let receivedPose = false;
  let state = { status: 'waiting', message: 'Waiting for the OpenXR game to start.' };

  function disconnect() {
    try {
      if (view && !unmapView(view)) throw new Error(`Cannot unmap OpenXR gaze data (${getError()}).`);
    } finally {
      view = null;
      if (mapping && !closeHandle(mapping)) throw new Error(`Cannot close OpenXR gaze data (${getError()}).`);
      mapping = null;
    }
  }

  function connect(now) {
    checkedAt = now;
    const nextProcess = getHostProcessId();
    if (!Number.isSafeInteger(nextProcess) || nextProcess < 0 || nextProcess > 0xffffffff) {
      throw new Error('Invalid OpenXR host process ID.');
    }
    if (processId === nextProcess && view) return true;
    disconnect();
    processId = nextProcess;
    receivedPose = false;
    if (!processId) {
      state = { status: 'waiting', message: 'Waiting for the OpenXR game to start.' };
      return false;
    }
    mapping = openMapping(4, false, `Local\\COVAS_NEXT_OpenXR_Gaze_v1_${processId}`);
    if (!mapping) {
      const error = getError();
      if (error === 2) {
        state = {
          status: 'waiting',
          message: 'The OpenXR game has not published tracking data yet. If this persists in VR, check the gaze integration.',
        };
        return false;
      }
      throw new Error(`Cannot open OpenXR gaze data (${error}).`);
    }
    view = mapView(mapping, 4, 0, 0, packetSize);
    if (!view) throw new Error(`Cannot map OpenXR gaze data (${getError()}).`);
    state = { status: 'waiting', message: 'Waiting for a valid OpenXR headset pose.' };
    return true;
  }

  function close() {
    if (closed) return;
    closed = true;
    try {
      disconnect();
    } finally {
      kernel.unload();
    }
  }

  try {
    connect(Number(clock()));
  } catch (error) {
    close();
    throw error;
  }
  return {
    read() {
      if (closed) throw new Error('OpenXR headset tracking has already been closed.');
      const now = Number(clock());
      if (now - checkedAt >= 1000) connect(now);
      if (!view) return null;
      // The producer's seqlock prevents mixing position and rotation from different frames.
      const before = koffi.decode(view, 12, 'uint32_t');
      if (before & 1) return null;
      const packet = Buffer.from(koffi.decode(view, 'uint8_t', packetSize));
      const after = koffi.decode(view, 12, 'uint32_t');
      if (before !== after || packet.readUInt32LE(12) !== before) return null;
      const pose = decodeOpenXrPose(packet, now);
      if (pose) {
        receivedPose = true;
        state = { status: 'active' };
      } else {
        state = {
          status: receivedPose ? 'unavailable' : 'waiting',
          message: receivedPose ? 'OpenXR headset tracking is unavailable.' : 'Waiting for a valid OpenXR headset pose.',
        };
      }
      return pose;
    },
    getState: () => closed ? { status: 'disabled' } : { ...state },
    close,
  };
}
