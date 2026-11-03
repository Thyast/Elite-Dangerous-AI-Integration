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
  controller, options, placement, logger, createReader = createVrHeadPoseReader,
}) {
  if (!options.vrGazeZoom) return { state: { status: 'disabled' }, controller: null };
  let reader;
  try {
    if (options.vrAnchor !== 'world') throw new Error('Gaze zoom requires the World Locked anchor.');
    reader = await createReader(controller);
    const zoom = createVrGazeZoom({
      controller, reader, placement, logger,
      sizeMeters: options.vrSizeMeters, curvature: options.vrCurvature,
      reductionFactor: options.vrGazeReductionFactor,
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
import { createVrGazeZoom } from './vr-gaze-zoom.js';
