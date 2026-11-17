export async function createVrHeadPoseReader(controller) {
  const backend = controller.getSelectedBackend();
  if (backend === 'openvr') {
    const { createOpenVrHeadPoseReader } = await import('./openvr-head-pose.js');
    return createOpenVrHeadPoseReader();
  }
  if (backend === 'openxr') {
    const runtime = controller.getRuntimeInfo();
    if (runtime.openxrMode && runtime.openxrMode !== 'api-layer') {
      throw new Error('OpenXR gaze requires the OpenXR application-integration mode.');
    }
    const { createOpenXrHeadPoseReader } = await import('./openxr-head-pose.js');
    return createOpenXrHeadPoseReader(() => controller.getRuntimeInfo().openxrHostProcessId);
  }
  throw new Error(`Gaze zoom is unavailable for the ${backend} VR backend.`);
}

export async function createOptionalVrGazeZoom({
  controller, options, placement, logger, setOpacity, createReader = createVrHeadPoseReader,
}) {
  if (options.mode === 'vr-full') {
    let zoom = null;
    let closed = false;
    let paused = false;
    let state = { status: 'disabled' };
    let current = { ...options };
    const adaptive = {
      getState: () => zoom?.getState() ?? state,
      close() {
        closed = true;
        zoom?.close();
        zoom = null;
      },
      setPaused(value) {
        paused = value;
        zoom?.setPaused(value);
      },
      setAppearance(sizeMeters, curvature) {
        zoom?.setAppearance(sizeMeters, curvature);
        current.vrSizeMeters = sizeMeters;
        current.vrCurvature = curvature;
      },
      setReducedOpacity(value) {
        zoom?.setReducedOpacity(value);
        current.vrGazeReducedOpacity = value;
      },
      async setReductionFactor(value) {
        if (!Number.isFinite(value) || value < 1) throw new Error('The area reduction factor must be at least 1.');
        if (closed) throw new Error('The VR overlay has closed.');
        if (value === 1) {
          zoom?.close();
          zoom = null;
          state = { status: 'disabled' };
        } else if (zoom && zoom.getState().status !== 'disabled' && zoom.getState().status !== 'unavailable') {
          zoom.setReductionFactor(value);
        } else {
          zoom?.close();
          zoom = null;
          const result = await createOptionalVrGazeZoom({
            controller, placement, logger, setOpacity, createReader,
            options: { ...current, mode: 'vr', vrGazeZoom: true, vrGazeReductionFactor: value },
          });
          if (closed) {
            result.controller?.close();
            throw new Error('The VR overlay closed while enabling tracking.');
          }
          state = result.state;
          zoom = result.controller;
          if (!zoom) throw new Error(state.message ?? 'VR tracking is unavailable.');
          zoom.setPaused(paused);
        }
        current.vrGazeReductionFactor = value;
      },
    };
    try {
      await adaptive.setReductionFactor(options.vrGazeReductionFactor);
    } catch (error) {
      logger.error({ err: error }, 'Full-window adaptive sizing is unavailable.');
      state = { status: 'unavailable', message: error instanceof Error ? error.message : String(error) };
    }
    return { state: adaptive.getState(), controller: adaptive };
  }
  if (!options.vrGazeZoom) return { state: { status: 'disabled' }, controller: null };
  let reader;
  try {
    if (options.vrAnchor !== 'world') throw new Error('Gaze zoom requires the World Locked anchor.');
    reader = await createReader(controller);
    const zoom = createVrGazeZoom({
      controller, reader, placement, logger,
      sizeMeters: options.vrSizeMeters, curvature: options.vrCurvature,
      reductionFactor: options.vrGazeReductionFactor,
      reducedOpacity: options.vrGazeReducedOpacity ?? 1, setOpacity,
    });
    return { state: zoom.getState(), controller: zoom };
  } catch (error) {
    try {
      reader?.close();
    } catch (cleanupError) {
      logger.error({ err: cleanupError }, 'Failed to close gaze tracking after initialization failed.');
    }
    logger.error({ err: error }, 'Gaze zoom unavailable; keeping the overlay visible at its configured size.');
    return {
      state: { status: 'unavailable', message: error instanceof Error ? error.message : String(error) },
      controller: null,
    };
  }
}

export async function createVrHeightInitialization({
  controller, options, adjustment, logger, createReader = createVrHeadPoseReader,
  setIntervalFn = setInterval, clearIntervalFn = clearInterval,
}) {
  let reader;
  let timer;
  let stopped = false;
  let state = { status: 'disabled' };

  function close() {
    if (stopped) return;
    stopped = true;
    clearIntervalFn(timer);
    if (state.status === 'waiting') state = { status: 'disabled' };
    reader?.close();
  }

  function fail(error) {
    state = { status: 'unavailable', message: error instanceof Error ? error.message : String(error) };
    logger.error({ err: error }, 'VR height initialization failed; keeping the existing world placement.');
    try {
      close();
    } catch (cleanupError) {
      logger.error({ err: cleanupError }, 'Failed to close VR height tracking.');
    }
  }

  function tick() {
    if (stopped) return;
    try {
      if (!adjustment.initializeHeight(reader.read())) return;
      state = { status: 'active' };
      logger.info('VR window height initialized; saved offsets and world orientation are unchanged.');
      close();
    } catch (error) {
      fail(error);
    }
  }

  if (options.mode === 'vr-full' && options.vrAnchor === 'world') {
    state = { status: 'waiting', message: 'Waiting for a valid headset height. OpenXR requires the tracking integration.' };
    logger.info('VR window height initialization is waiting for headset tracking.');
    try {
      reader = await createReader(controller);
      timer = setIntervalFn(tick, 100);
      timer.unref?.();
      tick();
    } catch (error) {
      fail(error);
    }
  }
  return { close, getState: () => ({ ...state }) };
}
import { createVrGazeZoom } from './vr-gaze-zoom.js';
