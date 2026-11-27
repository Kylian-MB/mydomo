/* The voice of the chat page. The person turns it on once (a button); from then on, and at each
   later visit once the browser has already granted the microphone, the page listens while it is
   open. Speech is told from silence IN THE PAGE (voice/detector.js): only moments of speech leave
   the browser, as raw sound (16-bit little-endian, 16 000 Hz, mono) over one WebSocket, /chat/voice
   (channels/voice/protocol.py is the definition of the messages).

   No sentence is written here: every text the person reads is the assistant's, handed over in the
   data- attributes of fragments/voice.html (a name here, a text there). The assistant's voice is
   played by voice/player.js: the messages and the sound the page is sent are routed to the hooks
   of `window.DomoVoice` (`onMessage`, `onSound`), which that player fills.

   States, as `DomoVoice.state().state` and `data-state` of the fragment:
     off, preparing, listening, unavailable, no_microphone, refused, moved, reload.
   "listening" is said only while the microphone is open, the audio runs, the socket is up and
   the house can hear: the line is never more optimistic than the facts. When the house hears only
   what is said with a gesture (`state` with hearing "gesture"), the talk button shows, and a moment
   of speech is sent only while it is held. */

import { createDetector } from "./voice/detector.js";
import { createPlayer, playbackHooks } from "./voice/player.js";

const FRAME_SAMPLES = 512;
const FRAME_BYTES = FRAME_SAMPLES * 2;
// The detector's thresholds: speech begins at SPEECH_ABOVE (twice in a row), a frame below
// SPEECH_BELOW is silence, and 700 ms of silence in a row (22 frames of 32 ms) ends the moment.
const SPEECH_ABOVE = 0.5;
const SPEECH_BELOW = 0.35;
const START_FRAMES = 2;
const SILENCE_FRAMES = 22;
// 300 ms of sound kept before speech begins (10 frames), plus the frames that proved it.
const PREROLL_FRAMES = 10;
// A moment is cut at about 28 s, under the 30 s the house cuts at itself (protocol.py).
const MAX_MOMENT_FRAMES = Math.floor((28 * 16000) / FRAME_SAMPLES);
// Frames waiting for the detector: past this the page drops sound rather than fall behind.
const MAX_PENDING = 100;
// The house takes a binary frame of at most 64 KiB (protocol.py).
const MAX_FRAME_BYTES = 65536;
// A moment is not begun while this much is still waiting to leave the page.
const MAX_BUFFERED_BYTES = 1000000;
const BACKOFF_FIRST_MS = 1000;
const BACKOFF_LAST_MS = 30000;
const STABLE_MS = 10000;
const RELOAD_GAP_MS = 60000;

const CLOSE_NORMAL = 1000;
const CLOSE_POLICY = 1008;
const CLOSE_ANOTHER_PERSON = 4403;
const CLOSE_REPLACED = 4409;

const STORE_CHOICE = "domo.voice";
const STORE_MICROPHONE = "domo.microphone";
const STORE_RELOAD = "domo.voice.reload";

// The text of each state is a name of the fragment's data-text- attributes.
const STATE_TEXT = {
  off: "off",
  preparing: "preparing",
  listening: "listening",
  unavailable: "unavailable",
  no_microphone: "no_microphone",
  refused: "refused_by_browser",
  moved: "moved",
  reload: "reload",
};
const RUNNING_STATES = ["preparing", "listening", "unavailable"];

const root = document.querySelector('[data-voice="root"]');
const toggle = root && root.querySelector('[data-voice="toggle"]');
const line = root && root.querySelector('[data-voice="state"]');
const notice = root && root.querySelector('[data-voice="notice"]');
const talk = root && root.querySelector('[data-voice="talk"]');
const NOTICE_MS = 10000;
let noticeTimer = null;

// --- storage: a convenience of this device, the page works without it ---------------------------

