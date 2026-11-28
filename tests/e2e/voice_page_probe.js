// Added to every page of the voice trials (tests/e2e/test_voice_stack.py) before its own scripts:
// it only watches, on the page's own clock (performance.now(), milliseconds), what the voice
// socket sends and receives, and what the page really plays. Nothing it records leaves the page
// but through the test, which reads window.__voiceProbe.
(() => {
  const probe = { sent: [], received: [], loud: [], contexts: [] };
  window.__voiceProbe = probe;

  const Original = window.WebSocket;
  function Watched(...args) {
    const socket = new Original(...args);
    socket.addEventListener("message", (event) => {
      const at = performance.now();
      if (typeof event.data === "string") {
        probe.received.push([at, event.data]);
      } else {
        const size = event.data.byteLength !== undefined ? event.data.byteLength : event.data.size;
        probe.received.push([at, "binary:" + size]);
      }
    });
    socket.addEventListener("close", (event) => {
      probe.received.push([performance.now(), JSON.stringify({ type: "__closed", code: event.code })]);
    });
    return socket;
  }
  Watched.prototype = Original.prototype;
  Object.setPrototypeOf(Watched, Original);
  window.WebSocket = Watched;

  const send = Original.prototype.send;
  Original.prototype.send = function (data) {
    const at = performance.now();
    if (typeof data === "string") {
      probe.sent.push([at, data]);
    } else {
      // The sound frames are counted, never kept.
      const size = data.byteLength !== undefined ? data.byteLength : data.size;
      probe.sent.push([at, "binary:" + size]);
    }
    return send.call(this, data);
  };

  // What reaches the speakers: every node connected to a context's destination is also connected
  // to one analyser of that context, read every 5 ms. A suspended context plays (and records)
  // nothing.
  const connect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (target, ...rest) {
    const result = connect.call(this, target, ...rest);
    if (target instanceof AudioDestinationNode) {
      const context = this.context;
      if (!context.__voiceTap) {
        const analyser = context.createAnalyser();
        analyser.fftSize = 256;
        const silent = context.createGain();
        silent.gain.value = 0;
        connect.call(analyser, silent);
        connect.call(silent, context.destination);
        context.__voiceTap = analyser;
        const index = probe.contexts.length;
        probe.contexts.push(context.state);
        context.addEventListener("statechange", () => {
          probe.contexts[index] = context.state;
        });
        const buffer = new Float32Array(analyser.fftSize);
        setInterval(() => {
          analyser.getFloatTimeDomainData(buffer);
          let peak = 0;
          for (const value of buffer) {
            peak = Math.max(peak, Math.abs(value));
          }
          if (peak > 0.01) {
            probe.loud.push([performance.now(), Math.round(peak * 1000) / 1000]);
          }
        }, 5);
      }
      connect.call(this, context.__voiceTap);
    }
    return result;
  };
})();
