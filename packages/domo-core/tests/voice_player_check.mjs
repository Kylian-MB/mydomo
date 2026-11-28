// Runs the player of the voice (static/voice/player.js, player-worklet.js) in Node, against a
// fake audio context: `node voice_player_check.mjs <player.mjs> <player-worklet.mjs> <scenario>`.
// A scenario that fails throws (exit code 1); one that holds exits 0. Used by
// test_voice_player_files.py. No browser is involved: what a real browser does with the sound is
// for the browser trials.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [, , playerPath, workletPath, scenario] = process.argv;
const { createPlayer, playbackHooks } = await import(pathToFileURL(playerPath).href);
const { PlaybackQueue } = await import(pathToFileURL(workletPath).href);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const TIMING = { duckMs: 80, stopFadeMs: 20, resumeMs: 150, duckBackMs: 40, houseDuckBackMs: 80, graceMs: 30 };

class Param {
  constructor() {
    this.value = 1;
    this.ramps = []; // [target, seconds it takes]
    this.now = 0;
  }
  cancelScheduledValues() {}
  setValueAtTime(value, at) {
    this.value = value;
    this.now = at;
  }
  linearRampToValueAtTime(target, at) {
    this.ramps.push([target, Number((at - this.now).toFixed(3))]);
  }
}

const nodes = [];
globalThis.AudioWorkletNode = class {
  constructor() {
    this.port = {
      posted: [],
      onmessage: null,
      postMessage(message) {
        this.posted.push(message);
      },
    };
    nodes.push(this);
  }
  connect() {}
  disconnect() {}
};

function setup(overrides = {}) {
  const gain = { gain: new Param(), connect() {}, disconnect() {} };
  const context = {
    currentTime: 0,
    state: "running",
    destination: {},
    createGain: () => gain,
    audioWorklet: { addModule: async () => {} },
    resume: async () => {},
  };
  Object.assign(context, overrides);
  const sent = [];
  const notes = [];
  const playing = [];
  const player = createPlayer(context, {
    send: (message) => sent.push(message),
    notify: (name) => notes.push(name),
    onPlaying: (on) => playing.push(on),
    timing: TIMING,
  });
  return { context, gain: gain.gain, player, sent, notes, playing, node: () => nodes[nodes.length - 1] };
}

function pcm(values) {
  const bytes = new Uint8Array(values.length * 2);
  const view = new DataView(bytes.buffer);
  values.forEach((v, i) => view.setInt16(i * 2, v, true));
  return bytes.buffer;
}

const kinds = (node, type) => node.port.posted.filter((m) => m.type === type);

