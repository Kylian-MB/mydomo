/* The player of the voice: the assistant's sound is played as it arrives, lowered at once when
   someone starts to speak, stopped net, raised again, and the house is told what was played.

   The sound comes over the voice socket as sentences of 16-bit mono PCM (`say`, binary frames,
   `say_end`); each frame is queued, without waiting for the sentence to end, into a worklet
   (player-worklet.js) that plays through one gain node. The audio context is the one the
   microphone is read through: the browser's echo cancellation takes out of the microphone what
   this context plays.

   What the page does by itself is mechanical: it LOWERS the gain when its own detector says
   someone began to speak (`duck`), and goes on playing. Whether the assistant falls silent is
   the house's judgement: `stop` silences, `resume` raises the gain. If the house says neither
   within DUCK_BACK_MS after the speech ended, the gain comes back by itself.

   `played` is sent once per reply: `whole: true` when everything the page was given of it was
   played (the house sends no "reply over" message: a reply is over when its last sentence was
   played and no other came for REPLY_GRACE_MS, or when another reply begins), `whole: false`
   when it was cut (`stop`, the page turning the voice off, the socket going) or was not all said.

   No sound is kept anywhere but in the queue of the worklet, and nothing of it is logged. No
   sentence is written here: what the person reads is named (`notify`) and written by the page. */

const PLAYER_WORKLET = new URL("./player-worklet.js", import.meta.url).href;

// The gain while someone speaks over the voice, and the time it takes to get there.
const DUCK_LEVEL = 0.25;
const DUCK_MS = 80;
// Stop: the gain goes to nothing in this time, then the queue is dropped.
const STOP_FADE_MS = 30;
// Resume: the gain goes back to full in this time.
const RESUME_MS = 150;
// A lowered voice that was told neither to stop nor to resume rises by itself this long after
// the person stopped speaking.
const DUCK_BACK_MS = 1500;
// The same, when only the house lowered the voice and said nothing after: longer, the house may
// still be judging.
const HOUSE_DUCK_BACK_MS = 5000;
// A reply is taken as over this long after its last sentence was played and nothing came after.
const REPLY_GRACE_MS = 2000;

const MIN_RATE = 8000;
const MAX_RATE = 96000;
const REMEMBERED = 64; // replies whose id is kept to ignore late sound and to report only once

function remember(set, value) {
  set.add(value);
  if (set.size > REMEMBERED) {
    set.delete(set.values().next().value);
  }
}