function stored(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

function store(key, value) {
  try {
    if (value === null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
  } catch (error) {
    // Private window or blocked storage: the choice is simply not remembered.
  }
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

let ownId = null;

function deviceId() {
  if (ownId === null) {
    const kept = stored(STORE_MICROPHONE);
    if (kept !== null && /^[0-9a-f]{16}$/.test(kept)) {
      ownId = kept;
    } else {
      ownId = hex(crypto.getRandomValues(new Uint8Array(8)));
      store(STORE_MICROPHONE, ownId);
    }
  }
  return ownId;
}

// The id the page gives the house: this device's own id and a short hash of the input in use
// (a headset and a room microphone of one device are two microphones).
async function microphoneName(track) {
  let short = "x";
  try {
    const input = (track.getSettings && track.getSettings().deviceId) || "default";
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
    short = hex(new Uint8Array(digest).slice(0, 4));
  } catch (error) {
    // No hash: the id still tells the device.
  }
  return deviceId() + ":" + short;
}

// --- what the page says --------------------------------------------------------------------------

let current = "off";
let hearing = null;
let speechOn = false;
let voicePlaying = false; // the assistant's voice is coming out of the speakers

function textOf(name) {
  return (root && root.getAttribute("data-text-" + name.split("_").join("-"))) || "";
}

function render() {
  if (!root) {
    return;
  }
  root.setAttribute("data-state", current);
  root.setAttribute("data-speech", speechOn ? "on" : "off");
  root.setAttribute("data-playing", voicePlaying ? "on" : "off");
  const running = RUNNING_STATES.includes(current);
  toggle.textContent = textOf(running ? "turn_off" : "turn_on");
  toggle.setAttribute("aria-pressed", running ? "true" : "false");
  const onGesture = hearing === "gesture" && current === "listening";
  if (talk) {
    talk.hidden = !onGesture;
  }
  // The line says the voice plays only while it really does and the microphone still listens.
  let shown = STATE_TEXT[current];
  if (voicePlaying && current === "listening") {
    shown = "speaking";
  } else if (onGesture) {
    shown = "on_gesture";
  }
  line.textContent = textOf(shown);
}

function setPlaying(on) {
  voicePlaying = on;
  render();
}

// Why the house did not take a sentence, and the notice that tells it.
const NOT_TAKEN = new Map([
  ["elsewhere", "not_taken_elsewhere"],
  ["not_now", "not_taken_now"],
]);

// A notice about one sentence (not taken, not answered), in the assistant's words; it goes by
// itself, and with the next moment of speech.
function showNotice(name) {
  if (!notice) {
    return;
  }
  notice.textContent = textOf(name);
  notice.hidden = false;
  window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(clearNotice, NOTICE_MS);
}

function clearNotice() {
  if (notice) {
    notice.hidden = true;
    notice.textContent = "";
  }
}

function setState(next) {
  current = next;
  render();
}

// What the state is, from what is true now: the microphone and the audio, the socket, the house.
function refresh() {
  if (!active) {
    return;
  }
  if (!audio || audio.state !== "running") {
    setState("preparing");
  } else if (!canSend()) {
    setState(hearing === "preparing" && socketIsUp() ? "preparing" : "unavailable");
  } else {
    setState("listening");
  }
}

// --- the microphone and the sound ------------------------------------------------------------------

let active = false; // the voice is on: the page keeps (or tries to keep) the microphone open
let generation = 0; // a number that changes at each turn on or off: late answers check it
let stream = null;
let audio = null;
let node = null;
let silent = null;
let detector = null;
let microphone = null;
let speaker = null; // the player of the assistant's voice (voice/player.js), on the same context

async function openMicrophone() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw Object.assign(new Error("none"), { name: "NotAllowedError" });
  }
  // The browser's echo cancellation, noise suppression and automatic gain, for a voice that
  // sounds the same whatever the room: the voice print will be made from this sound.
  return navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
}

function closeMicrophone() {
  if (speaker) {
    speaker.close();
    speaker = null;
    setPlaying(false);
  }
  if (node) {
    node.port.onmessage = null;
    node.disconnect();
    node = null;
  }
  if (silent) {
    silent.disconnect();
    silent = null;
  }
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }
  if (audio) {
    audio.onstatechange = null;
    audio.close().catch(() => {});
    audio = null;
  }
  frames.length = 0;
  pending = 0;
  abandonMoment();
  preroll.length = 0;
}

