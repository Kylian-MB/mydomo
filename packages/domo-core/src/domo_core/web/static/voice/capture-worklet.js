/* The capture worklet of the voice: it takes the microphone's sound at the rate of the device and
   hands it to the page as 16 000 Hz, mono, 16-bit samples, in frames of 512 samples (32 ms, the
   size the speech detector reads). It runs on the audio thread: no allocation beyond the frame.

   The resampling averages the input samples that fall in each output sample (a box filter, the
   fraction of a sample at the border split between its two neighbours), which also keeps what
   is above 8 kHz from folding into the speech band. */

const TARGET_RATE = 16000;
const FRAME = 512;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Input samples that make one output sample (`sampleRate` is the device's, a worklet global).
    this.ratio = sampleRate / TARGET_RATE;
    this.sum = 0;
    // How much of the current output sample is already filled, in input samples.
    this.filled = 0;
    this.frame = new Int16Array(FRAME);
    this.length = 0;
  }

  push(value) {
    const clipped = value > 1 ? 1 : value < -1 ? -1 : value;
    this.frame[this.length++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
    if (this.length === FRAME) {
      const done = this.frame;
      this.frame = new Int16Array(FRAME);
      this.length = 0;
      this.port.postMessage(done, [done.buffer]);
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) {
      return true;
    }
    for (let i = 0; i < channel.length; i++) {
      const x = channel[i];
      const room = this.ratio - this.filled;
      if (room > 1) {
        this.sum += x;
        this.filled += 1;
      } else {
        this.sum += x * room;
        this.push(this.sum / this.ratio);
        this.sum = x * (1 - room);
        this.filled = 1 - room;
      }
    }
    return true;
  }
}

registerProcessor("domo-capture", CaptureProcessor);
