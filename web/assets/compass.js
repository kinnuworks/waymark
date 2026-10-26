// Compass heading from DeviceOrientation, in degrees clockwise from north.
// iOS: needs DeviceOrientationEvent.requestPermission() inside a tap, and
// reports webkitCompassHeading. Android Chrome: the deviceorientationabsolute
// event gives alpha counter-clockwise, so heading = 360 - alpha.

export const compass = {
  heading: null, // latest heading in degrees, or null when unknown
  source: null, // 'ios' | 'absolute' | null
  started: false,
};

const listeners = new Set();
export function onHeading(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit(h) {
  compass.heading = ((h % 360) + 360) % 360;
  listeners.forEach((fn) => fn(compass.heading));
}

// Call from inside a click or touch handler.
// Resolves to 'granted', 'denied' or 'unsupported'. Never throws.
export async function startCompass() {
  if (compass.started) return 'granted';
  if (typeof DeviceOrientationEvent === 'undefined') return 'unsupported';

  if (typeof DeviceOrientationEvent.requestPermission === 'function') {
    try {
      const res = await DeviceOrientationEvent.requestPermission();
      if (res !== 'granted') return 'denied';
    } catch {
      return 'denied';
    }
  }

  compass.started = true;

  window.addEventListener('deviceorientationabsolute', (e) => {
    if (e.alpha == null) return;
    compass.source = 'absolute';
    emit(360 - e.alpha);
  }, true);

  window.addEventListener('deviceorientation', (e) => {
    if (typeof e.webkitCompassHeading === 'number' && !Number.isNaN(e.webkitCompassHeading)) {
      compass.source = 'ios';
      emit(e.webkitCompassHeading);
    } else if (e.absolute === true && e.alpha != null && compass.source !== 'absolute') {
      compass.source = 'absolute';
      emit(360 - e.alpha);
    }
  }, true);

  return 'granted';
}

// Signed smallest difference a - b in degrees, in (-180, 180].
export function angleDiff(a, b) {
  let d = (((a - b) % 360) + 360) % 360;
  if (d > 180) d -= 360;
  return d;
}