// options: send(message) -> bool, onPlaying(bool), notify(name), timing (the times above, for
// tests).
export function createPlayer(context, options = {}) {
  const send = options.send || (() => false);
  const onPlaying = options.onPlaying || (() => {});
  const notify = options.notify || (() => {});
  const timing = Object.assign(
    {
      duckMs: DUCK_MS,
      stopFadeMs: STOP_FADE_MS,
      resumeMs: RESUME_MS,
      duckBackMs: DUCK_BACK_MS,
      houseDuckBackMs: HOUSE_DUCK_BACK_MS,
      graceMs: REPLY_GRACE_MS,
    },
    options.timing,
  );

  const gain = context.createGain();
  gain.connect(context.destination);
  let node = null;

  const replies = []; // replies not reported yet, in the order they began
  const ignored = new Set(); // replies cut or not played: their late sound is dropped
  const reported = new Set(); // replies `played` was sent for
  const marks = new Map(); // mark id -> reply
  let markCounter = 0;
  let receiving = null; // the reply whose sentence is coming now
  let rate = 0;
  let leftover = null; // an odd byte at the end of a frame, the start of the next sample
  let ducked = false;
  let duckTimer = null;
  let stopTimer = null; // while the queue of a stopped voice is still fading out
  let gainClosed = false; // the gain was taken to nothing by a stop
  let playingNow = false; // sound is pending or arriving (not the grace after the last sentence)
  let stoppedIds = []; // replies a stop cut: a resume lets their later sentences play
  let personSpeaking = false; // the page's detector hears someone now

  function refreshPlaying() {
    const now = replies.some((reply) => reply.pending > 0 || reply.receiving);
    if (now !== playingNow) {
      playingNow = now;
      onPlaying(now);
    }
  }

  function ramp(target, ms) {
    const param = gain.gain;
    const at = context.currentTime;
    param.cancelScheduledValues(at);
    param.setValueAtTime(param.value, at);
    param.linearRampToValueAtTime(target, at + ms / 1000);
  }

  function report(reply, whole) {
    const at = replies.indexOf(reply);
    if (at >= 0) {
      replies.splice(at, 1);
    }
    clearTimeout(reply.timer);
    if (replies.length === 0 && ducked && !personSpeaking) {
      // Nothing left to lower: the next reply starts at full level.
      lift();
      if (!gainClosed) {
        ramp(1, timing.resumeMs);
      }
    }
    if (!reported.has(reply.id)) {
      remember(reported, reply.id);
      send({ type: "played", reply: reply.id, whole: whole });
    }
    refreshPlaying();
  }

  function isWhole(reply) {
    return reply.pending === 0 && reply.receiving === false && !reply.incomplete;
  }

  // A reply whose sound was all played is reported at once when another reply came after it, and
  // after a short wait when it is the newest (a sentence of it may still be on its way).
  function check(reply) {
    clearTimeout(reply.timer);
    if (reply.pending > 0 || reply.receiving) {
      return;
    }
    refreshPlaying();
    if (replies[replies.length - 1] !== reply) {
      report(reply, !reply.incomplete);
    } else {
      reply.timer = setTimeout(() => report(reply, !reply.incomplete), timing.graceMs);
    }
  }

  function lift() {
    ducked = false;
    clearTimeout(duckTimer);
    duckTimer = null;
  }

  // The queue of a stopped voice is dropped once the gain reached nothing.
  function flush() {
    clearTimeout(stopTimer);
    stopTimer = null;
    if (node) {
      node.port.postMessage({ type: "clear" });
    }
    marks.clear();
  }

  function onNode(event) {
    const message = event.data;
    if (message && message.type === "overflow") {
      // The worklet refused sound (its queue is full): what was said of that reply is not whole.
      const reply = replies.find((r) => r.id === message.reply);
      if (reply) {
        reply.incomplete = true;
      }
    } else if (message && message.type === "reached") {
      for (const id of message.ids) {
        const reply = marks.get(id);
        marks.delete(id);
        if (reply) {
          reply.pending -= 1;
          check(reply);
        }
      }
    }
  }

  const ready = (async () => {
    await context.audioWorklet.addModule(PLAYER_WORKLET);
    const made = new AudioWorkletNode(context, "domo-player", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    made.port.onmessage = onNode;
    made.connect(gain);
    node = made;
  })();

  function drop(id) {
    remember(ignored, id);
    if (!reported.has(id)) {
      remember(reported, id);
      send({ type: "played", reply: id, whole: false });
    }
    notify("cannot_play");
  }

  const player = {
    ready: ready,

    // The sound of a sentence of `id` is coming, at `sampleRate`. False when it will not be
    // played (the reply is then told as not played).
    begin(id, sampleRate) {
      receiving = null;
      leftover = null;
      if (!node || ignored.has(id)) {
        return false;
      }
      if (!(sampleRate >= MIN_RATE && sampleRate <= MAX_RATE) || context.state !== "running") {
        if (context.state === "suspended") {
          context.resume().catch(() => {});
        }
        drop(id);
        return false;
      }
      if (stopTimer !== null) {
        flush();
      }
      if (gainClosed) {
        gainClosed = false;
        const at = context.currentTime;
        gain.gain.cancelScheduledValues(at);
        gain.gain.setValueAtTime(ducked ? DUCK_LEVEL : 1, at);
      }
      let reply = replies.find((r) => r.id === id);
      if (!reply) {
        const before = replies[replies.length - 1];
        reply = { id: id, pending: 0, receiving: false, incomplete: false, timer: null };
        replies.push(reply);
        if (before) {
          check(before);
        }
      }
      clearTimeout(reply.timer);
      reply.receiving = true;
      receiving = reply;
      rate = sampleRate;
      if (personSpeaking && !ducked) {
        // Someone is already speaking: the voice starts lowered.
        ducked = true;
        ramp(DUCK_LEVEL, timing.duckMs);
      }
      refreshPlaying();
      return true;
    },

    // A frame of the sentence: 16-bit little-endian samples, one may be split between frames.
    push(bytes) {
      if (!node || !receiving || !(bytes instanceof ArrayBuffer)) {
        return;
      }
      let view = new Uint8Array(bytes);
      if (leftover !== null) {
        const joined = new Uint8Array(view.length + 1);
        joined[0] = leftover;
        joined.set(view, 1);
        view = joined;
        leftover = null;
      }
      const count = view.length >> 1;
      if (view.length & 1) {
        leftover = view[view.length - 1];
      }
      if (count === 0) {
        return;
      }
      const data = new DataView(view.buffer, view.byteOffset, count * 2);
      const samples = new Float32Array(count);
      for (let i = 0; i < count; i++) {
        samples[i] = data.getInt16(i * 2, true) / 32768;
      }
      node.port.postMessage({ type: "push", samples: samples, rate: rate, reply: receiving.id }, [
        samples.buffer,
      ]);
    },

    // The sentence is over.
    end(id) {
      const reply = receiving;
      receiving = null;
      leftover = null;
      if (!node || !reply || reply.id !== id) {
        return;
      }
      reply.receiving = false;
      reply.pending += 1;
      refreshPlaying();
      markCounter += 1;
      marks.set(markCounter, reply);
      node.port.postMessage({ type: "mark", id: markCounter });
    },

    // The rest of the reply cannot be said: what was played of it is not the whole.
    unsaid() {
      replies.forEach((reply) => {
        reply.incomplete = true;
      });
    },

    // The page's own detector heard someone begin to speak: lower the sound, go on playing.
    duck() {
      personSpeaking = true;
      if (!node || replies.length === 0 || gainClosed) {
        return;
      }
      ducked = true;
      clearTimeout(duckTimer);
      duckTimer = null;
      ramp(DUCK_LEVEL, timing.duckMs);
    },

    // The person stopped speaking: the house has a short time to say stop or resume.
    speechEnded() {
      personSpeaking = false;
      if (!ducked) {
        return;
      }
      clearTimeout(duckTimer);
      duckTimer = setTimeout(() => {
        duckTimer = null;
        if (ducked) {
          lift();
          ramp(1, timing.resumeMs);
        }
      }, timing.duckBackMs);
    },

    // The house says someone speaks over the voice: lowers it if it is not already, and leaves a
    // pending rise of the page alone (the page's own `duck` is the one that restarts it).
    houseDuck() {
      if (!node || ducked || replies.length === 0 || gainClosed) {
        return;
      }
      ducked = true;
      ramp(DUCK_LEVEL, timing.duckMs);
      clearTimeout(duckTimer);
      duckTimer = setTimeout(() => {
        duckTimer = null;
        if (ducked) {
          lift();
          ramp(1, timing.resumeMs);
        }
      }, timing.houseDuckBackMs);
    },

    // Silence at once and drop what is queued; the replies cut are told as such.
    stop() {
      lift();
      personSpeaking = false;
      receiving = null;
      leftover = null;
      if (!node) {
        return;
      }
      const cut = replies.slice();
      cut.forEach((reply) => {
        remember(ignored, reply.id);
        stoppedIds.push(reply.id);
        if (stoppedIds.length > REMEMBERED) {
          stoppedIds.shift();
        }
        report(reply, isWhole(reply));
      });
      if (cut.length === 0 && !gainClosed) {
        ramp(1, timing.resumeMs);
        return;
      }
      gainClosed = true;
      ramp(0, timing.stopFadeMs);
      clearTimeout(stopTimer);
      stopTimer = setTimeout(flush, timing.stopFadeMs);
    },

    // Raise the sound again.
    resume() {
      lift();
      if (!node) {
        return;
      }
      // A reply that was stopped goes on if the house says so: its later sentences play.
      if (stoppedIds.length > 0) {
        if (stopTimer !== null) {
          flush();
        }
        stoppedIds.forEach((id) => ignored.delete(id));
        stoppedIds = [];
      }
      gainClosed = false;
      ramp(1, timing.resumeMs);
    },

    playing() {
      return replies.length > 0;
    },

    // The context is going: nothing stays.
    close() {
      player.stop();
      flush();
      if (node) {
        node.port.onmessage = null;
        node.disconnect();
        node = null;
      }
      gain.disconnect();
    },
  };
  return player;
}

// What the page does with what the house sends: `onMessage` and `onSound` are the hooks of
// `window.DomoVoice`. `current()` gives the player (null when this browser could not make one:
// then no reply is played, and the house is told so).
export function playbackHooks(current, options = {}) {
  const send = options.send || (() => false);
  const notify = options.notify || (() => {});
  const unplayed = new Set();
  return {
    onMessage(message) {
      const player = current();
      switch (message.type) {
        case "say":
          if (typeof message.reply !== "string") {
            return;
          }
          if (player) {
            player.begin(message.reply, message.rate);
          } else if (!unplayed.has(message.reply)) {
            remember(unplayed, message.reply);
            send({ type: "played", reply: message.reply, whole: false });
            notify("cannot_play");
          }
          break;
        case "say_end":
          if (player && typeof message.reply === "string") {
            player.end(message.reply);
          }
          break;
        case "duck":
          if (player) {
            player.houseDuck();
          }
          break;
        case "stop":
          if (player) {
            player.stop();
          }
          break;
        case "resume":
          if (player) {
            player.resume();
          }
          break;
        case "speech_unavailable":
          if (player) {
            player.unsaid();
          }
          notify(unavailableName(message.reason));
          break;
        default:
          break;
      }
    },
    onSound(data) {
      const player = current();
      if (player) {
        player.push(data);
      }
    },
  };
}

// The text of a reason the voice cannot speak: a name of the page's data-text- attributes.
function unavailableName(reason) {
  if (reason === "preparing") {
    return "cannot_speak_preparing";
  }
  if (reason === "limit_reached") {
    return "cannot_speak_limit";
  }
  if (reason === "no_model") {
    return "cannot_speak_no_model";
  }
  return "cannot_speak";
}
