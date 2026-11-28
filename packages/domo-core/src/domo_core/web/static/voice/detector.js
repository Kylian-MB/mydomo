/* The speech detector of the voice page: the Silero VAD model (voice/silero_vad.onnx) run by ONNX
   Runtime Web (voice/ort/), single-threaded, in the page: no worker, no SharedArrayBuffer. The
   runtime and the model are served from our own address; nothing is fetched from elsewhere.

   The model reads 512 samples of 16 kHz sound (32 ms) preceded by the last 64 samples of the
   frame before, with a recurrent state it hands back, and answers the probability that the frame
   holds speech. The runtime is loaded on the first `createDetector`, not when the page opens. */

const RATE = 16000;
const FRAME = 512;
const CONTEXT = 64;
const STATE_SIZE = 2 * 128;

export async function createDetector(base = import.meta.url) {
  const ort = await import(new URL("ort/ort.wasm.min.mjs", base).href);
  // One thread and no proxy worker: nothing needs cross-origin isolation or a worker-src.
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;
  ort.env.wasm.wasmPaths = {
    mjs: new URL("ort/ort-wasm-simd-threaded.mjs", base).href,
    wasm: new URL("ort/ort-wasm-simd-threaded.wasm", base).href,
  };
  const session = await ort.InferenceSession.create(new URL("silero_vad.onnx", base).href, {
    executionProviders: ["wasm"],
  });

  const rate = new ort.Tensor("int64", BigInt64Array.from([BigInt(RATE)]), []);
  const input = new Float32Array(CONTEXT + FRAME);
  let state = new Float32Array(STATE_SIZE);

  function reset() {
    state = new Float32Array(STATE_SIZE);
    input.fill(0);
  }

  // `frame`: an Int16Array of 512 samples. Resolves to the probability of speech, 0 to 1.
  async function probability(frame) {
    input.copyWithin(0, FRAME, FRAME + CONTEXT);
    for (let i = 0; i < FRAME; i++) {
      input[CONTEXT + i] = frame[i] / 32768;
    }
    // `input` is rewritten only after this call returned, so the tensor may share its memory.
    const out = await session.run({
      input: new ort.Tensor("float32", input, [1, CONTEXT + FRAME]),
      state: new ort.Tensor("float32", state, [2, 1, 128]),
      sr: rate,
    });
    state = Float32Array.from(out.stateN.data);
    return out.output.data[0];
  }

  return { probability, reset, frame: FRAME, rate: RATE };
}