// Everything stops: the microphone, the socket, the choice to listen (the stored choice stays
// unless the caller clears it), and the line says why.
function stopAll(next) {
  if (speaking) {
    sendJson({ type: "speech_end" });
  }
  if (speaker) {
    speaker.stop(); // tells the house what was cut while the socket is still up
  }
  active = false;
  generation += 1;
  closeSocket(CLOSE_NORMAL);
  closeMicrophone();
  clearNotice();
  setState(next);
}

async function startCapture(mine) {
  const [microphoneTry, detectorTry] = await Promise.allSettled([
    openMicrophone(),
    detector ? Promise.resolve(detector) : createDetector(new URL("./voice/", import.meta.url).href),
  ]);
  const opened = microphoneTry.status === "fulfilled" ? microphoneTry.value : null;
  // Whatever is opened here is held by this call alone until it is handed to the page's state at
  // the end: on any failure or late answer it is stopped, so the browser's recording indicator
  // never stays on for a voice that is not listening.
  let context = null;
  const release = () => {
    if (opened) {
      opened.getTracks().forEach((track) => track.stop());
    }
    if (context) {
      context.close().catch(() => {});
    }
  };
  try {
    if (opened === null || detectorTry.status !== "fulfilled") {
      throw opened === null ? microphoneTry.reason : detectorTry.reason;
    }
    if (mine !== generation) {
      release();
      return false;
    }
    detectorTry.value.reset();
    const track = opened.getAudioTracks()[0];
    const name = await microphoneName(track);
    if (mine !== generation) {
      release();
      return false;
    }
    context = new AudioContext({ latencyHint: "interactive" });
    await context.audioWorklet.addModule(new URL("./voice/capture-worklet.js", import.meta.url).href);
    if (mine !== generation) {
      release();
      return false;
    }
    // The voice is played through the same context, so the browser's echo cancellation knows
    // what comes out of the speakers. A browser that cannot make the player still listens.
    let player = null;
    try {
      player = createPlayer(context, { send: sendJson, onPlaying: setPlaying, notify: showNotice });
      await player.ready;
    } catch (error) {
      if (player) {
        player.close();
      }
      player = null;
    }
    if (mine !== generation) {
      if (player) {
        player.close();
      }
      release();
      return false;
    }
    const worklet = new AudioWorkletNode(context, "domo-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
    });
    // The worklet writes no output: it is connected, silenced, to the speakers so that the
    // browser keeps it running.
    const mute = context.createGain();
    mute.gain.value = 0;
    context.createMediaStreamSource(opened).connect(worklet);
    worklet.connect(mute);
    mute.connect(context.destination);
    // Handed over: from here the page's state owns them (closeMicrophone).
    stream = opened;
    detector = detectorTry.value;
    microphone = name;
    audio = context;
    speaker = player;
    node = worklet;
    silent = mute;
    node.port.onmessage = (event) => onFrame(event.data);
    audio.onstatechange = refresh;
    track.addEventListener("ended", () => {
      if (mine === generation && active) {
        stopAll("no_microphone");
      }
    });
    if (audio.state !== "running") {
      // Opened without a gesture (the page opened by itself): the browser may keep the sound
      // suspended until the person touches the page.
      audio.resume().catch(() => {});
    }
    return true;
  } catch (error) {
    release();
    throw error;
  }
}

// A page that opened by itself: the first touch lets a suspended audio run.
function resumeOnTouch() {
  if (audio && audio.state === "suspended") {
    audio.resume().catch(() => {});
  }
}
document.addEventListener("pointerdown", resumeOnTouch);
document.addEventListener("keydown", resumeOnTouch);

// --- speech found in the page ----------------------------------------------------------------------

const preroll = []; // the last frames while nobody speaks
const frames = []; // frames waiting for the detector
let pending = 0;
let busy = false;
let loud = 0; // frames of speech in a row, before a moment begins
let quiet = 0; // frames of silence in a row, in a moment
let sentFrames = 0;
let speaking = false;
let pressed = false; // the talk button is held (when the house hears only after a gesture)

