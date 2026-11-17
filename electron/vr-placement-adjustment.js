import { validateReducedOpacity } from './vr-gaze-zoom.js';

const fields = {
  horizontal: ['vrHorizontalOffset', 'overlay_vr_horizontal_offset', -2, 2],
  vertical: ['vrVerticalOffset', 'overlay_vr_vertical_offset', -1.5, 1.5],
  distance: ['vrDistanceOffset', 'overlay_vr_distance_offset', -1.5, 1.5],
  pitch: ['vrTiltDegrees', 'overlay_vr_tilt_degrees', -90, 90],
  yaw: ['vrYawDegrees', 'overlay_vr_yaw_degrees', -90, 90],
};
const profileFields = {
  ...Object.fromEntries(Object.values(fields).map(([option, key, min, max]) => [key, [option, min, max]])),
  overlay_vr_size_meters: ['vrSizeMeters', 0.1, 3],
  overlay_vr_curvature: ['vrCurvature', 0, 0.5],
  overlay_vr_gaze_reduction_factor: ['vrGazeReductionFactor', 1, Infinity],
  overlay_vr_gaze_reduced_opacity: ['vrGazeReducedOpacity', 0.1, 1],
};

export function getOverlayPlacement(vrAnchor, vrSizeMeters, horizontalOffset = 0, verticalOffset = 0, distanceOffset = 0, tiltDegrees = 0, yawDegrees = 0, origin = null) {
  const halfPitch = tiltDegrees * Math.PI / 360;
  const halfYaw = yawDegrees * Math.PI / 360;
  const rotation = {
    x: Math.cos(halfYaw) * Math.sin(halfPitch),
    y: Math.sin(halfYaw) * Math.cos(halfPitch),
    z: -Math.sin(halfYaw) * Math.sin(halfPitch),
    w: Math.cos(halfYaw) * Math.cos(halfPitch),
  };
  if (vrAnchor === 'world') {
    if (origin) {
      const { position, direction } = origin;
      if (![position?.x, position?.y, position?.z, direction?.x, direction?.y, direction?.z].every(Number.isFinite)
          || Math.hypot(direction.x, direction.z) < 1e-6) {
        throw new Error('A valid forward-facing headset pose is required to recenter COVAS.');
      }
      // Preserve the horizon and configured tilt; only the headset's heading defines the new forward axis.
      const yaw = Math.atan2(-direction.x, -direction.z);
      const sin = Math.sin(yaw);
      const cos = Math.cos(yaw);
      const localX = horizontalOffset - 0.5;
      const localZ = -Math.max(0.1, 2 + distanceOffset);
      const qy = Math.sin(yaw / 2);
      const qw = Math.cos(yaw / 2);
      return {
        mode: 'world',
        position: {
          x: position.x + cos * localX + sin * localZ,
          y: position.y + verticalOffset - (720 / 1280) / 2,
          z: position.z - sin * localX + cos * localZ,
        },
        rotation: {
          x: qw * rotation.x + qy * rotation.z,
          y: qw * rotation.y + qy * rotation.w,
          z: qw * rotation.z - qy * rotation.x,
          w: qw * rotation.w - qy * rotation.y,
        },
      };
    }
    return {
      mode: 'world',
      position: { x: horizontalOffset - 0.5, y: 1.4 + verticalOffset - (720 / 1280) / 2, z: -Math.max(0.1, 2 + distanceOffset) },
      rotation,
    };
  }
  return {
    mode: 'head',
    position: { x: horizontalOffset, y: verticalOffset, z: -Math.max(0.1, 1.1 + distanceOffset) },
    rotation,
  };
}

