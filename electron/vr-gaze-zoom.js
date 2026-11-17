function rotateToLocal(vector, rotation) {
  const q = { x: -rotation.x, y: -rotation.y, z: -rotation.z };
  const cross = {
    x: q.y * vector.z - q.z * vector.y,
    y: q.z * vector.x - q.x * vector.z,
    z: q.x * vector.y - q.y * vector.x,
  };
  return {
    x: vector.x + 2 * (rotation.w * cross.x + q.y * cross.z - q.z * cross.y),
    y: vector.y + 2 * (rotation.w * cross.y + q.z * cross.x - q.x * cross.z),
    z: vector.z + 2 * (rotation.w * cross.z + q.x * cross.y - q.y * cross.x),
  };
}

export function isLookingAtOverlay(pose, placement, width, curvature = 0) {
  const origin = rotateToLocal({
    x: pose.position.x - placement.position.x,
    y: pose.position.y - placement.position.y,
    z: pose.position.z - placement.position.z,
  }, placement.rotation);
  const direction = rotateToLocal(pose.direction, placement.rotation);
  const halfHeight = width * 9 / 16 / 2;
  if (curvature <= 0) {
    if (direction.z >= -1e-6) {
      return false;
    }
    const distance = -origin.z / direction.z;
    return distance > 0
      && Math.abs(origin.x + distance * direction.x) <= width / 2
      && Math.abs(origin.y + distance * direction.y) <= halfHeight;
  }
  const arc = curvature * 2 * Math.PI;
  const radius = width / arc;
  const a = direction.x ** 2 + direction.z ** 2;
  const b = 2 * (origin.x * direction.x + (origin.z - radius) * direction.z);
  const c = origin.x ** 2 + (origin.z - radius) ** 2 - radius ** 2;
  const discriminant = b * b - 4 * a * c;
  if (a < 1e-6 || discriminant < 0) {
    return false;
  }
  return [-1, 1].some(sign => {
    const distance = (-b + sign * Math.sqrt(discriminant)) / (2 * a);
    const x = origin.x + distance * direction.x;
    const z = origin.z + distance * direction.z;
    return distance > 0
      && direction.x * -x + direction.z * (radius - z) < 0
      && Math.abs(Math.atan2(x, radius - z)) <= arc / 2
      && Math.abs(origin.y + distance * direction.y) <= halfHeight;
  });
}