function onFrame(frame) {
  if (!active) {
    return;
  }
  if (pending >= MAX_PENDING) {
    // The detector fell behind: this sound is dropped, and a moment in progress ends.
    if (speaking) {
      sendJson({ type: "speech_end" });
    }
    abandonMoment();
    return;
  }
  pending += 1;
  frames.push(frame);
  drain();
}

async function drain() {
  if (busy) {
    return;
  }
  busy = true;
  const mine = generation;
  try {
    while (frames.length > 0 && mine === generation) {
      const frame = frames.shift();
      await handle(frame, mine);
      pending = Math.max(0, pending - 1);
    }
  } catch (error) {
    // The detector failed: nothing can be heard until the voice is turned on again.
    if (mine === generation && active) {
      stopAll("unavailable");
    }
  } finally {
    busy = false;
    if (mine !== generation) {
      frames.length = 0;
      pending = 0;
    }
  }
}

async function handle(frame, mine) {
  if (!canSend()) {
    abandonMoment();
    preroll.length = 0;
    return;
  }
  const probability = await detector.probability(frame);
  if (mine !== generation || !canSend()) {
    return;
  }
  const held = hearing !== "gesture" || pressed;
  if (!speaking && !held) {
    // Nothing from before the press is sent.
    preroll.length = 0;
    loud = 0;
    return;
  }
  if (!speaking) {
    preroll.push(frame);
    while (preroll.length > PREROLL_FRAMES + START_FRAMES) {
      preroll.shift();
    }
    loud = probability >= SPEECH_ABOVE ? loud + 1 : 0;
    if (loud >= START_FRAMES && socket.bufferedAmount < MAX_BUFFERED_BYTES) {
      beginMoment();
    }
    return;
  }
  sendSound(frame);
  sentFrames += 1;
  quiet = probability < SPEECH_BELOW ? quiet + 1 : 0;
  if (quiet >= SILENCE_FRAMES || !held) {
    endMoment();
  } else if (sentFrames >= MAX_MOMENT_FRAMES) {
    // Long speech is cut here, and goes on as a moment of its own.
    sendJson({ type: "speech_end" });
    sendJson({ type: "speech_start" });
    sentFrames = 0;
  }
}

function beginMoment() {
  clearNotice();
  // Someone speaks while the voice plays: the sound is lowered at once, mechanically; whether
  // the voice stops is the house's judgement (`stop`, `resume`).
  if (speaker) {
    speaker.duck();
  }
  sendJson({ type: "speech_start" });
  const before = new Int16Array(preroll.length * FRAME_SAMPLES);
  preroll.forEach((frame, i) => before.set(frame, i * FRAME_SAMPLES));
  if (before.byteLength > 0) {
    sendBinary(before.buffer);
  }
  sentFrames = preroll.length;
  preroll.length = 0;
  quiet = 0;
  loud = 0;
  speaking = true;
  speechOn = true;
  render();
}

function endMoment() {
  sendJson({ type: "speech_end" });
  abandonMoment();
  detector.reset();
}

// A moment that stops without a word to the house (the socket went, the detector fell behind):
// the house drops what it was given when the socket closes.
function abandonMoment() {
  if (speaking && speaker) {
    speaker.speechEnded();
  }
  speaking = false;
  quiet = 0;
  sentFrames = 0;
  loud = 0;
  if (speechOn) {
    speechOn = false;
    render();
  }
}

// --- the socket ---------------------------------------------------------------------------------------

let socket = null;
let helloSent = false;
let attempt = 0;
let retry = null;
let openedAt = 0;
let lastClose = null;

function socketIsUp() {
  return socket !== null && socket.readyState === WebSocket.OPEN && helloSent;
}

function canSend() {
  return socketIsUp() && hearing !== "preparing" && hearing !== "unavailable";
}

