# Vendored files

Third-party files served as they are, without a CDN at run time (the page loads nothing from
another host). Each one is the file of the official npm package, copied byte for byte.

| File | npm package | Version | Source | SHA-256 | License |
| --- | --- | --- | --- | --- | --- |
| `htmx.min.js` | `htmx.org` | 2.0.11 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` | 0BSD |
| `htmx-ext-sse.js` | `htmx-ext-sse` | 2.2.4 | https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/dist/sse.js | `3b5992a541619babefc4c169505af474df5c3039da51e59b96ccf9241ecd61d2` | 0BSD |

Both versions are 2.x releases that were published more than two weeks before they were vendored
(htmx 2.0.11 on 2026-09-22, htmx-ext-sse 2.2.4 on 2025-10-18). The files were read from the
package tarballs, whose integrity was checked against the npm registry before extraction:

- `htmx.org-2.0.11.tgz`: `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==`
- `htmx-ext-sse-2.2.4.tgz`: `sha512-LJmxVhykyflBWgh5PvbRidcyuqMHlgfajmmzumvKctv9puvsufeH6OaejSMZTNTnEI8O2wXXn4ZZtBdpEMqmEQ==`

The files that jsDelivr serves at the URLs above are byte-identical (same SHA-256).
`htmx-ext-sse.js` is the extension's readable build (`dist/sse.js`), not the minified one.

The other scripts of this folder (`chat.js`, `model-step.js`, `passkeys.js`) are written by the
project, not vendored. In particular no third-party browser library is used for passkeys:
`passkeys.js` does the base64url conversions and the calls to `navigator.credentials` itself, and
the checking is done by the server (the `webauthn` Python package, see `accounts/passkeys.py`).

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
