# domo-speech

A small stateless speech service at home. It turns sound into text, text into sound, and sound
into a voice print, and keeps nothing: sound lives in memory for the time it is processed, and
neither the sound nor the heard text is ever logged or written to disk.

## Routes

| Route | What it does |
|---|---|
| `POST /v1/audio/transcriptions` | multipart `file` (WAV or raw PCM s16le, 16 kHz, mono), `model`, optional `language`, `response_format=verbose_json`, `timestamp_granularities[]=word` |
| `POST /v1/audio/speech` | JSON `{"model","input","voice"?,"language"?,"response_format":"pcm"}`, answers streamed PCM s16le, 24 000 Hz, mono |
| `POST /v1/voice/print` | multipart `file`, answers the vector and the measures of the voice |
| `GET /v1/models` | the models that hold on this machine, what each one does (`hear`, `say`, `voice_print`), what was measured of it, how many requests it serves at once (`at_once`), its `licence` (`id`, `url`, `restricted`) and, for a print model, its `thresholds` |
| `GET /health` | 200 `ready` (with the uses `missing`, if any, the uses `slow`, served by a model kept although too slow, and `retrying: true` while they are to be measured again); 503 `preparing` until the first use is measured; 503 `failed` with a `reason`: `nothing_holds` (not even the floor holds here), `weights_unreachable` (the weights cannot be fetched; `retrying: true`, tried again later), `measure_failed` |

While it is not ready, the three working routes answer 503 `{"error":"preparing"}`, or
`{"error":"failed","reason":...}` with the same reason as `/health`. A request that is not a
proper upload (an empty one, for one) answers 400 `bad_upload`. Each working
route takes the `model` the caller chose among those `/v1/models` lists: the service never
chooses one. When an engine fails, the answer is a plain code and never holds the text or the
sound: 404 `unknown_model`, 503 `engine_unavailable` (a model that cannot be loaded now), 500
`engine_failed`. A voice that fails after its first chunk cuts the connection: the caller sees a
broken answer, never a complete one.

## Which models

No model is chosen in advance. `src/domo_speech/data/models.json` lists open candidates, each
with the licence of its weights and of the code that runs it, where its weights come from
(pinned revision and digests), whether it needs a graphics card, and `floor: true` for the small
models meant for a plain processor. At the first start, and again when the machine changes (its
processor, graphics cards, memory, the `DEVICE` setting) or the list does, the service fetches
and tries the candidates this machine may run (`domo_speech.measure`): load time, memory, speed,
and for hearing whether it hears back what a voice said (a voice holds only if a hearing that
holds hears it back). A model just loaded is slow on its first call: each trial makes that call
first, not counted, then times the work three times and keeps the median. Each trial has a time
limit (5 minutes): one that hangs is given up and the others go on; what is measured is listed as each use completes. The result is written in
`MODEL_DIR/measure.json`; only the candidates that hold are listed. The assistant chooses among
them. A use is never left with nothing for speed alone: when none of its candidates is fast
enough, the fastest that did its work right (not failed; for hearing, it heard the voices back;
for a voice, it was heard back) is kept, one request at a time, and marked `"slow": true` on the
model in `/v1/models` and `"slow": ["hear"]` (its uses) in `/health`: on a weak or busy machine
hearing takes longer and replies come later, rather than no hearing at all. With a graphics card, a candidate that can also run on a processor and does not hold on the
card (it fails to load or to run there, hangs, or is too slow) is tried once more on the processor,
and served there when it holds: missing card libraries, or a card short of memory, never leave a
use with nothing the processor could do. No hearing candidate lighter than `faster-whisper-base` is
listed (a lighter one needs its pinned revision and digests, so a download to check them): on a
modest processor hearing may not be fast enough (on the PC of the trials, base took 0.41 to 0.79
seconds of work per second of sound, the bound being 0.5), and then it is kept as slow. Deleting
`measure.json` makes the service measure again at its next start. A use served slowly, or left
with nothing because its trials were too slow, failed or heard wrong (a busy machine), is measured
again in the same start, with the waits below, at most 5 times (`REMEASURES`; health says
`retrying` meanwhile), and again at each start; a model fast enough found then replaces the slow
one, without a restart. A measure again never takes away what held: when nothing of the use
holds then (a machine busier still), the model kept before stays, and a model tried again only
to hear a voice or be heard keeps what it held; a round to measure again waits while requests
are being served. A candidate
whose weights cannot be fetched (no network, or not the published files) is saved as
`not_fetched` and tried again alone after 10 minutes, then twice as long each time, up to 6
hours.