function sendJson(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

function sendBinary(buffer) {
  if (socket && socket.readyState === WebSocket.OPEN && buffer.byteLength <= MAX_FRAME_BYTES) {
    socket.send(buffer);
    return true;
  }
  return false;
}

function sendSound(frame) {
  return sendBinary(frame.buffer.slice(frame.byteOffset, frame.byteOffset + FRAME_BYTES));
}

function socketAddress() {
  const address = new URL("/chat/voice", window.location.href);
  address.protocol = address.protocol === "https:" ? "wss:" : "ws:";
  return address.href;
}

function connect() {
  retry = null;
  if (!active || socket) {
    return;
  }
  const person = document.body.getAttribute("data-page-person");
  if (!person || !microphone) {
    setState("unavailable");
    return;
  }
  helloSent = false;
  hearing = null;
  const mine = generation;
  const opening = new WebSocket(socketAddress());
  opening.binaryType = "arraybuffer";
  socket = opening;
  opening.onopen = () => {
    if (mine !== generation || socket !== opening) {
      return;
    }
    openedAt = Date.now();
    // The id of the person this page was rendered for: the house closes the socket of a tab left
    // open for someone else (4403).
    opening.send(JSON.stringify({ type: "hello", microphone: microphone, person: person }));
    helloSent = true;
    refresh();
  };
  opening.onmessage = (event) => {
    if (socket === opening) {
      onSocketMessage(event.data);
    }
  };
  opening.onclose = (event) => {
    if (socket === opening) {
      onSocketClosed(event.code);
    }
  };
  opening.onerror = () => {
    // The close that follows says what happened.
  };
}

function closeSocket(code) {
  if (retry !== null) {
    window.clearTimeout(retry);
    retry = null;
  }
  const closing = socket;
  socket = null;
  openedAt = 0;
  helloSent = false;
  hearing = null;
  if (closing) {
    closing.onopen = closing.onmessage = closing.onclose = closing.onerror = null;
    try {
      closing.close(code);
    } catch (error) {
      // Already closed.
    }
  }
}

function onSocketClosed(code) {
  socket = null;
  helloSent = false;
  hearing = null;
  lastClose = code;
  if (speaker) {
    speaker.stop(); // the sound of a socket that is gone is not played on
  }
  abandonMoment();
  preroll.length = 0;
  if (!active) {
    return;
  }
  if (code === CLOSE_ANOTHER_PERSON) {
    // This tab was rendered for someone else: it is reloaded, never reconnected as it was.
    stopAll("reload");
    reloadOnce();
    return;
  }
  if (code === CLOSE_REPLACED) {
    // Another socket took this one's place: the voice moved. This page stops listening and
    // says so; the button takes the voice back.
    stopAll("moved");
    return;
  }
  // The back-off starts again only after a socket that really opened and lived; a failed
  // reconnect (which never opened) leaves it growing.
  const lived = openedAt !== 0 && Date.now() - openedAt >= STABLE_MS;
  openedAt = 0;
  if (lived) {
    attempt = 0;
  }
  const base =
    code === CLOSE_POLICY ? BACKOFF_LAST_MS : Math.min(BACKOFF_LAST_MS, BACKOFF_FIRST_MS * 2 ** attempt);
  attempt += 1;
  setState("unavailable");
  retry = window.setTimeout(connect, Math.round(base * (0.8 + Math.random() * 0.4)));
}

function reloadOnce() {
  try {
    const last = Number(window.sessionStorage.getItem(STORE_RELOAD) || 0);
    if (Date.now() - last > RELOAD_GAP_MS) {
      window.sessionStorage.setItem(STORE_RELOAD, String(Date.now()));
      window.location.reload();
    }
  } catch (error) {
    // No storage: the line says to reload, and the person does.
  }
}

function onSocketMessage(data) {
  if (typeof data !== "string") {
    callHook("onSound", data);
    return;
  }
  let message;
  try {
    message = JSON.parse(data);
  } catch (error) {
    return;
  }
  if (!message || typeof message !== "object") {
    return;
  }
  switch (message.type) {
    case "state":
      hearing = message.hearing;
      refresh();
      break;
    case "listening":
      if (message.on === false) {
        turnOff();
      }
      break;
    case "off":
      turnOff();
      break;
    case "not_taken":
      showNotice(NOT_TAKEN.get(message.reason) || "not_taken_elsewhere");
      break;
    case "failed":
      showNotice(message.reason === "no_key" || message.reason === "not_told" ? message.reason : "failed");
      break;
    default:
      // say, say_end, duck, stop, resume, speech_unavailable, and whatever comes later.
      callHook("onMessage", message);
  }
}

function callHook(name, value) {
  const hook = window.DomoVoice && window.DomoVoice[name];
  if (typeof hook === "function") {
    try {
      hook(value);
    } catch (error) {
      // A hook that fails never stops the voice.
    }
  }
}

// --- turning it on and off ------------------------------------------------------------------------------

async function turnOn() {
  if (!root || active) {
    return;
  }
  active = true;
  generation += 1;
  const mine = generation;
  attempt = 0;
  setState("preparing");
  try {
    if (!(await startCapture(mine))) {
      return;
    }
  } catch (error) {
    if (mine !== generation) {
      return;
    }
    const name = error && error.name;
    if (name === "NotFoundError" || name === "OverconstrainedError" || name === "NotReadableError") {
      stopAll("no_microphone");
    } else if (name === "NotAllowedError" || name === "SecurityError") {
      // The browser said no: the choice is not kept, the next visit does not ask again by itself.
      store(STORE_CHOICE, null);
      stopAll("refused");
    } else {
      stopAll("unavailable");
    }
    return;
  }
  store(STORE_CHOICE, "on");
  connect();
  refresh();
}

function turnOff() {
  if (socket && socket.readyState === WebSocket.OPEN && helloSent) {
    // The moment in progress ends, and what was cut is told, before the house hears `off`.
    if (speaking) {
      sendJson({ type: "speech_end" });
    }
    if (speaker) {
      speaker.stop();
    }
    sendJson({ type: "off" });
  }
  abandonMoment();
  store(STORE_CHOICE, null);
  stopAll("off");
}

// Held to talk, when the house hears only after a gesture.
function hold(on) {
  pressed = on;
}

if (talk) {
  talk.addEventListener("pointerdown", () => hold(true));
  for (const name of ["pointerup", "pointerleave", "pointercancel"]) {
    talk.addEventListener(name, () => hold(false));
  }
  talk.addEventListener("keydown", (event) => {
    if (event.key === " " || event.key === "Enter") {
      hold(true);
    }
  });
  talk.addEventListener("keyup", () => hold(false));
  talk.addEventListener("blur", () => hold(false));
}

const playback = playbackHooks(() => speaker, { send: sendJson, notify: showNotice });

window.DomoVoice = Object.assign(window.DomoVoice || {}, {
  state() {
    return {
      state: current,
      hearing: hearing,
      listening: current === "listening",
      speaking: speaking,
      microphone: microphone,
      socket: socket ? socket.readyState : null,
      lastClose: lastClose,
      attempt: attempt,
    };
  },
  turnOn,
  turnOff,
  // For scripts that talk to the house: a message of the page (`played`).
  send: sendJson,
  // The hooks the socket's messages and sound are routed to: the player's.
  onMessage: playback.onMessage,
  onSound: playback.onSound,
});

// At a later visit the page listens as it opens when the browser already granted the microphone.
function listenIfChosen() {
  if (active || stored(STORE_CHOICE) !== "on" || !navigator.permissions || !navigator.permissions.query) {
    return;
  }
  navigator.permissions
    .query({ name: "microphone" })
    .then((status) => {
      status.onchange = () => {
        if (status.state === "denied" && active) {
          stopAll("refused");
        }
      };
      if (status.state === "granted" && !active) {
        turnOn();
      }
    })
    .catch(() => {});
}

if (root) {
  toggle.addEventListener("click", () => {
    if (active) {
      turnOff();
    } else {
      turnOn();
    }
  });
  render();
  listenIfChosen();
  // Leaving the page lets go of the microphone, the audio and the socket at once; the stored
  // choice stays, so the next visit (or the page coming back from the browser's cache) listens
  // again.
  window.addEventListener("pagehide", () => {
    if (active) {
      stopAll("off");
    }
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      listenIfChosen();
    }
  });
  // A hidden tab keeps listening (the choice of the person): the line is read again from the facts
  // when the tab shows, since a browser may have suspended the audio meanwhile.
  document.addEventListener("visibilitychange", refresh);
}
