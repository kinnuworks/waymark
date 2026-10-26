// Rear camera access and JPEG capture.
// The server wants about one JPEG per second with the long side at 960 px.

export class CameraError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'CameraError';
    this.code = code; // insecure | unsupported | denied | missing | busy | unknown
  }
}

export async function openCamera(video, { facingMode = 'environment', width = 1280, height = 960 } = {}) {
  if (!window.isSecureContext) {
    throw new CameraError('insecure', 'The camera only works when this page is opened over https. Ask the organiser for the https link.');
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new CameraError('unsupported', 'This browser cannot use the camera. Try Safari on iPhone or Chrome on Android.');
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: facingMode }, width: { ideal: width }, height: { ideal: height } },
    });
  } catch (e) {
    throw toCameraError(e);
  }

  video.setAttribute('playsinline', ''); // iOS: keep the video inline, not fullscreen
  video.setAttribute('autoplay', '');
  video.muted = true;
  video.srcObject = stream;
  try {
    await video.play();
  } catch {
    // Autoplay can be refused until the first tap; frames still arrive once it plays.
  }
  await waitForFrames(video);
  return stream;
}

function toCameraError(e) {
  switch (e && e.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return new CameraError('denied', 'Camera access is blocked. Allow the camera for this site in your browser settings, then try again.');
    case 'NotFoundError':
    case 'OverconstrainedError':
      return new CameraError('missing', 'No suitable camera was found on this device.');
    case 'NotReadableError':
    case 'AbortError':
      return new CameraError('busy', 'Another app is using the camera. Close it and try again.');
    default:
      return new CameraError('unknown', 'The camera could not start. Reload the page and try again.');
  }
}

function waitForFrames(video, timeout = 4000) {
  if (video.readyState >= 2 && video.videoWidth) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { clearTimeout(t); video.removeEventListener('loadeddata', done); resolve(); };
    const t = setTimeout(done, timeout);
    video.addEventListener('loadeddata', done);
  });
}

export function stopCamera(stream) {
  stream?.getTracks().forEach((t) => t.stop());
}

const canvas = document.createElement('canvas');

// Draws the current video frame scaled so the long side is longSide px,
// and returns a JPEG Blob. Resolves to null if no frame is available yet.
export function captureJpeg(video, { longSide = 960, quality = 0.8 } = {}) {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return Promise.resolve(null);
  const scale = Math.min(1, longSide / Math.max(w, h));
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob((b) => resolve(b), 'image/jpeg', quality));
}