On a plain processor the first start fetches about 1.1 GB (every candidate a processor may run);
with a graphics card, about 2.9 GB.

### The imported floor voice and its licence

The voice that runs inside the service on a plain processor (`supertonic-3`) is not under an
open-source licence: its weights are under the BigScience Open RAIL-M licence
(https://huggingface.co/supertone-oss-archive/supertonic-3/blob/aafc6e32416a594460b32413efc49d7fe4ce6d46/LICENSE).
Its use is free, but restricted: among other things, not to break the law, not to impersonate
someone without their consent, not to give medical advice or interpret medical results, not to
spread content without saying it is machine-made, not for fully automated decisions against
someone's rights, not to discriminate or harass (its Attachment A has the full list). Its
authors archived their repository: no fix or update will come. Such candidates are marked
`restricted: true` in the data file and in `/v1/models`. They are offered by default;
`DOMO_SPEECH_RESTRICTED_MODELS=no` hides them from the measure and from `/v1/models`. The code
that runs it is ours, derived from its authors' MIT inference code (see `NOTICE`).

A voice under the GPL (Piper, for one) is never imported: it runs as a separate program the
service starts for each answer, fed the text on its input and read as raw sound on its output.
Install it apart (for example `uv tool install piper-tts`) and set `VOICE_PROGRAM` to it; without
it, its candidates are not tried. Hearing never imports PyAV (FFmpeg, LGPL): the service gives it
sound already decoded.

### Several people at once

Two people in two rooms may speak to the assistant at the same moment. How many requests each
model serves side by side is not fixed in code: it is measured on the machine, with the rest of
the measure. Once every use is measured (and served, one at a time), a candidate that holds alone
is tried again with two requests at once, then three, and so on (up to 8, within 15 minutes for
all of them), until it no longer holds: the same speed rules as above (and for hearing, the
words heard back), and the memory of the whole, at its peak during the work, must fit (at most a
quarter of the machine's memory for one model, and of the graphics card's for a model that runs
there, read from the card's driver; and loading must not fail). The number is saved in
`measure.json` with the rest, measured again when the machine changes, and `/v1/models` gives it
as `at_once`. A model short of memory while another model of its use is still loaded (still
serving a request) waits for that one to be gone, then is loaded for the full number again.
Short of memory alone, it is loaded for fewer (down to one), and the lower number is kept, and
listed, while the service runs; not while a measure round runs (its trials take memory too):
then it is served with fewer for now and loaded in full again later. Meanwhile the models of the
other uses are loaded as usual. Under steady requests for the other model of its use, a model
waiting for room may be refused (`503 engine_unavailable`) again and again: the caller asks
again later.

The service then serves up to that number at once for each model; the requests beyond it wait
their turn, in the order they came, and none is dropped while its caller waits. A caller that hangs
up leaves the queue at once, before any work; work already begun ends and its result is dropped. A machine where a model holds one at a
time serves one at a time, as before (a voice, one sentence at a time; a separate program voice
one answer at a time, its sound kept until read, so a slow reader holds no one up). Switching to
another model of the same use never cuts a request in flight: the model asked for no more is
unloaded once its last request is served. Hearing runs one worker of its library per request at once (the
processor's cores shared among them); the voice and the prints run one model, safe to call from
several requests at once; a separate program voice runs once per answer.

`DOMO_SPEECH_AT_ONCE` lowers the number for every model (for one, to keep the machine free for
other work); it never raises it above what was measured. The assistant does not read `at_once`
yet: he cannot tell a person how many may speak at once; the service gates by itself.

## Run

    uv run --extra models domo-speech

### In the stack (Docker Compose)

`compose.yaml` has a service `speech` (image built from `packages/domo-speech/Dockerfile`). It is
reached by the gatekeeper alone: it shares a closed network (`speech_net`, with a fixed subnet the
gatekeeper counts as its own, so a program never reaches it) with it, is not on the core's network,
and publishes no port. Its root filesystem is read-only; the only place it writes
is the volume `speech-models`, mounted on `/models`. The core finds it at
`DOMO_SPEECH_URL=http://speech:8083/v1`.

It needs the Internet once, at its first start, to fetch the open model weights (it has a network of its
own for that, `speech_egress`, which no other service joins); give it a folder already filled and it fetches nothing. The first
start measures the models and may take several minutes: its health check (`healthcheck.py`) counts
`preparing` as healthy, but a service that says it failed (for example `weights_unreachable`, the
weights cannot be fetched) is unhealthy; nothing waits for it. To keep the weights on another disk, replace the volume with a
bind mount in a compose override file (`volumes: ["/mnt/big/speech-models:/models"]`, a folder
owned by user 10001).

With an NVIDIA graphics card (and the NVIDIA Container Toolkit). The CUDA libraries it needs
(cuBLAS and cuDNN, the optional extra `gpu`) are proprietary software of NVIDIA, under its own
licence: they are installed only in the GPU image you build yourself for your own card, and this
project never distributes them.

    docker compose -f compose.yaml -f compose.gpu.yaml up -d

The voice that holds on a plain processor is under a licence with conditions of use (see "The
imported floor voice and its licence" above, where its URL is; it is the interim floor voice the
owner accepted). To hide every model with such conditions, set `DOMO_SPEECH_RESTRICTED_MODELS=no`
in `.env`: `compose.yaml` passes it to the service, with `DOMO_SPEECH_DEVICE`,
`DOMO_SPEECH_AT_ONCE` and `DOMO_SPEECH_VOICE_PROGRAM` (a path inside the container, for one under
`/models`); `.env.example` names them. A setting left empty is unset.

### Outside Docker

    uv tool install "./packages/domo-speech[models]"
    domo-speech

On a server that runs the assistant in Docker, or on another machine of the house, set
`DOMO_SPEECH_HOST` to the address the machine's network card has (the default `127.0.0.1` is
reachable from the machine itself only) and `DOMO_SPEECH_MODEL_DIR` to a folder you own. The core's
`DOMO_SPEECH_URL` names where the gatekeeper reaches it (for a service on the host machine of the
stack, `http://host.docker.internal:8083/v1`, once the gatekeeper can reach the host). The service
has no password: reach it from the house's network only, never from the Internet.

Settings are environment variables prefixed `DOMO_SPEECH_`: `HOST` (127.0.0.1), `PORT` (8083),
`MODEL_DIR` (where the weights and the measure are written: the owner picks the disk; a few
hundred megabytes on a plain processor, a few gigabytes with a graphics card), `DEVICE` (`auto`,
`cpu`, `gpu`), `ENGINES` (`real` or `fake`), `VOICE_PROGRAM` (a voice that is a separate
program), `RESTRICTED_MODELS` (`yes` by default; `no` hides candidates whose licence restricts
their use), `AT_ONCE` (unset; a number lowers how many requests one model serves at once). The fake engines are deterministic and need no model; they exist for tests. The real
engines come with the optional extra `models` (CTranslate2 and ONNX Runtime, no deep-learning
framework).

A graphics card (NVIDIA) needs the CUDA 12 libraries: cuBLAS and cuDNN 9, from the system or as
Python packages (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) installed beside the service. Without
them every trial on the card fails, and the candidates that can run on a processor are measured
there instead (see "Which models").

The tests that load real models are opt-in:

    DOMO_SPEECH_TEST_MODELS=1 DOMO_SPEECH_MODEL_DIR=<dir> pytest -m speech_models packages/domo-speech

## A small local model for listening

Judging whether a sentence heard is meant for the assistant, and sorting what he heard, is not
done by this service: it is ordinary chat, served by a small local model the owner declares as a
provider at home: the owner gives the assistant its address in the chat, like a key, with no
secret. Any local server with an OpenAI-compatible chat API fits (llama.cpp's
`llama-server`, Ollama, vLLM, LM Studio); a small instruction model of a few billion parameters
is enough. Its speed was not measured in this branch's trials: they measured the speech parts
alone, from the end of a sentence to the first sound of the answer without the chat model, at
about 2.2 s with a graphics card and 3.4 to 5.1 s on a busy processor
(`docs/superpowers/followups/2026-11-24-voice-trials.md`); the chat model's time adds to that.
Nothing of it is bundled here. "At home" is
the gatekeeper's zone of its address (the house, or the machine that hosts the assistant). Without
such a model, the voice stays off until the owner chooses to let what is heard go to a provider
outside (`speech.hear_outside`) or to be heard only while a button is held
(`voice.after_a_gesture`); and nothing of what is not said to the assistant is kept.