export function createVrGazeZoom({
  controller, reader, placement, sizeMeters, curvature, reductionFactor = 5, logger,
  reducedOpacity = 1, setOpacity = () => true,
  now = () => performance.now(), setIntervalFn = setInterval, clearIntervalFn = clearInterval,
}) {
  if (!Number.isFinite(reductionFactor) || reductionFactor < 1) {
    throw new Error('The VR gaze area reduction factor must be a finite number of at least 1.');
  }
  validateReducedOpacity(reducedOpacity);
  let scale = 1;
  let dim = 0;
  let appliedWidth = sizeMeters;
  let appliedOpacity = 1;
  let focused = true;
  let candidateSince = null;
  let previousTime = now();
  let stopped = false;
  let trackingLost = false;
  let failureReason = null;
  let paused = false;
  let timer;

  function applyWidth(width) {
    if (Math.abs(width - appliedWidth) < 0.0001) {
      return;
    }
    if (!controller.setSizeMeters(width)) {
      throw new Error('The VR runtime could not update the gaze zoom size.');
    }
    appliedWidth = width;
  }

  function applyOpacity(opacity) {
    const endpoint = opacity === 1 || opacity === reducedOpacity;
    if (opacity === appliedOpacity || (!endpoint && Math.abs(opacity - appliedOpacity) < 0.005)) {
      return;
    }
    if (setOpacity(opacity) === false) {
      throw new Error('The VR window could not update its opacity.');
    }
    appliedOpacity = opacity;
  }

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    clearIntervalFn(timer);
    try {
      try {
        applyOpacity(1);
      } finally {
        applyWidth(sizeMeters);
      }
    } finally {
      reader.close();
    }
  }

  function tick() {
    if (stopped || reductionFactor === 1) {
      return;
    }
    if (paused) {
      previousTime = now();
      return;
    }
    try {
      const time = now();
      const elapsed = Math.max(0, Math.min(100, time - previousTime));
      previousTime = time;
      const pose = reader.read();
      if (!pose) {
        if (!trackingLost) {
          if (reader.getState?.().status === 'waiting') {
            logger.info('VR adaptive sizing is waiting for headset tracking; reconnecting automatically.');
          } else {
            logger.warn('VR gaze zoom paused: headset tracking is unavailable.');
          }
          trackingLost = true;
        }
        focused = true;
        candidateSince = null;
      } else {
        if (trackingLost) {
          logger.info('VR gaze zoom resumed: headset tracking recovered.');
          trackingLost = false;
        }
        const looking = isLookingAtOverlay(
          pose, placement, sizeMeters * scale * (focused ? 1.08 : 1), curvature,
        );
        if (looking === focused) {
          candidateSince = null;
        } else if (candidateSince === null) {
          candidateSince = time;
        } else if (time - candidateSince >= (looking ? 200 : 400)) {
          focused = looking;
          candidateSince = null;
        }
      }
      const target = focused ? 1 : 1 / Math.sqrt(reductionFactor);
      const smoothing = 1 - Math.exp(-elapsed / 100);
      scale += (target - scale) * smoothing;
      if (Math.abs(target - scale) < 0.001) {
        scale = target;
      }
      const dimTarget = focused ? 0 : 1;
      dim += (dimTarget - dim) * smoothing;
      if (Math.abs(dimTarget - dim) < 0.001) {
        dim = dimTarget;
      }
      applyWidth(sizeMeters * scale);
      applyOpacity(dim === 1 ? reducedOpacity : 1 - (1 - reducedOpacity) * dim);
    } catch (error) {
      failureReason = error instanceof Error ? error.message : String(error);
      logger.error({ err: error }, 'VR gaze zoom failed; disabling tracking and restoring the configured size.');
      try {
        stop();
      } catch (cleanupError) {
        logger.error({ err: cleanupError }, 'Failed to clean up VR gaze zoom.');
      }
    }
  }

  try {
    if (reductionFactor > 1) {
      timer = setIntervalFn(tick, 50);
      timer.unref?.();
    }
  } catch (error) {
    stop();
    throw error;
  }
  return {
    close: stop,
    getState() {
      if (failureReason !== null) {
        return { status: 'unavailable', message: failureReason };
      }
      if (stopped) return { status: 'disabled' };
      if (reductionFactor === 1) return { status: 'disabled' };
      const readerState = reader.getState?.();
      if (readerState && readerState.status !== 'active') return readerState;
      if (trackingLost) return { status: 'unavailable', message: 'Headset tracking is unavailable.' };
      return { status: 'active' };
    },
    setPaused(value) {
      if (stopped) return;
      if (value) {
        applyWidth(sizeMeters);
        applyOpacity(1);
      }
      paused = value;
      scale = 1;
      dim = 0;
      focused = true;
      candidateSince = null;
      previousTime = now();
    },
    setReductionFactor(value) {
      if (!Number.isFinite(value) || value < 1) {
        throw new Error('The VR gaze area reduction factor must be a finite number of at least 1.');
      }
      if (value === 1) {
        applyWidth(sizeMeters);
        applyOpacity(1);
        scale = 1;
        dim = 0;
        focused = true;
        candidateSince = null;
        clearIntervalFn(timer);
        timer = null;
      } else if (reductionFactor === 1 && !stopped) {
        previousTime = now();
        timer = setIntervalFn(tick, 50);
        timer.unref?.();
      }
      reductionFactor = value;
    },
    setReducedOpacity(value) {
      validateReducedOpacity(value);
      reducedOpacity = value;
    },
    setAppearance(width, curve) {
      if (!Number.isFinite(width) || width < 0.1 || width > 3 || !Number.isFinite(curve) || curve < 0 || curve > 0.5) {
        throw new Error('Invalid VR window size or curvature.');
      }
      applyWidth(width);
      sizeMeters = width;
      curvature = curve;
      scale = 1;
      dim = 0;
      focused = true;
      candidateSince = null;
      applyOpacity(1);
    },
  };
}

export function validateReducedOpacity(value) {
  if (!Number.isFinite(value) || value < 0.1 || value > 1) {
    throw new Error('The reduced VR window opacity must be between 10% and 100%.');
  }
}
