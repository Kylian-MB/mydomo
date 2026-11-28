/* The playing worklet of the voice: the assistant's sound is queued here (as the page gets it, any
   rate) and played at the rate of the audio context, resampled by a straight line between two
   neighbouring samples (speech of 24 000 Hz, played at 44 100 or 48 000 Hz). It runs on the audio
   thread; the page only posts messages to it:
     {type: "push", samples: Float32Array, rate, reply}   sound to play after what is queued
                                                   (refused when the queue is full: it posts
                                                   {type: "overflow", reply} back)
     {type: "mark", id}                            posted back as `reached` once everything
                                                   queued before it was played
     {type: "clear"}                               drop everything queued (marks included)
   and it posts {type: "reached", ids: [...]} back.

   `PlaybackQueue` is the whole logic and touches no audio object: it is exported so that it can
   be run (and tested) outside a browser; the processor is registered only where there is an
   audio worklet scope. */

// The queue refuses sound past this much (the assistant says sentences, not hours).
const MAX_QUEUED_SECONDS = 120;

export class PlaybackQueue {
  constructor(outputRate) {
    this.outputRate = outputRate;
    // Chunks {samples, rate, pos} and marks {mark}, in the order they were given.
    this.items = [];
  }

  // The seconds of sound waiting.
  queued() {
    let seconds = 0;
    for (const item of this.items) {
      if (item.samples) {
        seconds += item.samples.length / item.rate;
      }
    }
    return seconds;
  }

  push(samples, rate) {
    if (!(rate > 0) || samples.length === 0) {
      return false;
    }
    if (this.queued() + samples.length / rate > MAX_QUEUED_SECONDS) {
      return false;
    }
    this.items.push({ samples: samples, rate: rate, pos: 0 });
    return true;
  }

  mark(id) {
    this.items.push({ mark: id });
  }

  clear() {
    this.items.length = 0;
  }

  // Fills `out` with the next samples (silence when nothing is queued); returns the ids of the
  // marks the playing passed on the way.
  read(out) {
    const reached = [];
    let n = 0;
    while (n < out.length) {
      const head = this.items[0];
      if (head === undefined) {
        break;
      }
      if (head.mark !== undefined) {
        reached.push(head.mark);
        this.items.shift();
        continue;
      }
      const s = head.samples;
      const step = head.rate / this.outputRate;
      while (n < out.length) {
        const i = Math.floor(head.pos);
        if (i >= s.length) {
          break;
        }
        const a = s[i];
        let b = a;
        if (i + 1 < s.length) {
          b = s[i + 1];
        } else {
          const next = this.items[1];
          if (next !== undefined && next.samples !== undefined) {
            b = next.samples[0];
          }
        }
        out[n++] = a + (b - a) * (head.pos - i);
        head.pos += step;
      }
      if (Math.floor(head.pos) >= s.length) {
        const carry = head.pos - s.length;
        this.items.shift();
        const next = this.items[0];
        if (next !== undefined && next.samples !== undefined) {
          next.pos = carry;
        }
      }
    }
    out.fill(0, n);
    return reached;
  }
}

if (typeof registerProcessor === "function") {
  class PlayerProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      // `sampleRate` is the rate of the context, a global of the worklet scope.
      this.queue = new PlaybackQueue(sampleRate);
      this.port.onmessage = (event) => {
        const message = event.data;
        if (message.type === "push") {
          if (!this.queue.push(message.samples, message.rate)) {
            this.port.postMessage({ type: "overflow", reply: message.reply });
          }
        } else if (message.type === "mark") {
          this.queue.mark(message.id);
        } else if (message.type === "clear") {
          this.queue.clear();
        }
      };
    }

    process(inputs, outputs) {
      const channels = outputs[0];
      const reached = this.queue.read(channels[0]);
      for (let c = 1; c < channels.length; c++) {
        channels[c].set(channels[0]);
      }
      if (reached.length > 0) {
        this.port.postMessage({ type: "reached", ids: reached });
      }
      return true;
    }
  }
  registerProcessor("domo-player", PlayerProcessor);
}