export function createVrPlacementAdjustment({ controller, options, placement, gazeZoom, readHeadPose }) {
  let current = { ...options };
  let original = null;
  let prepared = false;
  let active = false;
  let sessionId = null;
  let origin = null;
  let recentering = false;
  let generation = 0;

  function config() {
    return Object.fromEntries(Object.values(fields).map(([option, key]) => [key, current[option]]));
  }

  function apply(next, nextOrigin = origin) {
    const nextPlacement = getOverlayPlacement(
      next.vrAnchor, next.vrSizeMeters, next.vrHorizontalOffset, next.vrVerticalOffset,
      next.vrDistanceOffset, next.vrTiltDegrees, next.vrYawDegrees, nextOrigin,
    );
    if (!controller.setPlacement(nextPlacement)) {
      throw new Error('The VR runtime rejected the window placement.');
    }
    Object.assign(placement, nextPlacement);
    current = next;
    origin = nextOrigin;
  }

  async function applyProfile(values) {
    if (current.vrContent !== 'interface') throw new Error('Ship profiles require the full VR window.');
    if (active) throw new Error('Finish or cancel the current adjustment before switching ship profiles.');
    if (!values || Object.keys(values).length !== Object.keys(profileFields).length) throw new Error('Invalid VR ship profile.');
    const next = { ...current };
    for (const [key, [option, min, max]] of Object.entries(profileFields)) {
      if (!Number.isFinite(values[key]) || values[key] < min || values[key] > max) throw new Error(`Invalid VR profile setting: ${key}.`);
      next[option] = values[key];
    }
    const previous = { ...current };
    const startedGeneration = generation;
    recentering = true;
    gazeZoom?.setPaused(true);
    try {
      if (!controller.setSizeMeters(next.vrSizeMeters) || !controller.setCurvature(next.vrCurvature)) {
        throw new Error('The VR runtime rejected the profile size or curvature.');
      }
      gazeZoom?.setAppearance(next.vrSizeMeters, next.vrCurvature);
      gazeZoom?.setReducedOpacity(next.vrGazeReducedOpacity);
      await gazeZoom?.setReductionFactor(next.vrGazeReductionFactor);
      if (generation !== startedGeneration) throw new Error('The VR overlay closed while applying a ship profile.');
      apply(next);
    } catch (error) {
      if (!controller.setSizeMeters(previous.vrSizeMeters) || !controller.setCurvature(previous.vrCurvature ?? 0)) {
        throw new Error(`VR profile failed and its appearance could not be restored: ${error.message}`);
      }
      gazeZoom?.setAppearance(previous.vrSizeMeters, previous.vrCurvature ?? 0);
      gazeZoom?.setReducedOpacity(previous.vrGazeReducedOpacity ?? 0.25);
      await gazeZoom?.setReductionFactor(previous.vrGazeReductionFactor ?? 1);
      throw error;
    } finally {
      if (generation === startedGeneration) {
        recentering = false;
        gazeZoom?.setPaused(false);
      }
    }
    return { active, config: { ...values } };
  }

  async function recenter() {
    if (current.vrAnchor === 'head') return { active, recentered: true };
    if (!readHeadPose) throw new Error('Headset tracking is unavailable for COVAS recentering.');
    const startedGeneration = generation;
    recentering = true;
    try {
      const pose = await readHeadPose();
      if (generation !== startedGeneration) throw new Error('The VR overlay closed during recentering.');
      if (!pose) throw new Error('No fresh headset pose is available. In OpenXR, check the gaze integration and enter VR before retrying.');
      gazeZoom?.setPaused(true);
      apply(current, structuredClone(pose));
      return { active, recentered: true };
    } finally {
      if (generation === startedGeneration) {
        recentering = false;
        gazeZoom?.setPaused(active);
      }
    }
  }

  function handle({ operation, axis, delta, factor, opacity, config: profile, size, curvature, session_id: requestedSession }) {
    if (recentering) throw new Error('A VR recenter is already in progress.');
    if (operation === 'recenter') return recenter();
    if (operation === 'apply_profile') return applyProfile(profile);
    if (operation === 'set_appearance' || operation === 'restore_appearance') {
      if (!Number.isFinite(size) || size < 0.1 || size > 3 || !Number.isFinite(curvature) || curvature < 0 || curvature > 0.5) {
        throw new Error('VR size must be between 0.1 and 3 metres; curvature between 0 and 180 degrees.');
      }
      const previous = { size: current.vrSizeMeters, curvature: current.vrCurvature ?? 0 };
      if (!controller.setSizeMeters(size)) throw new Error('The VR runtime rejected the window size.');
      if (!controller.setCurvature(curvature)) {
        if (!controller.setSizeMeters(previous.size)) throw new Error('The VR runtime rejected curvature and failed to restore the size.');
        throw new Error('The VR runtime rejected the window curvature.');
      }
      try {
        gazeZoom?.setAppearance(size, curvature);
      } catch (error) {
        if (!controller.setSizeMeters(previous.size) || !controller.setCurvature(previous.curvature)) {
          throw new Error(`VR appearance failed and could not be restored: ${error.message}`);
        }
        gazeZoom?.setAppearance(previous.size, previous.curvature);
        throw error;
      }
      current.vrSizeMeters = size;
      current.vrCurvature = curvature;
      return { active, size, curvature, previous };
    }
    if (operation === 'set_reduction' || operation === 'restore_reduction') {
      if (!Number.isFinite(factor) || factor < 1) throw new Error('The area reduction factor must be a finite number of at least 1.');
      const previous = current.vrGazeReductionFactor ?? 5;
      const result = gazeZoom?.setReductionFactor(factor);
      const finish = () => {
        current.vrGazeReductionFactor = factor;
        return { active, reduction_factor: factor, previous_factor: previous };
      };
      return result?.then ? result.then(finish) : finish();
    }
    if (operation === 'set_opacity' || operation === 'restore_opacity') {
      validateReducedOpacity(opacity);
      const previous = current.vrGazeReducedOpacity ?? 0.25;
      gazeZoom?.setReducedOpacity(opacity);
      current.vrGazeReducedOpacity = opacity;
      return { active, reduced_opacity: opacity, previous_opacity: previous };
    }
    if (typeof requestedSession !== 'string' || !requestedSession) throw new Error('Missing VR adjustment session ID.');
    if (operation === 'begin') {
      if (active) {
        if (sessionId === requestedSession) return { active, config: config() };
        throw new Error('A VR placement adjustment is already active.');
      }
      if (current.vrContent !== 'interface') {
        throw new Error('Select VR Overlay / Full Window in Overlay Mode before adjusting the GUI.');
      }
      gazeZoom?.setPaused(true);
      original = { ...current };
      active = true;
      prepared = false;
      sessionId = requestedSession;
    } else {
      if (!original || sessionId !== requestedSession) throw new Error('No matching VR placement adjustment is active.');
      if (operation === 'move') {
        if (!active) throw new Error('The VR placement adjustment has ended.');
        if (prepared) throw new Error('The position is being saved; finish or cancel the adjustment first.');
        if (!Object.hasOwn(fields, axis) || !Number.isFinite(delta) || delta === 0) {
          throw new Error('Invalid VR placement axis or increment.');
        }
        const [option, , min, max] = fields[axis];
        const value = Math.round((current[option] + delta) * 1e6) / 1e6;
        if (value < min || value > max) {
          throw new Error(`VR ${axis} must remain between ${min} and ${max}${axis === 'pitch' || axis === 'yaw' ? ' degrees' : ' metres'}.`);
        }
        apply({ ...current, [option]: value });
      } else if (operation === 'commit') {
        prepared = true;
      } else if (operation === 'finish' || operation === 'cancel') {
        if (operation === 'finish' && !prepared) throw new Error('Confirm the position before finishing.');
        if (operation === 'cancel') {
          apply({
            ...original,
            vrGazeReductionFactor: current.vrGazeReductionFactor,
            vrGazeReducedOpacity: current.vrGazeReducedOpacity,
            vrSizeMeters: current.vrSizeMeters,
            vrCurvature: current.vrCurvature,
          });
        }
        gazeZoom?.setPaused(false);
        active = false;
        if (operation === 'cancel') prepared = false;
      } else {
        throw new Error(`Unknown VR placement operation: ${operation}`);
      }
    }
    return { active, config: config() };
  }

  return {
    handle,
    initializeHeight(pose) {
      if (current.vrAnchor !== 'world' || origin) return true;
      if (active || recentering) return false;
      if (!pose) return false;
      if (!Number.isFinite(pose.position?.y)) {
        throw new Error('A valid headset height is required to initialize the VR window.');
      }
      // Only establish the vertical reference; startup must not inherit the headset's heading or horizontal position.
      apply(current, { position: { x: 0, y: pose.position.y, z: 0 }, direction: { x: 0, y: 0, z: -1 } });
      return true;
    },
    getSettings() {
      return { ...current };
    },
    close() {
      generation++;
      recentering = false;
      return active ? handle({ operation: 'cancel', session_id: sessionId }) : { active, config: config() };
    },
  };
}

export function handleVrPlacementRequest(message, adjustment) {
  if (typeof message.request_id !== 'string' || !message.request_id) {
    throw new Error('Missing VR placement request ID.');
  }
  if (!adjustment) throw new Error('No active VR overlay. Start the assistant with a VR overlay enabled.');
  return adjustment.handle(message);
}
