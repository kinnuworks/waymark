// Speech queue on top of SpeechSynthesis.
// - Never talks over itself: one utterance at a time, the rest wait.
// - Directions go stale fast, so only the newest waiting line is kept,
//   unless a line is marked { keep: true }.
// - urgent lines (stop, stairs) cut in immediately.
// - iOS only speaks after a user gesture, so call speech.unlock() in a tap.
// - Voices can load after the page does; we pick one when they arrive.

const synth = typeof window !== 'undefined' ? window.speechSynthesis : null;

let voice = null;
let unlocked = false;
let speaking = false;
let current = null; // keep a reference so the utterance is not garbage collected (Chrome bug)
let watchdog = 0;
const queue = [];
const listeners = new Set();

function pickVoice() {
  if (!synth) return;
  const voices = synth.getVoices();
  if (!voices.length) return;
  const lang = (navigator.language || 'en-GB').toLowerCase();
  const base = lang.split('-')[0];
  voice =
    voices.find((v) => v.lang.toLowerCase() === lang && v.localService) ||
    voices.find((v) => v.lang.toLowerCase() === lang) ||
    voices.find((v) => v.lang.toLowerCase().startsWith(base) && v.default) ||
    voices.find((v) => v.lang.toLowerCase().startsWith(base)) ||
    voices.find((v) => v.default) ||
    voices[0];
}

if (synth) {
  pickVoice();
  synth.addEventListener?.('voiceschanged', pickVoice);
}

function finish() {
  clearTimeout(watchdog);
  speaking = false;
  current = null;
  setTimeout(pump, 120); // short gap between lines
}

function pump() {
  if (!synth || speaking || !queue.length || !speech.enabled) return;
  const item = queue.shift();
  const u = new SpeechSynthesisUtterance(item.text);
  if (voice) u.voice = voice;
  u.lang = voice?.lang || navigator.language || 'en-GB';
  u.rate = speech.rate;
  u.pitch = 1;
  // Only the current line may advance the queue (a cancelled line fires late).
  u.onend = () => { if (current === u) finish(); };
  u.onerror = () => { if (current === u) finish(); };
  current = u;
  speaking = true;
  // Safari sometimes never fires onend; move on after a generous estimate.
  watchdog = setTimeout(() => { if (current === u) finish(); }, Math.min(15000, 1500 + item.text.length * 90 / speech.rate));
  synth.speak(u);
}

export const speech = {
  enabled: true,
  rate: 1,
  last: '',
  get supported() { return !!synth; },
  get unlocked() { return unlocked; },

  // Must run inside a click or touch handler at least once on iOS.
  unlock() {
    if (!synth || unlocked) return;
    try {
      const u = new SpeechSynthesisUtterance(' ');
      u.volume = 0;
      synth.speak(u);
      unlocked = true;
      pickVoice();
    } catch { /* ignore */ }
  },

  // Listeners receive every line, even when the voice is off, so pages can
  // mirror it into an aria-live region for screen reader users.
  onLine(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },

  say(text, { urgent = false, keep = false, remember = true } = {}) {
    text = String(text || '').trim();
    if (!text) return;
    if (remember) speech.last = text;
    listeners.forEach((fn) => fn(text, { urgent }));
    if (!synth || !speech.enabled) return;

    if (urgent) {
      queue.length = 0;
      if (speaking) {
        current = null;
        clearTimeout(watchdog);
        speaking = false;
        synth.cancel();
      }
      queue.push({ text });
      setTimeout(pump, 60);
      return;
    }

    // Skip if this exact line is being said or is already waiting.
    if (current && current.text === text) return;
    if (queue.some((q) => q.text === text)) return;

    if (!keep) {
      // Drop older waiting lines that were not marked keep.
      for (let i = queue.length - 1; i >= 0; i--) if (!queue[i].keep) queue.splice(i, 1);
    }
    queue.push({ text, keep });
    pump();
  },

  repeat() {
    if (speech.last) speech.say(speech.last, { urgent: true });
  },

  stop() {
    queue.length = 0;
    clearTimeout(watchdog);
    speaking = false;
    current = null;
    try { synth?.cancel(); } catch { /* ignore */ }
  },

  setEnabled(on) {
    speech.enabled = !!on;
    if (!on) speech.stop();
  },
};
