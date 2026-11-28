# Vendored files

Third-party files served as they are, without a CDN at run time (the page loads nothing from
another host). Each one is the file of the official npm package, copied byte for byte.

| File | npm package | Version | Source | SHA-256 | License |
| --- | --- | --- | --- | --- | --- |
| `htmx.min.js` | `htmx.org` | 2.0.11 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` | 0BSD |
| `htmx-ext-sse.js` | `htmx-ext-sse` | 2.2.4 | https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.js | `3b5992a541619babefc4c169505af474df5c3039da51e59b96ccf9241ecd61d2` | 0BSD |
| `voice/ort/ort.wasm.min.mjs` | `onnxruntime-web` | 1.31.1 | https://cdn.jsdelivr.net/npm/onnxruntime-web@1.31.1/dist/ort.wasm.min.mjs | `c21d9c326d453a4a61398270c804d5979e118540597cb0c07f7b3d64e6098234` | MIT |
| `voice/ort/ort-wasm-simd-threaded.mjs` | `onnxruntime-web` | 1.31.1 | https://cdn.jsdelivr.net/npm/onnxruntime-web@1.31.1/dist/ort-wasm-simd-threaded.mjs | `30ed0b5cd1182ede931fea448afb567d4dc705ec957af09f108a2be5f770dac3` | MIT |
| `voice/ort/ort-wasm-simd-threaded.wasm` | `onnxruntime-web` | 1.31.1 | https://cdn.jsdelivr.net/npm/onnxruntime-web@1.31.1/dist/ort-wasm-simd-threaded.wasm | `4b884c203b0fa48d057be3332c65513ff85aa3c5bddd6946acc0fab7ecda001d` | MIT |
| `voice/silero_vad.onnx` | (not on npm: Silero VAD v6.2 model) | 6.2 | https://github.com/snakers4/silero-vad/blob/bfdc019302/src/silero_vad/data/silero_vad.onnx | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` | MIT |

Both versions are 2.x releases that were published more than two weeks before they were vendored
(htmx 2.0.11 on 2026-09-22, htmx-ext-sse 2.2.4 on 2025-10-18). The files were read from the
package tarballs, whose integrity was checked against the npm registry before extraction:

- `htmx.org-2.0.11.tgz`: `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==`
- `htmx-ext-sse-2.2.4.tgz`: `sha512-LJmxVhykyflBWgh5PvbRidcyuqMHlgfajmmzumvKctv9puvsufeH6OaejSMZTNTnEI8O2wXXn4ZZtBdpEMqmEQ==`

The files that jsDelivr serves at the URLs above are byte-identical (same SHA-256).
`htmx-ext-sse.js` is the extension's readable build (`dist/sse.js`), not the minified one.

The other scripts of this folder (`chat.js`, `model-step.js`, `passkeys.js`, `voice.js`) are written by the
project, not vendored. In particular no third-party browser library is used for passkeys:
`passkeys.js` does the base64url conversions and the calls to `navigator.credentials` itself, and
the checking is done by the server (the `webauthn` Python package, see `accounts/passkeys.py`).

## The speech detector of the voice (`voice/`)

The page tells speech from silence itself, so that only moments of speech leave the browser
(`voice.js`, `voice/detector.js`, written by the project). It runs the Silero VAD model on the
ONNX Runtime Web WebAssembly runtime, single-threaded and without a worker of its own (no
`SharedArrayBuffer`, so no cross-origin isolation is needed), everything served from our own address.

- The three `voice/ort/` files are the files of the npm package `onnxruntime-web` 1.31.1
  (published 2026-10-26), copied byte for byte from the tarball whose integrity was checked against
  the registry (`sha512-5GeH+bWQ+HxM10z7erGlsWqryrwMbSvm/1tkIsKTT48g0fI19VXVVC435t4t7VsPdSQeS7NmuZPXrQ73k82URw==`).
  Only the plain WebAssembly build is kept: not the WebGPU, JSEP, JSPI or asyncify builds (30 MB
  and more each). Licence MIT: stated in the header of `ort.wasm.min.mjs` and in the package's
  `package.json`. The source repository's licence file could not be read on the day it was
  vendored (the repository answered 404): the licence is the one the package declares.
- Follow-up: read ONNX Runtime's `LICENSE` at its repository (github.com/microsoft/onnxruntime) when it is reachable again, and record it here.
- `voice/silero_vad.onnx` is the model of Silero VAD v6.2 (repository `snakers4/silero-vad`,
  commit `bfdc019302`, 2025-11-06; the newer v6.3 of 2026-11-20 was left for later because it
  is younger than two weeks). Licence MIT (`LICENSE` of the repository, "Copyright (c) 2020-present
  Silero Team"). Input: 64 samples of context plus 512 samples of 16 kHz sound, a state of
  2x1x128, the rate; output: the probability of speech.
- `voice/capture-worklet.js` and `voice/detector.js` are written by the project, not vendored.

## Updating

Download the new version from the npm package (or the jsDelivr URL pattern above), replace the
file, and update the version, source and SHA-256 in the table. A test
(`tests/test_web_app.py`) fails when a file and its recorded hash differ.

## How htmx is configured

htmx is made to work under the content security policy (`default-src 'self'`, no `unsafe-eval`,
no inline code) by the `htmx-config` meta tag in `templates/base.html`: `allowEval` and
`allowScriptTags` are off, and `includeIndicatorStyles` is off because htmx would otherwise add
an inline `<style>` element (the indicator rules are in `app.css` instead). Templates therefore
cannot use `hx-on:*` attributes or `js:` expressions in `hx-vals`.

Two more consequences of `allowEval: false`:

- `hx-trigger` filters (`keyup[key=='Enter']`, `click[ctrlKey]`...) are conditions that htmx
  evaluates as code. With evaluation off they are silently *always true*: the trigger fires on
  every event of that kind. Do not use them; filter in a script of `static/` instead.
- `hx-vals` and `hx-headers` cannot use `js:`, and `hx-on:*` is not available either.

## The server-sent events extension

The chat streams a reply with `htmx-ext-sse` (`sse-connect` on the reply's bubble, `sse-swap` on
the elements that receive the events). What to know when touching it:

- `sse-close` names one event: the extension closes its connection when that event arrives. The
  chat sends `done` at the end of every reply, with or without an `error` event before it.
- When the browser's own `EventSource` ends up closed (the server answered something else than a
  stream, `204` included) the extension opens a new one after a growing delay, for as long as the
  element has `sse-connect`. `static/chat.js` removes the attribute on `htmx:sseClose` and on a
  closed `htmx:sseError`, which is what stops it.
- The browser's own connection errors are events named `error` as well, and reach an
  `sse-swap="error"` listener with no data: the extension would try to swap `undefined` in (a
  `TypeError` in the console). `static/chat.js` cancels such a swap (`htmx:sseBeforeMessage`
  with no text). Keep this in mind before naming other server events `error`.