const scenarios = {
  queue_resamples_and_reports_marks() {
    const queue = new PlaybackQueue(48000);
    assert.equal(queue.push(new Float32Array(100).fill(0.5), 24000), true);
    queue.mark(7);
    const first = new Float32Array(150);
    assert.deepEqual(queue.read(first), []);
    assert.ok(first.every((v) => v === 0.5));
    const second = new Float32Array(150);
    assert.deepEqual(queue.read(second), [7]);
    // 100 samples at 24 kHz are 200 at 48 kHz: 50 more, then silence.
    assert.equal(second.slice(0, 50).every((v) => v === 0.5), true);
    assert.equal(second.slice(50).every((v) => v === 0), true);
  },

  queue_interpolates_and_carries_across_chunks() {
    const queue = new PlaybackQueue(48000);
    queue.push(Float32Array.of(0, 1), 24000);
    queue.push(Float32Array.of(2, 3), 24000);
    const out = new Float32Array(8);
    queue.read(out);
    assert.deepEqual(Array.from(out), [0, 0.5, 1, 1.5, 2, 2.5, 3, 3]);
  },

  queue_clear_and_cap() {
    const queue = new PlaybackQueue(48000);
    queue.push(Float32Array.of(1, 1, 1), 24000);
    queue.mark(1);
    queue.clear();
    const out = new Float32Array(4).fill(9);
    assert.deepEqual(queue.read(out), []);
    assert.ok(out.every((v) => v === 0));
    assert.equal(queue.push(new Float32Array(24000 * 121), 24000), false);
    assert.equal(queue.push(new Float32Array(0), 24000), false);
    assert.equal(queue.push(Float32Array.of(1), 0), false);
  },

  async played_whole_once_after_the_last_sentence_was_played() {
    const t = setup();
    await t.player.ready;
    assert.equal(t.player.begin("r1", 24000), true);
    assert.equal(t.player.playing(), true);
    t.player.push(pcm([16384, -16384]));
    t.player.end("r1");
    const pushed = kinds(t.node(), "push");
    assert.equal(pushed.length, 1);
    assert.equal(pushed[0].rate, 24000);
    assert.deepEqual(Array.from(pushed[0].samples), [0.5, -0.5]);
    const mark = kinds(t.node(), "mark")[0];
    assert.deepEqual(t.sent, []);
    t.node().port.onmessage({ data: { type: "reached", ids: [mark.id] } });
    assert.deepEqual(t.sent, []); // a sentence may still come
    await sleep(60);
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: true }]);
    assert.equal(t.player.playing(), false);
    assert.deepEqual(t.playing, [true, false]);
  },

  async a_second_sentence_keeps_the_reply_open() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.end("r1");
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    await sleep(10);
    t.player.begin("r1", 24000);
    t.player.push(pcm([2]));
    t.player.end("r1");
    await sleep(50);
    assert.deepEqual(t.sent, []); // the second sentence was not played yet
    t.node().port.onmessage({ data: { type: "reached", ids: [2] } });
    await sleep(60);
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: true }]);
  },

  async a_reply_that_began_after_is_told_at_once_when_the_first_was_played() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.end("r1");
    t.player.begin("r2", 24000);
    t.player.push(pcm([2]));
    t.player.end("r2");
    assert.deepEqual(t.sent, []);
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: true }]);
  },

  async a_split_sample_is_joined() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    const whole = new Uint8Array(pcm([1000, 2000]));
    t.player.push(whole.slice(0, 3).buffer);
    t.player.push(whole.slice(3).buffer);
    const samples = kinds(t.node(), "push").flatMap((m) => Array.from(m.samples));
    assert.deepEqual(samples, [1000 / 32768, 2000 / 32768]);
  },

  async duck_lowers_while_playing_and_only_then() {
    const t = setup();
    await t.player.ready;
    t.player.duck();
    assert.deepEqual(t.gain.ramps, []); // nothing plays: nothing to lower
    t.player.speechEnded();
    t.player.begin("r1", 24000);
    t.player.push(pcm([1, 2]));
    t.player.duck();
    assert.deepEqual(t.gain.ramps, [[0.25, 0.08]]);
    // Nothing else came from the house: back up, by itself, after the speech ended.
    t.player.speechEnded();
    await sleep(15);
    assert.equal(t.gain.ramps.length, 1);
    await sleep(60);
    assert.deepEqual(t.gain.ramps[1], [1, 0.15]);
    assert.deepEqual(kinds(t.node(), "clear"), []); // it never stopped playing
  },

  async speaking_again_cancels_the_rise() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.duck();
    t.player.speechEnded();
    await sleep(15);
    t.player.duck();
    await sleep(60);
    assert.deepEqual(t.gain.ramps, [[0.25, 0.08], [0.25, 0.08]]);
  },

  async the_house_resume_raises_the_sound_and_cancels_the_rise() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.duck();
    t.player.speechEnded();
    t.player.resume();
    assert.deepEqual(t.gain.ramps[1], [1, 0.15]);
    await sleep(70);
    assert.equal(t.gain.ramps.length, 2);
  },

  async stop_silences_drops_the_queue_and_tells_the_cut() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1, 2, 3]));
    t.player.duck();
    t.player.stop();
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
    assert.deepEqual(t.gain.ramps[t.gain.ramps.length - 1], [0, 0.02]);
    assert.equal(t.player.playing(), false);
    assert.deepEqual(kinds(t.node(), "clear"), []);
    await sleep(40);
    assert.equal(kinds(t.node(), "clear").length, 1);
    // What the stopped reply still sends is not played, and not told twice.
    const before = t.node().port.posted.length;
    t.player.push(pcm([4]));
    assert.equal(t.player.begin("r1", 24000), false);
    t.player.push(pcm([5]));
    t.player.end("r1");
    assert.equal(t.node().port.posted.length, before);
    t.player.stop();
    assert.equal(t.sent.length, 1);
    // The next reply plays at full sound.
    assert.equal(t.player.begin("r2", 24000), true);
    t.player.push(pcm([6]));
    assert.equal(t.gain.value, 1);
    assert.equal(kinds(t.node(), "push").length, 2);
  },

  async a_reply_fully_played_but_waiting_is_whole_when_stopped() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.end("r1");
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    t.player.stop();
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: true }]);
    await sleep(60);
    assert.equal(t.sent.length, 1);
  },

  async a_sentence_cut_in_the_middle_is_not_whole() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.stop(); // the page turning the voice off, or the socket going
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
  },

  async a_voice_that_could_not_say_the_rest_is_not_whole() {
    const t = setup();
    await t.player.ready;
    const hooks = playbackHooks(() => t.player, { send: (m) => t.sent.push(m), notify: (n) => t.notes.push(n) });
    hooks.onMessage({ type: "say", reply: "r1", index: 0, rate: 24000 });
    hooks.onSound(pcm([1]));
    hooks.onMessage({ type: "say_end", reply: "r1", index: 0 });
    hooks.onMessage({ type: "speech_unavailable", reason: "limit_reached" });
    hooks.onMessage({ type: "speech_unavailable", reason: "surprise" });
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    await sleep(60);
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
    assert.deepEqual(t.notes, ["cannot_speak_limit", "cannot_speak"]);
  },

  async a_context_that_does_not_run_plays_nothing_and_says_so() {
    const t = setup({ state: "suspended" });
    await t.player.ready;
    assert.equal(t.player.begin("r1", 24000), false);
    t.player.push(pcm([1]));
    assert.deepEqual(kinds(t.node(), "push"), []);
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
    assert.deepEqual(t.notes, ["cannot_play"]);
    t.player.begin("r1", 24000);
    assert.equal(t.sent.length, 1);
  },

  async a_bad_rate_is_not_played() {
    const t = setup();
    await t.player.ready;
    assert.equal(t.player.begin("r1", 5), false);
    assert.equal(t.player.begin("r2", "fast"), false);
    assert.equal(t.sent.length, 2);
  },

  async without_a_player_the_house_is_told_once_per_reply() {
    const sent = [];
    const notes = [];
    const hooks = playbackHooks(() => null, { send: (m) => sent.push(m), notify: (n) => notes.push(n) });
    hooks.onMessage({ type: "say", reply: "r1", index: 0, rate: 24000 });
    hooks.onMessage({ type: "say", reply: "r1", index: 1, rate: 24000 });
    hooks.onSound(pcm([1]));
    hooks.onMessage({ type: "stop" });
    assert.deepEqual(sent, [{ type: "played", reply: "r1", whole: false }]);
    assert.deepEqual(notes, ["cannot_play"]);
  },

  async the_houses_duck_after_a_short_utterance_leaves_the_rise_alone() {
    const t = setup();
    await t.player.ready;
    const hooks = playbackHooks(() => t.player, { send: () => true, notify: () => {} });
    hooks.onMessage({ type: "say", reply: "r1", index: 0, rate: 24000 });
    t.player.duck(); // the page's detector
    t.player.speechEnded(); // a short utterance: the rise is pending
    hooks.onMessage({ type: "duck" }); // the house's, late
    assert.equal(t.gain.ramps.length, 1); // no second ramp
    await sleep(80);
    assert.deepEqual(t.gain.ramps[1], [1, 0.15]); // the rise still came
  },

  async the_houses_duck_alone_lowers_once() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.houseDuck();
    t.player.houseDuck();
    assert.deepEqual(t.gain.ramps, [[0.25, 0.08]]);
  },

  async a_house_duck_with_nothing_after_it_rises_by_itself() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.houseDuck();
    await sleep(40);
    assert.equal(t.gain.ramps.length, 1);
    await sleep(70);
    assert.deepEqual(t.gain.ramps[1], [1, 0.15]);
  },

  async a_reply_after_a_house_duck_starts_at_full_level() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.end("r1");
    t.player.houseDuck();
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    await sleep(60); // the reply is reported, nothing is left
    assert.deepEqual(t.gain.ramps[t.gain.ramps.length - 1], [1, 0.15]);
    const ramps = t.gain.ramps.length;
    t.player.begin("r2", 24000);
    assert.equal(t.gain.ramps.length, ramps); // not lowered again
  },

  async a_stop_forgets_that_someone_was_speaking() {
    const t = setup();
    await t.player.ready;
    t.player.duck();
    t.player.stop();
    t.player.begin("r1", 24000);
    assert.equal(t.gain.ramps.some((r) => r[0] === 0.25), false);
  },

  async a_stopped_reply_goes_on_when_the_house_resumes() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.stop();
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
    t.player.resume();
    assert.deepEqual(t.gain.ramps[t.gain.ramps.length - 1], [1, 0.15]);
    assert.equal(t.player.begin("r1", 24000), true);
    t.player.push(pcm([2]));
    t.player.end("r1");
    const heard = kinds(t.node(), "push").flatMap((m) => Array.from(m.samples));
    assert.deepEqual(heard, [1 / 32768, 2 / 32768]);
    assert.equal(t.sent.length, 1); // told once
  },

  async a_stop_without_a_resume_still_drops_the_reply() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.stop();
    assert.equal(t.player.begin("r1", 24000), false);
  },

  async the_speaking_line_is_for_sound_not_for_the_wait() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.end("r1");
    assert.deepEqual(t.playing, [true]);
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    assert.deepEqual(t.playing, [true, false]); // during the grace the line is not "speaking"
    assert.equal(t.player.playing(), true); // the reply is still open for ducking
    await sleep(60);
  },

  async sound_the_worklet_refused_makes_the_reply_not_whole() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    assert.equal(kinds(t.node(), "push")[0].reply, "r1");
    t.node().port.onmessage({ data: { type: "overflow", reply: "r1" } });
    t.player.end("r1");
    t.node().port.onmessage({ data: { type: "reached", ids: [1] } });
    await sleep(60);
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
  },

  async a_reply_that_begins_while_someone_speaks_starts_lowered() {
    const t = setup();
    await t.player.ready;
    t.player.duck(); // someone speaks, nothing plays
    assert.deepEqual(t.gain.ramps, []);
    t.player.begin("r1", 24000);
    assert.deepEqual(t.gain.ramps, [[0.25, 0.08]]);
    t.player.speechEnded();
    await sleep(70);
    assert.deepEqual(t.gain.ramps[1], [1, 0.15]);
  },

  async close_lets_everything_go() {
    const t = setup();
    await t.player.ready;
    t.player.begin("r1", 24000);
    t.player.push(pcm([1]));
    t.player.close();
    assert.deepEqual(t.sent, [{ type: "played", reply: "r1", whole: false }]);
    t.player.push(pcm([2])); // after the close: nothing
  },
};

assert.ok(scenarios[scenario], "unknown scenario");
await scenarios[scenario]();
