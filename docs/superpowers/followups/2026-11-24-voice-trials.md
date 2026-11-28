# The voice trials (Task 29)

**En bref, pour toi.** J'ai fabriqué des voix (cinq personnes, deux micros, Alex enrhumé, Paul qui
imite Alex, un « Oui. » d'une demi-seconde, 40 secondes sans pause, Dana et Sam qui discutent) avec
les voix du service lui-même, sans enregistrer personne, puis je les ai fait passer par les vrais
modèles et par le vrai code qui reconnaît les voix. Ce qui marche : sur le micro où il a appris,
chaque voix est reconnue (sauf quand l'apprentissage n'a pas abouti) ; l'imitation n'est jamais
prise pour Alex ; un mot très court n'accuse jamais personne ; les phrases sont bien comprises en
français et en anglais. Ce qui ne va pas encore : sur un mauvais micro, il n'arrive parfois pas à
apprendre une voix ; Alex enrhumé ET sur un autre micro est pris pour un inconnu ; une fois, Sam
(qu'il ne connaissait pas sur ce micro) a été prise pour Dana ; la mesure de vitesse du service
peut, un mauvais jour, déclarer trop lent le modèle qui entend, et alors il n'entend plus rien.
Entre la fin de ta phrase et le premier son, compte environ 2,2 s avec la carte graphique et
3,4 à 5 s sans elle, plus le temps du modèle qui réfléchit (pas mesuré ici). Les voix fabriquées
sont un test plus faible que de vraies personnes : ta liste d'essais à la main, en bas, reste
indispensable. Les essais avec la pile et le navigateur sont commencés, pas finis (voir en bas).

Date: 2026-11-26. Branch `claude/feat-voice-on-a-computer-fb81a5`, base `2d1345da` (the core was
being edited by another implementer at the same time: line numbers below are those of the tree
at that moment).

## What was run

- **The recorded set** (`scripts/voice_bench/make_voices.py`, 102 WAV files + `manifest.json` in
  `D:\domo-voice-models\trials\voices`, never in the repository). Every voice is a style of the
  service's own floor voice (`supertonic-3`): Alex `M1`, Dana `F1`, Paul `M4`, Sam `F3`, Tom `F2`
  raised 4 semitones (pitch and formants, length 0.92: child-like; median pitch 285-307 Hz).
  Four sentences to learn from and four to read (two French, two English each), each through two
  microphone chains: `mic1` ("laptop": 48 kHz, 90-7000 Hz, white noise and hum 30 dB under) and
  `mic2` ("webcam": 22.05 kHz, small room 0.25 s, 300-3800 Hz with a tilt, pink noise 18 dB
  under). Cold Alex (-1.5 semitones and 9 % slower by resampling, nasal filter); Paul imitating
  Alex (moved to Alex's median pitch and length, sentence by sentence); a 0.5 s "Oui." of Alex and
  of Dana; 41.9 s of Alex without a pause; Dana and Sam in six French turns with one 0.4 s overlap
  (18.6 s, room 0.4 s). Fixed seeds: a second build gave 97 of the 103 files byte for byte; the 6 others differ by
  at most one least significant bit (the voice model's threaded sums on the CPU).
- **The speech service** as a plain local process (`python -m domo_speech`, 127.0.0.1:18500, from
  the venv on D:, weights in `D:\domo-voice-models\models`), three times: CPU only
  (`DOMO_SPEECH_DEVICE=cpu`, `CUDA_VISIBLE_DEVICES=`) twice, GPU allowed (`auto`) once. No
  container, no stack, no browser.
- **`tests/e2e/test_voice.py`** (`speech_models`, opt-in, skipped by default; `voice.env` gives
  the defaults): every sound printed by every print model the service lists, the core's own
  `learn` (four sentences, two visits two hours apart, `sure_otherwise=True`) then `read_voice` of
  every other sound against all five people, on a throwaway PostgreSQL (the core tests'
  `pgvector/pgvector:pg17`, via testcontainers, migrated to head; one transaction per scenario,
  rolled back). Two scenarios per print model: everyone learnt on `mic1`, everyone learnt on
  `mic2`. Then every sentence transcribed by every hearing model listed (no language given).
  Results: `D:\domo-voice-models\trials\results\voice-{cpu,gpu}.{json,md}` (the prints gave
  identical rows on both runs: the print models run on the CPU either way).
  Outcome, both runs: **4 passed, 5 failed**; each failure is a finding below.
- **`scripts/voice_bench/speed.py`** against the running service (HTTP, as the core calls it):
  speeds, time to the first chunk of sound, two at once for real, memory. **`first_call.py`**:
  what the first call of a freshly built engine costs.
- The machine: 6-core Intel, 32 GB, RTX 2070 8 GB, Windows 11. **It was busy** (other sessions,
  Docker/WSL: the processor at about 50 % before I started): CPU figures are pessimistic and
  vary from run to run (see defect 1).

**Synthetic voices are a weaker test than real people.** Styles of one voice model may be too
easy to tell apart (each is a fixed embedding, the same every sentence) or too hard (they share
one vocoder); a resampled "child" or "cold" is a crude imitation of the real thing; nobody coughs,
laughs or turns away from the microphone. These numbers rank the print models and show where the
code breaks; they do not prove the thresholds for a real household. The owner's by-hand list at
the end covers the real thing.

## The voice prints

Degree = the core's reported degree of the sound against that person (0..1); then the readings:
`F` for, `A` against (someone else known is nearer), `u` unknown/unsure. Four sounds per cell
(one for `short`, `long`). "same mic": the sound on the microphone the person was learnt on;
"cross mic": on the other one (the core discounts it by 0.8). The service's thresholds for both
print models are `print_low 0.20, same_voice 0.40, print_high 0.70` (so "for" from cosine 0.50 on
the same microphone, "against" under 0.35); the core's `FOR_AT 0.60`, `AGAINST_AT 0.30`.

### Summary of cosines (similarity of a sound to a person's learnt base print)

| print model | learnt on | genuine, same mic (min / median) | genuine, cross mic (min / median) | another person (median / max) | cold Alex vs Alex (min-max) | imitation vs Alex (max) |
|---|---|---|---|---|---|---|
| resnet34 | mic1 | 0.64 / 0.74 | 0.48 / 0.62 | 0.15 / 0.46 | 0.25-0.54 | 0.34 |
| resnet34 | mic2 | 0.58 / 0.73 | 0.53 / 0.63 | 0.14 / 0.50 | 0.37-0.61 | 0.34 |
| CAM++ | mic1 | 0.64 / 0.76 | 0.50 / 0.61 | 0.15 / 0.40 | 0.17-0.40 | 0.27 |
| CAM++ | mic2 | 0.59 / 0.73 | 0.57 / 0.72 | 0.17 / 0.36 | (Alex not learnt) | (Alex not learnt) |

(CAM++ on mic2 covers only Paul, Sam and Tom: Alex and Dana were never learnt there, defect 3.)

### What the spec expects, and what happened

| expectation | result |
|---|---|
| every voice learnt from 4 sentences on 2 visits | **no** on mic2: CAM++ never learnt Alex nor Dana (candidate restarted); resnet34 did not learn Sam (Dana "in the way"). Always yes on mic1. |
| a genuine voice on its own microphone weighs "for" | yes whenever the voice was learnt (40/40 on mic1, every learnt voice on mic2) |
| no voice weighs "for" somebody else | **one miss**: `sam.test1.fr.mic2` read "for" Dana at degree 0.60 (cosine 0.503), resnet34, mic2, where Sam is not learnt |
| a voice learnt on mic1 is not a stranger on mic2 (and back) | yes (passed): 64 of 68 readings of learnt voices "for", 4 unsure |
| the cold voice is unsure and still Alex | same mic: yes (unsure, or even "for" with resnet34), but one CAM++ reading sits at the edge (degree just under 0.30: "resembles nobody"). **Learnt on mic1, heard on mic2: no**: 7 of 8 readings (both models) say "resembles nobody" (a stranger). Learnt on mic2 (resnet34), heard on mic1: unsure, as wanted |
| the imitation is never sure | yes (passed): never "for" Alex, highest degree 0.30; it often still reads "for" Paul |
| a 0.5 s utterance is never "against" | yes (passed): always `short`, unsure |
| 40 s without a pause is Alex on his microphone | yes (passed): degree 0.90 (both models) |
| the room (two people) | 3 s windows follow whoever speaks most of the window (resnet34, mic1: 11/11 windows; CAM++ mic1: 10/11); the whole 18.6 s file is given to one of the two ("for" Sam at 1.00 when both are learnt on mic1; "for" Dana at 0.60 with resnet34 learnt on mic2): a mixture is read as one person, never as "two voices" |

## The full print tables

Model names shortened (`wespeaker-voxceleb-` dropped). The cosines lines are to the learnt base;
the learning lines give what each of the four `learn` calls returned and the cosine from each
learn sentence to the next (FR, FR, EN, EN).

### Print model campplus-lm, learnt on mic1

| sounds | Alex | Dana | Paul | Sam | Tom |
|---|---|---|---|---|---|
| Alex test (same mic) | 1.00 4F | 0.00 4A | 0.00-0.10 4A | 0.00-0.10 4A | 0.00 4A |
| Dana test (same mic) | 0.00 4A | 1.00 4F | 0.00 4A | 0.00-0.30 4A | 0.00 4A |
| Paul test (same mic) | 0.10-0.49 4A | 0.00 4A | 1.00 4F | 0.00 4A | 0.00 4A |
| Sam test (same mic) | 0.00 4A | 0.00-0.30 4A | 0.00-0.09 4A | 1.00 4F | 0.00-0.03 4A |
| Tom test (same mic) | 0.00 4A | 0.00 4A | 0.00 4A | 0.00-0.07 4A | 1.00 4F |
| Alex test (cross mic) | 0.60-0.80 4F | 0.00-0.10 4A | 0.00-0.11 4A | 0.00-0.10 4A | 0.00-0.12 4A |
| Dana test (cross mic) | 0.00 4A | 0.60-0.75 4F | 0.00 4A | 0.00-0.18 4A | 0.00 4A |
| Paul test (cross mic) | 0.15-0.30 4A | 0.00 4A | 0.64-0.90 4F | 0.00 4A | 0.00-0.05 4A |
| Sam test (cross mic) | 0.00-0.02 4A | 0.00-0.23 4A | 0.00-0.02 4A | 0.48-0.90 3F1u | 0.00 4A |
| Tom test (cross mic) | 0.00 4A | 0.00 4A | 0.00 4A | 0.00 4A | 0.63-0.90 4F |
| Alex cold (same mic) | 0.30-0.49 4u | 0.00 3A1u | 0.00-0.10 3A1u | 0.00 3A1u | 0.00 3A1u |
| Alex cold (cross mic) | 0.00-0.02 4u | 0.00-0.10 4u | 0.00 4u | 0.00-0.10 4u | 0.00-0.10 4u |
| Paul imitation (same mic) | 0.10-0.24 3A1u | 0.00-0.10 3A1u | 0.18-1.00 2F2u | 0.00-0.30 3A1u | 0.00 3A1u |
| Paul imitation (cross mic) | 0.00-0.20 2A2u | 0.00 2A2u | 0.00-0.90 2F2u | 0.00-0.30 2A2u | 0.00 2A2u |
| Alex short (same mic) | 0.28 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (same mic) | 0.02 1u | 0.19 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex short (cross mic) | 0.08 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (cross mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex long (same mic) | 0.90 1F | 0.00 1A | 0.15 1A | 0.00 1A | 0.00 1A |

- cosine to the base, genuine, same mic: min 0.64, median 0.76, max 0.90 (n=20)
- cosine to the base, genuine, cross mic: min 0.50, median 0.61, max 0.80 (n=20)
- cosine to the base, another person: min -0.09, median 0.15, max 0.40 (n=160)
- cosine to the base, cold Alex vs Alex: min 0.17, median 0.31, max 0.40 (n=8)
- cosine to the base, imitation vs Alex: min 0.03, median 0.15, max 0.27 (n=8)

Room windows (3 s, every 1.5 s), read against Dana / Sam:

| window | truth | Dana | Sam |
|---|---|---|---|
| 0.0-3.0 s | Dana 2.1s + Sam 0.6s | 0.71 for | 0.53 against |
| 1.5-4.5 s | Dana 0.6s + Sam 2.1s | 0.29 against | 1.00 for |
| 3.0-6.0 s | Dana 0.6s + Sam 2.1s | 0.00 against | 1.00 for |
| 4.5-7.5 s | Dana 2.1s + Sam 0.6s | 0.60 for | 0.30 against |
| 6.0-9.0 s | Dana 2.4s + Sam 1.0s | 1.00 for | 0.12 against |
| 7.5-10.5 s | Dana 0.9s + Sam 2.5s | 0.70 for | 0.40 against |
| 9.0-12.0 s | Dana 0.2s + Sam 2.5s | 0.30 against | 1.00 for |
| 10.5-13.5 s | Dana 1.7s + Sam 1.0s | 0.76 for | 0.48 against |
| 12.0-15.0 s | Dana 2.8s | 0.85 for | 0.10 against |
| 13.5-16.5 s | Dana 1.3s + Sam 1.3s | 0.48 against | 0.80 for |
| 15.0-18.0 s | Sam 2.8s | 0.24 against | 1.00 for |

Learning (what each of the four learn calls returned):

- Alex: candidate candidate learnt updated (cosines one sentence to the next: 0.70, 0.45, 0.76)
- Dana: candidate candidate learnt updated (cosines one sentence to the next: 0.76, 0.52, 0.78)
- Paul: candidate candidate learnt updated (cosines one sentence to the next: 0.82, 0.56, 0.77)
- Sam: candidate candidate learnt updated (cosines one sentence to the next: 0.78, 0.51, 0.78)
- Tom: candidate candidate learnt updated (cosines one sentence to the next: 0.89, 0.62, 0.85)

### Print model campplus-lm, learnt on mic2

| sounds | Alex | Dana | Paul | Sam | Tom |
|---|---|---|---|---|---|
| Alex test (same mic) | 0.00 4u | 0.00 4u | 0.00-0.31 4u | 0.00 1A3u | 0.00-0.19 1A3u |
| Dana test (same mic) | 0.00 4u | 0.00 4u | 0.00 1A3u | 0.00-0.30 4u | 0.00-0.10 1A3u |
| Paul test (same mic) | 0.00 4u | 0.00 4u | 1.00 4F | 0.00-0.01 4A | 0.00-0.16 4A |
| Sam test (same mic) | 0.00 4u | 0.00 4u | 0.00-0.12 4A | 0.89-1.00 4F | 0.00-0.00 4A |
| Tom test (same mic) | 0.00 4u | 0.00 4u | 0.00-0.09 4A | 0.00 4A | 0.86-1.00 4F |
| Alex test (cross mic) | 0.00 4u | 0.00 4u | 0.10-0.29 4u | 0.00 4u | 0.00 4u |
| Dana test (cross mic) | 0.00 4u | 0.00 4u | 0.00 4u | 0.10-0.29 4u | 0.00 4u |
| Paul test (cross mic) | 0.00 4u | 0.00 4u | 0.85-0.90 4F | 0.00 4A | 0.00 4A |
| Sam test (cross mic) | 0.00 4u | 0.00 4u | 0.00 4A | 0.80-0.90 4F | 0.00 4A |
| Tom test (cross mic) | 0.00 4u | 0.00 4u | 0.00 4A | 0.00 4A | 0.90 4F |
| Alex cold (same mic) | 0.00 4u | 0.00 4u | 0.00-0.10 4u | 0.00-0.10 4u | 0.00 4u |
| Alex cold (cross mic) | 0.00 4u | 0.00 4u | 0.10-0.11 4u | 0.00 4u | 0.00 4u |
| Paul imitation (same mic) | 0.00 4u | 0.00 4u | 0.12-1.00 1A2F1u | 0.00-0.31 2A2u | 0.00 3A1u |
| Paul imitation (cross mic) | 0.00 4u | 0.00 4u | 0.15-0.90 1A1F2u | 0.00-0.15 2A2u | 0.00 3A1u |
| Alex short (same mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (same mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex short (cross mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (cross mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex long (cross mic) | 0.00 1u | 0.00 1u | 0.10 1u | 0.00 1u | 0.00 1u |

- cosine to the base, genuine, same mic: min 0.59, median 0.73, max 0.84 (n=12)
- cosine to the base, genuine, cross mic: min 0.57, median 0.72, max 0.83 (n=12)
- cosine to the base, another person: min -0.04, median 0.17, max 0.36 (n=96)

Room windows (3 s, every 1.5 s), read against Dana / Sam:

| window | truth | Dana | Sam |
|---|---|---|---|
| 0.0-3.0 s | Dana 2.1s + Sam 0.6s | 0.00 unknown | 0.43 unknown |
| 1.5-4.5 s | Dana 0.6s + Sam 2.1s | 0.00 unknown | 0.90 for |
| 3.0-6.0 s | Dana 0.6s + Sam 2.1s | 0.00 unknown | 0.87 for |
| 4.5-7.5 s | Dana 2.1s + Sam 0.6s | 0.00 unknown | 0.45 unknown |
| 6.0-9.0 s | Dana 2.4s + Sam 1.0s | 0.00 unknown | 0.17 unknown |
| 7.5-10.5 s | Dana 0.9s + Sam 2.5s | 0.00 unknown | 0.41 unknown |
| 9.0-12.0 s | Dana 0.2s + Sam 2.5s | 0.00 unknown | 0.84 for |
| 10.5-13.5 s | Dana 1.7s + Sam 1.0s | 0.00 unknown | 0.60 unknown |
| 12.0-15.0 s | Dana 2.8s | 0.00 unknown | 0.30 unknown |
| 13.5-16.5 s | Dana 1.3s + Sam 1.3s | 0.00 unknown | 0.42 unknown |
| 15.0-18.0 s | Sam 2.8s | 0.00 unknown | 0.89 for |

Learning (what each of the four learn calls returned):

- Alex: candidate candidate candidate candidate (cosines one sentence to the next: 0.64, 0.37, 0.69)
- Dana: candidate candidate candidate candidate (cosines one sentence to the next: 0.49, 0.42, 0.67)
- Paul: candidate candidate learnt updated (cosines one sentence to the next: 0.74, 0.50, 0.64)
- Sam: candidate candidate learnt updated (cosines one sentence to the next: 0.65, 0.48, 0.77)
- Tom: candidate candidate learnt updated (cosines one sentence to the next: 0.79, 0.59, 0.75)

### Print model resnet34, learnt on mic1

| sounds | Alex | Dana | Paul | Sam | Tom |
|---|---|---|---|---|---|
| Alex test (same mic) | 1.00 4F | 0.00 4A | 0.00-0.30 4A | 0.00-0.10 4A | 0.00 4A |
| Dana test (same mic) | 0.00 4A | 1.00 4F | 0.00 4A | 0.00-0.32 4A | 0.00 4A |
| Paul test (same mic) | 0.14-0.53 4A | 0.00 4A | 1.00 4F | 0.13-0.30 4A | 0.00 4A |
| Sam test (same mic) | 0.00 4A | 0.00-0.60 4A | 0.14-0.30 4A | 1.00 4F | 0.00-0.03 4A |
| Tom test (same mic) | 0.00 4A | 0.00 4A | 0.00 4A | 0.00-0.09 4A | 1.00 4F |
| Alex test (cross mic) | 0.60-0.79 4F | 0.00-0.10 4A | 0.00-0.29 4A | 0.00-0.10 4A | 0.00 4A |
| Dana test (cross mic) | 0.00 4A | 0.60-0.85 3F1u | 0.00 4A | 0.00-0.30 4A | 0.00 4A |
| Paul test (cross mic) | 0.10-0.30 4A | 0.00 4A | 0.62-0.85 4F | 0.00 4A | 0.00 4A |
| Sam test (cross mic) | 0.00 4A | 0.00-0.30 4A | 0.02-0.16 4A | 0.60-0.90 4F | 0.00-0.06 4A |
| Tom test (cross mic) | 0.00 4A | 0.00 4A | 0.00 4A | 0.00 4A | 0.50-0.88 3F1u |
| Alex cold (same mic) | 0.60-0.79 2F2u | 0.00 4A | 0.00-0.10 4A | 0.00 4A | 0.00 4A |
| Alex cold (cross mic) | 0.00-0.34 4u | 0.00-0.10 1A3u | 0.00 1A3u | 0.00-0.10 1A3u | 0.00-0.10 1A3u |
| Paul imitation (same mic) | 0.10-0.30 2A2u | 0.00-0.19 2A2u | 0.16-1.00 2F2u | 0.11-0.36 2A2u | 0.00-0.06 2A2u |
| Paul imitation (cross mic) | 0.00-0.18 2A2u | 0.00 2A2u | 0.00-0.90 2F2u | 0.06-0.20 2A2u | 0.00 2A2u |
| Alex short (same mic) | 0.40 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (same mic) | 0.00 1u | 0.24 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex short (cross mic) | 0.05 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (cross mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex long (same mic) | 0.90 1F | 0.00 1A | 0.35 1A | 0.00 1A | 0.00 1A |

- cosine to the base, genuine, same mic: min 0.64, median 0.74, max 0.89 (n=20)
- cosine to the base, genuine, cross mic: min 0.48, median 0.62, max 0.78 (n=20)
- cosine to the base, another person: min -0.07, median 0.15, max 0.46 (n=160)
- cosine to the base, cold Alex vs Alex: min 0.25, median 0.45, max 0.54 (n=8)
- cosine to the base, imitation vs Alex: min 0.04, median 0.17, max 0.34 (n=8)

Room windows (3 s, every 1.5 s), read against Dana / Sam:

| window | truth | Dana | Sam |
|---|---|---|---|
| 0.0-3.0 s | Dana 2.1s + Sam 0.6s | 0.90 for | 0.78 against |
| 1.5-4.5 s | Dana 0.6s + Sam 2.1s | 0.51 against | 1.00 for |
| 3.0-6.0 s | Dana 0.6s + Sam 2.1s | 0.00 against | 1.00 for |
| 4.5-7.5 s | Dana 2.1s + Sam 0.6s | 0.67 for | 0.49 against |
| 6.0-9.0 s | Dana 2.4s + Sam 1.0s | 1.00 for | 0.30 against |
| 7.5-10.5 s | Dana 0.9s + Sam 2.5s | 0.60 against | 0.60 for |
| 9.0-12.0 s | Dana 0.2s + Sam 2.5s | 0.44 against | 1.00 for |
| 10.5-13.5 s | Dana 1.7s + Sam 1.0s | 0.88 for | 0.61 against |
| 12.0-15.0 s | Dana 2.8s | 0.99 for | 0.37 against |
| 13.5-16.5 s | Dana 1.3s + Sam 1.3s | 0.60 against | 0.88 for |
| 15.0-18.0 s | Sam 2.8s | 0.26 against | 1.00 for |

Learning (what each of the four learn calls returned):

- Alex: candidate candidate learnt updated (cosines one sentence to the next: 0.77, 0.53, 0.81)
- Dana: candidate candidate learnt updated (cosines one sentence to the next: 0.80, 0.59, 0.79)
- Paul: candidate candidate learnt updated (cosines one sentence to the next: 0.82, 0.60, 0.80)
- Sam: candidate candidate learnt updated (cosines one sentence to the next: 0.80, 0.55, 0.79)
- Tom: candidate candidate learnt updated (cosines one sentence to the next: 0.89, 0.62, 0.85)

### Print model resnet34, learnt on mic2

| sounds | Alex | Dana | Paul | Sam | Tom |
|---|---|---|---|---|---|
| Alex test (same mic) | 0.95-1.00 4F | 0.00-0.10 4A | 0.00-0.47 4A | 0.00 4u | 0.00 4A |
| Dana test (same mic) | 0.00 4A | 0.76-1.00 4F | 0.00 4A | 0.00 4u | 0.00-0.10 4A |
| Paul test (same mic) | 0.28-0.60 4A | 0.00 4A | 1.00 4F | 0.00 4u | 0.00 4A |
| Sam test (same mic) | 0.00-0.10 2A2u | 0.02-0.60 1F3u | 0.01-0.19 2A2u | 0.00 4u | 0.00-0.08 2A2u |
| Tom test (same mic) | 0.00 4A | 0.00-0.01 4A | 0.00-0.01 4A | 0.00 4u | 0.87-1.00 4F |
| Alex test (cross mic) | 0.60-0.90 3F1u | 0.00 4A | 0.10-0.24 4A | 0.00 4u | 0.00 4A |
| Dana test (cross mic) | 0.00 4A | 0.60-0.90 4F | 0.00 4A | 0.00 4u | 0.00 4A |
| Paul test (cross mic) | 0.10-0.30 4A | 0.00 4A | 0.82-0.90 4F | 0.00 4u | 0.00 4A |
| Sam test (cross mic) | 0.00-0.10 2A2u | 0.00-0.54 4u | 0.00 2A2u | 0.00 4u | 0.00 2A2u |
| Tom test (cross mic) | 0.00 4A | 0.00 4A | 0.00 4A | 0.00 4u | 0.83-0.90 4F |
| Alex cold (same mic) | 0.30-0.72 1F3u | 0.00 4A | 0.00-0.10 4A | 0.00 4u | 0.00 4A |
| Alex cold (cross mic) | 0.50-0.60 4u | 0.00 4A | 0.10-0.12 4A | 0.00 4u | 0.00 4A |
| Paul imitation (same mic) | 0.00-0.30 3A1u | 0.00-0.15 3A1u | 0.30-1.00 2F2u | 0.00 4u | 0.00-0.08 3A1u |
| Paul imitation (cross mic) | 0.10-0.30 3A1u | 0.00-0.18 2A2u | 0.17-0.90 1A1F2u | 0.00 4u | 0.00 3A1u |
| Alex short (same mic) | 0.24 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (same mic) | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex short (cross mic) | 0.36 1u | 0.00 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Dana short (cross mic) | 0.00 1u | 0.12 1u | 0.00 1u | 0.00 1u | 0.00 1u |
| Alex long (cross mic) | 0.70 1F | 0.00 1A | 0.05 1A | 0.00 1u | 0.00 1A |

- cosine to the base, genuine, same mic: min 0.58, median 0.73, max 0.84 (n=16)
- cosine to the base, genuine, cross mic: min 0.53, median 0.63, max 0.81 (n=16)
- cosine to the base, another person: min -0.06, median 0.14, max 0.50 (n=128)
- cosine to the base, cold Alex vs Alex: min 0.37, median 0.45, max 0.61 (n=8)
- cosine to the base, imitation vs Alex: min 0.05, median 0.17, max 0.34 (n=8)

Room windows (3 s, every 1.5 s), read against Dana / Sam:

| window | truth | Dana | Sam |
|---|---|---|---|
| 0.0-3.0 s | Dana 2.1s + Sam 0.6s | 0.68 for | 0.00 unknown |
| 1.5-4.5 s | Dana 0.6s + Sam 2.1s | 0.30 unknown | 0.00 unknown |
| 3.0-6.0 s | Dana 0.6s + Sam 2.1s | 0.10 unknown | 0.00 unknown |
| 4.5-7.5 s | Dana 2.1s + Sam 0.6s | 0.60 for | 0.00 unknown |
| 6.0-9.0 s | Dana 2.4s + Sam 1.0s | 0.70 for | 0.00 unknown |
| 7.5-10.5 s | Dana 0.9s + Sam 2.5s | 0.43 unknown | 0.00 unknown |
| 9.0-12.0 s | Dana 0.2s + Sam 2.5s | 0.30 unknown | 0.00 unknown |
| 10.5-13.5 s | Dana 1.7s + Sam 1.0s | 0.74 for | 0.00 unknown |
| 12.0-15.0 s | Dana 2.8s | 0.60 for | 0.00 unknown |
| 13.5-16.5 s | Dana 1.3s + Sam 1.3s | 0.39 unknown | 0.00 unknown |
| 15.0-18.0 s | Sam 2.8s | 0.12 unknown | 0.00 unknown |

Learning (what each of the four learn calls returned):

- Alex: candidate candidate learnt updated (cosines one sentence to the next: 0.75, 0.49, 0.70)
- Dana: candidate candidate learnt updated (cosines one sentence to the next: 0.70, 0.49, 0.75)
- Paul: candidate candidate learnt updated (cosines one sentence to the next: 0.78, 0.54, 0.73)
- Sam: candidate candidate nothing candidate (cosines one sentence to the next: 0.71, 0.55, 0.75)
- Tom: candidate candidate learnt updated (cosines one sentence to the next: 0.79, 0.56, 0.78)



## Hearing

No language given, so the detected language is the model's. "Heard back" is the share of the
words said found in the text (a plain count: "9h" for "neuf heures" counts as two misses). Time is
the median HTTP round trip.

**Since 2026-11-28 (the owner's decisions, ledger X41).** The trial still gives no language (it
measures the model). The test compares once hours and numbers are written as words on both sides
(`tests/e2e/heard_words.py`: "9h00" = "neuf heures", "3 pommes" = "trois pommes"; nothing else is
forgiven), and each hearing model has its own bound of misheard sentences (a wrong language, or
under 0.8 heard back): base 10% (its measured misses), small 3%, turbo and any model never measured
none. On the home server's processor (builds 823, 825 then 830, same set, base only): base mishears 9
of 77 sentences counted the old way, **6 of 77** once figures are words (the two "9h00"/"9h" and
"3 pommes" are no longer misses; 6 again in 830); bound 7: the test passes. What stays: base on a processor
mishears French from the high and child-like voices ("peu tue allumée", "aligner/annumer la
lumière", "pattes ... promage", Tom's "vers nos sœurs" at 0.38). The core now sends the person's
known language as a hint (`language_hint`, heard in only when detection is not clear): it does not
mend those, the language was already right.

| run | model | sounds | lang | n | right language | heard back >= 0.8 | mean heard back | median time (s) |
|---|---|---|---|---|---|---|---|---|
| cpu | faster-whisper-base | sentences 3.4-5.2 s | fr | 38 | 38 | 29 | 0.87 | 2.34 |
| cpu | faster-whisper-base | sentences 3.4-5.2 s | en | 38 | 38 | 38 | 1.00 | 2.20 |
| cpu | faster-whisper-base | 40 s no pause | fr | 1 | 1 | 1 | 0.94 | 8.55 |
| cpu | faster-whisper-base | room 18.6 s | fr | 1 | 1 | 1 | 0.87 | 6.70 |
| cpu | faster-whisper-base | 0.5 s "Oui." | fr | 4 | **0** | **0** | 0.00 | 3.38 |
| gpu | faster-whisper-small | sentences 3.4-5.2 s | fr | 38 | 38 | 37 | 0.94 | 0.36 |
| gpu | faster-whisper-small | sentences 3.4-5.2 s | en | 38 | 38 | 38 | 1.00 | 0.33 |
| gpu | faster-whisper-small | 40 s no pause | fr | 1 | 1 | 1 | 0.99 | 2.16 |
| gpu | faster-whisper-small | room 18.6 s | fr | 1 | 1 | 1 | 0.95 | 1.04 |
| gpu | faster-whisper-small | 0.5 s "Oui." | fr | 4 | **0** | **0** | 0.00 | 0.24 |
| gpu | faster-whisper-large-v3-turbo | sentences 3.4-5.2 s | fr | 38 | 38 | 38 | 0.98 | 0.76 |
| gpu | faster-whisper-large-v3-turbo | sentences 3.4-5.2 s | en | 38 | 38 | 38 | 1.00 | 0.74 |
| gpu | faster-whisper-large-v3-turbo | 40 s no pause | fr | 1 | 1 | 1 | 1.00 | 2.11 |
| gpu | faster-whisper-large-v3-turbo | room 18.6 s | fr | 1 | 1 | 1 | 1.00 | 1.20 |
| gpu | faster-whisper-large-v3-turbo | 0.5 s "Oui." | fr | 4 | **0** | 3 | 0.75 | 0.69 |

The base model on CPU misses words in French on the higher and the child-like voices ("peu tue
allumée la lumière du salant", "Petit m'a rappelé ... vers nos sœurs" for Tom); English is word
for word on every model. A 0.5 s "Oui." is detected as English by every model; small and base
hear "We", "Wii", "Bye", "away"; turbo hears "Oui." but still says English.

## Speed and capacity (no stack)

Sentences: `alex.test0.fr.mic1` (4.13 s) and three others of 3.6-4.9 s. Replies said: short "D'accord,
j'allume la lumière du salon." (2.6 s of sound), long two sentences (9.3 s of sound). Medians of
three. Memory: the working set of the service's process (Windows `tasklist`); per-process graphics
memory is not given by `nvidia-smi` on Windows (WDDM), so the card's whole use is given.

| | GPU allowed | CPU only |
|---|---|---|
| hearing that holds (service measure) | small (RTF 0.084, at once 5), turbo (0.195, at once 1); base did **not** hold (RTF 3.45, defect 1) | base (0.53 = too slow on the first start, 0.41 = holds on the second, at once 1); small too slow (1.40-1.85) |
| hearing a 4.1 s sentence | small 0.37 s (RTF 0.08), turbo 0.76 s (0.18) | base 1.6-3.3 s (RTF 0.43-0.79, the machine was busy) |
| hearing 41.9 s | small 1.92 s, turbo 2.16 s | base 7.1 s |
| hearing 0.5 s | small 0.24 s, turbo 0.69 s | base 2.4-3.4 s (the model always works on a 30 s window) |
| voice: first chunk, short reply | 1.09 s (the voice runs on the CPU in both cases) | 1.08 s |
| voice: first chunk, long reply sent in one call | 2.77 s = the whole reply (one chunk per call up to 300 characters: limit 4) | 2.99 s |
| voice RTF | 0.30-0.42 | 0.32-0.42 |
| print of a 4 s sentence / of 41.9 s | resnet34 0.13 s / 1.02 s; CAM++ 0.06 s / 0.94 s | resnet34 0.15 s / 1.20 s; CAM++ 0.07 s / 0.65 s |
| at once (service measure) | small 5, turbo 1, voice 1, prints 8 | base 1, voice 1, prints 8 |
| memory of the service | RAM 1.34 GB with small + turbo + voice + two prints loaded; card 3.1 GB used in all (0.6 GB before the service) | RAM 0.96 GB (base 0.34 GB, voice +0.43 GB, prints +0.2 GB) |

### Two at once, for real (HTTP, two threads)

| | GPU allowed | CPU only |
|---|---|---|
| two hearings (4.1 s and 4.6 s) | small (at once 5): alone 0.35 s, together 0.67 / 0.70 s. turbo (at once 1): alone 0.78 s, together 0.78 / 1.51 s (the second waits) | base (at once 1): alone 1.93 s, together 3.42 / 3.12 s |
| two sayings (short reply) | first chunk alone 1.09 s, together 1.03 / 1.97 s; total 1.09 s alone, 2.27 / 2.15 s together | first chunk 1.08 s alone, together 1.14 / 2.22 s; total 2.25 s each |
| a hearing while a saying | hearing 0.46 s -> 1.13 s; first chunk 1.38 s -> 1.21 s | hearing 1.64 s -> 3.74 s; first chunk 1.04 s -> 1.37 s |

No error and no refusal in any of these (the `errors` list of both runs is empty): the real
libraries took several calls at once without failing. Several hearings on one GPU model with
`at_once` 5 gave no speed-up for two (0.35 s alone, 0.7 s each together): the workers share the
card and the processor. A hearing while the voice speaks is slowed 2.3-2.5x (the voice takes all
the processor's cores), the voice much less.

### End of speech to first sound (estimate from measured parts)

The page waits 700 ms of silence (`voice.js`, 22 frames of 32 ms); hearing and the print run
together (`hearing.py`), so the longer one counts (the hearing); the thinking model is **not
measured here**; then the first sentence's sound (the core sends sentence by sentence,
`listener.py`).

| | page silence | hearing (4 s sentence) | thinking model | first chunk (one short sentence) | total without the model |
|---|---|---|---|---|---|
| GPU, small | 0.70 s | 0.37 s | ? | 1.09 s | **about 2.2 s** |
| GPU, turbo | 0.70 s | 0.76 s | ? | 1.09 s | about 2.6 s |
| CPU, base | 0.70 s | 1.6-3.3 s | ? | 1.08 s | **about 3.4-5.1 s** |

Not counted: the page's own playback buffer, the network, the conversation turn's own work.
A first sentence of 300 characters would take about 0.4 x its length before any sound.

## DEFECTS FOUND

Not fixed here (no product code in this task); each goes back to the task that owns the code.

1. **The service's measure can declare the floor hearing too slow, and keeps that verdict.**
   `packages/domo-speech/src/domo_speech/real/trial.py:82` (`_try`) times the very first calls of
   an engine it has just built, once, with no warm-up and no repeat; `measure.py:322` reuses the
   saved verdict while the fingerprint matches. Seen: GPU run, `faster-whisper-base` RTF **3.45**
   ("too_slow") while `first_call.py` gives 0.12 on its first call and 0.05-0.06 after; CPU run,
   base **0.53 (too slow)** on one start and **0.41 (holds)** fifteen minutes later. With base
   failing on CPU, `/health` said `ready` with `missing: ["hear"]`: the service could not hear at
   all, and would have stayed so until `measure.json` is deleted. Reproduce: start the service on
   a busy machine (`DOMO_SPEECH_DEVICE=cpu`), read `measure.json`; then
   `python -m scripts.voice_bench.first_call faster-whisper-base [--gpu]`. Suggested: one warm-up
   call not counted, the best (or median) of several, and a re-measure when nothing holds for a use.
2. **A cold reads as a voice like nobody known.** On another microphone, the owner decided on
   2026-11-28 « Domo juge à chaque fois » (ledger X40): such a reading is not a failure of the
   reading, it is judged: at Alex's own signed-in page the whose-voice question is asked (X21), then
   the stranger's exchange if the quick model does not say "theirs". (An earlier build, 825, made it
   always unsure instead; that rule was taken out.) The trial now records who the product takes the
   speaker for (`voice_speaker`) and has two tests: `test_the_cold_voice_on_another_microphone_is_alex_or_judged`
   (a cold taken for another known person fails; a cold taken for nobody is counted as judged) and
   `test_the_cold_voice_is_unsure_and_still_alex` (the microphone Alex was learnt on, strict,
   unchanged). Home server, build 830: **another microphone: 16 readings, 6 Alex (unsure or for),
   10 judged, 0 another known person: passes**; **the microphone he was learnt on: 5 of 16 still
   "resemble nobody" (CAM++: mic1 0.30 at the edge, mic2 0.04, 0.17, 0.24; resnet34 mic2 0.14),
   the same five as in 823 and 825: STILL FAILS**, on purpose (no threshold moved, no known-defect
   mark: the trials are not needed green to merge; in the product those five are judged too, by
   the same question). Why: the synthetic cold (-1.5 semitones, 9 % slower, a nasal filter) moves
   the print further than a real cold may; a voice that far from the learnt base cannot be told
   from a stranger by the print alone without letting strangers in. Reproduce: the `cpu` run of
   `tests/e2e/test_voice.py`.
3. **A voice may never be learnt on a poor microphone when the person changes language.**
   FIXED on 2026-11-28 (the owner: « deux phrases de suite », ledger X39): one unlike sentence no
   longer restarts the candidate, two in a row do, and a clearly far one (under `print_low`) at once
   (the owner: « oui, tout de suite »). Home server, build 823 then 825 (and 830 the same): CAM++ on mic2,
   Dana `candidate candidate candidate candidate` then **`candidate candidate candidate learnt`**
   (her first English sentence is not counted, the second joins); everyone is learnt on both
   microphones with both print models, and a genuine voice on its own microphone reads "for"
   everywhere (4 misses, then **0**). Was:
   `prints.py:980` (`_candidate`): the candidate restarts when the next utterance's cosine to it
   is under `same_voice` (0.40). On mic2 with CAM++, Alex's French-to-English step is 0.37 and
   Dana's 0.42/0.49 around it: four sentences on two visits end as `candidate candidate candidate
   candidate`, nothing learnt. On mic1 every step is 0.45 or more and everyone is learnt.
   Reproduce: `test_every_voice_is_learnt_on_each_microphone`. A threshold matter (proposed
   below) and, perhaps, a design one: a single unlike utterance throws away the candidate.
4. **A voice not learnt on a microphone was taken for a near known one.** resnet34, everyone
   learnt on mic2 except Sam (Dana was in the way: Sam's third learn returned `nothing`):
   `sam.test1.fr.mic2` reads **"for" Dana**, degree 0.60, cosine 0.503. The worst "another
   person" cosine with resnet34 (0.46-0.50) touches the "for" bound (0.50). Reproduce:
   `test_no_voice_weighs_for_somebody_else`. A threshold matter (below).
5. **The detected language of a very short utterance is wrong, and the core uses it.** Every
   hearing model calls a 0.5 s French "Oui." English. `packages/domo-core/src/domo_core/channels/
   voice/session.py:716` passes `heard.language` on to the conversation. To check by the owner
   of the session: what a short utterance's language changes (the reply's language?), and
   whether the person's language should win under a couple of seconds.

One defect of my own test, fixed between runs: the "somebody else" check counted the two-people
room (a mixture) as a wrong person; the room is reported on its own.

## PROPOSED THRESHOLDS (controller's ruling needed)

Nothing changed in the data files: no value is plainly broken (every genuine voice on its own
microphone reads "for"; nothing genuine reads "against"). Measured (cosine to the learnt base):
genuine same mic min 0.58-0.64 (median 0.73-0.76); genuine cross mic min 0.48-0.57; another
person max 0.46-0.50 (resnet34), 0.36-0.40 (CAM++); cold 0.17-0.61; imitation max 0.27-0.34;
learn steps across languages 0.37-0.62.

- **A. Prefer CAM++ (`wespeaker-voxceleb-campplus-lm`) as the print model when both hold.** Its
  gap is wider (others max 0.40 vs genuine min 0.59) and it is faster (0.06 s vs 0.13 s). Cost:
  it learnt fewer voices on the poor microphone (defect 3); the core picks the print model today
  by its own catalogue order, not by this.
- **B. resnet34: `print_low 0.25`, `print_high 0.75` (keep `same_voice 0.40`).** "For" from
  cosine 0.55 on the same microphone, "against" under 0.40. Removes the false "for" of defect 4
  (0.503 -> degree 0.51, unsure); every genuine same-mic sound still "for" (min 0.58). Cost:
  cross-microphone genuine readings need cosine 0.625 to be "for" (about half of them become
  unsure), and more cold readings fall under "against"/"resembles nobody" (cold 0.25-0.40).
- **C. CAM++: keep `0.20 / 0.70`, lower `same_voice` to 0.35.** Lets Alex's 0.37 step join the
  candidate on mic2 (defect 3). Cost: a candidate may gather an unlearnt other voice (others reach
  0.36-0.40); the check at the end only stops known voices.
- **D. Core `FOR_AT` / `AGAINST_AT` unchanged (0.60 / 0.30).** Moving them moves every model at
  once; the bounds per model are the right knob. `OTHER_MICROPHONE 0.8` held: 64 of 68 cross-mic
  readings of learnt voices were "for" with the current bounds.

## Limits

1. Synthetic voices (above). Ten styles of one model; no real microphone, real room, cough or
   distance. Nothing here measures how a real cold or a real imitator reads.
2. Intonation is blind on mic2: its 300 Hz low cut removes a man's fundamental, so
   `pitch_hz` is `None` for Alex on mic2 and the cold Alex reads 263 Hz (an octave error) instead
   of 132 Hz. Intonation habits learnt on one microphone then misfit on the other (the core only
   nudges by 0.10 inside a band, so no reading flipped because of it).
3. The CPU figures were taken on a machine about half busy with other work; a quiet machine was
   faster in Task 2 (base RTF 0.26). Speeds swing by up to 2x between runs.
4. The voice sends one chunk per call (up to 300 characters, `real/says.py:72` `sentences`): the
   first sound of a call is the whole call's synthesis. The core says sentence by sentence, so
   only a very long first sentence is slow.
5. Graphics memory per process cannot be read on Windows; the card's total is given.
6. The thinking model's time, the page's playback buffer and the network are not in the
   estimate.
7. The print and DB part ran with the core's code as it was while another implementer edited it;
   a later change to `prints.py` can change these readings (re-run the test, it takes 2-5 min).

How to run again (no stack): build the set, start the service on a free local port, then
`DOMO_SPEECH_TEST_MODELS=1 DOMO_VOICE_TRIALS_LABEL=<name> uv run --no-sync pytest
tests/e2e/test_voice.py -q -p no:cacheprovider` (Docker is needed only for the throwaway
PostgreSQL), and `python -m scripts.voice_bench.speed --pid <service pid> --label <name>`.

## STACK AND BROWSER TRIALS (second half)

Dates: 2026-11-26 (first run, branch at `c108ce69`, stopped when the stack slot was lent out) and
2026-11-26/27 (second run, branch at `20e344da` plus the test fixes of this section, images
rebuilt), then 2026-11-27 (whole `-m stack` suite and voice runs at `419f071b`, which found the
defects of the third list below; reruns after the fixes, the last at `9c8477a5`). The thinking model is the scripted one everywhere (see "Thinking model" below).

### How it runs

- `tests/e2e/test_voice_stack.py` (markers `stack` and `speech_models`, opt-in:
  `DOMO_SPEECH_TEST_MODELS=1`, `DOMO_VOICE_MODELS_DIR`, the recorded set built;
  `DOMO_VOICE_TRIALS_DEVICE=gpu|cpu`). Its stack (`voice_support.voice_stack`): `compose.yaml` +
  `compose.test.yaml` (the fake model) + `compose.gpu.yaml` for the card +
  `tests/e2e/compose.voice.yaml` (the `speech` service's `/models` bound to
  `D:\domo-voice-models\models`, where the weights already are: nothing fetched, nothing written
  on C:), project `domo-e2e-voice`, ports and networks of `tests/e2e/voice_stack.env`.
  `DOMO_VOICE_STACK_ATTACH=1` uses a stack already up under that project (the second run brought
  it up by hand, ran the module several times, and took it down with `down -v` between the GPU
  and the CPU runs and at the end).
- Each browser is Playwright's headless shell with a fake microphone
  (`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream
  --use-file-for-fake-audio-capture=<wav>%noloop`), the file built from the recorded set
  (silences and sentences, written to `D:\domo-voice-models\trials\stack`), and the microphone
  granted to the site (`permissions=["microphone"]`, as a browser keeps it once the person said
  yes). Playwright's own full Chromium cannot start on this machine (Windows refuses
  `chrome-win64\chrome.exe`, "Permission denied"); the Chrome installed on the machine can
  (`channel="chrome"`, a fresh profile of Playwright's, never the owner's).
- `tests/e2e/voice_page_probe.js`, added before the page's scripts, records on the page's clock
  every frame of the voice socket each way and when the page's speakers really play (an
  analyser on every node connected to the audio destination, peak > 0.01 every 5 ms). End of
  the spoken sentence = the page's `speech_end` minus its 0.704 s of silence (22 frames of
  32 ms); "first sound" = the first peak the speakers played after that `speech_end`.
- Results: `D:\domo-voice-models\trials\results\stack-<device>.json` (each run overwrote it;
  the copies of the runs quoted here are in the agent's notes, the figures below are complete).

### Thinking model

The scripted model of `compose.test.yaml` answered every turn ("Fake reply: <what was heard>")
and the quick judgement (its answer is unreadable, so each sentence is taken as said to him and,
when he speaks, he goes on: the designed fallback). The live key was NOT used: the stack takes a
model key only through the first page of the installation (typed into the page), and a real key
typed into a form, even by a test, is not something these trials do. So every figure below is
"without a real thinking model": a real one adds its own time to the first word, not measured
here.

### What was found

1. **The GPU image could not hear on the card (fixed, `573e09ef`, first run).** On Linux nothing
   put the CUDA libraries of the `nvidia-*` packages on the loader's path.
2. **A voice socket the browser dropped logged a traceback (fixed, `c108ce69`, first run).**
3. **Scenario (1) "no answer within 60 s" of the first run: not reproduced.** On the rebuilt
   branch the first sentence was answered aloud on the first try, at each of four fresh starts
   (GPU three times, CPU once hearing was there). The first run's page diagnostics were not
   kept, so its cause is not proven; since then the speech service's health check, the settings
   compose passes to it and its fallback from the card to the processor were all fixed. What the
   second run found instead was in the test: after a reload the page never listened by itself,
   because `--use-fake-ui-for-media-stream` accepts each request but leaves the microphone's
   permission at `prompt`, and the page (rightly) listens by itself only when the browser says
   `granted`. Fixed in the test (the context grants the microphone); with it, 10 reloads out of
   10 listened by themselves and answered (GPU 5, CPU 5).
4. **On the processor, the first start of the stack left the house deaf (defect, not fixed, see
   DEFECTS FOUND).** The speech service measured `faster-whisper-base` at RTF 0.56 (the limit is
   0.5) while the core ran its migrations and two other sessions' test databases ran: nothing
   held for hearing (`missing: ["hear"]`). Restarted alone, the service measured it again at
   0.43 and held it (at once 1), but the core had kept the first list and still said the voice
   was unavailable (`state` `unavailable`, `listening` off) until the core itself was restarted.
5. **One slow, cut first answer on the card, not reproduced.** Right after a timing of my own had
   loaded the small and turbo hearing models on the card (2.7 GB of card in use), the first
   sentence of the next run came back after 16.1 s and only its end was heard ("S'il te plaît !"
   for the whole sentence). The three other cold first sentences on the card took 3.9 to 7.6 s
   and were heard whole.
6. **The test of (4) spoke too early (fixed in the test):** its second sentence followed the
   first at once, before his voice began; it now waits `DOMO_VOICE_TRIALS_OVER_HIM_GAP` (6.5 s,
   8.5 s on the processor) so the second sentence lands while he speaks.
7. **The test of two people read a reply before it was written (fixed in the test),** and invited
   Dana a second time on a stack attached again (now it first tries her login).

### Items (1)-(8)

| Item | Result |
|---|---|
| (1) turned on once, spoken to, he answers aloud; reload: listens by itself | PASS, GPU and CPU (first answer, then 5 reloads out of 5 on each) |
| (2) a card said in words, a yes said aloud | NOT TRIED: needs a real thinking model |
| (3) a phrase agreed, given, changed, removed, a near phrase refused | NOT TRIED: needs a real thinking model |
| (4) speaking while he speaks | PASS for the lowering: the peaks of his voice went from 0.2-0.5 to 0.05-0.1 within the quarter second after the page's `speech_start`, GPU and CPU; he went on to the end lowered, `resume` after the sentence (GPU, gap 5 s). "Stops" was not tried: it is the quick judgement's answer, which the scripted model cannot give |
| (5) another voice at the owner's page | NOT TRIED: a learnt voice needs two visits hours apart, and the judging is a real model's |
| (6) "stop listening" in free words | NOT TRIED: needs a real thinking model |
| (7) the socket passes the edge | PASS: the page is the edge's `https://localhost:18473`, the socket `wss://` through Caddy, no Caddyfile change needed |
| (8) nothing on disk, no heard words in logs | PASS, GPU and CPU: no sound file in `speech`, `core`, `gatekeeper`; none of the marker words in the logs of those and `edge`; no sound in the models folder |

### Figures

Method: the module run on one stack, five runs of each measure (fresh browser each time for two
people, reloads of one browser for one person), median and worst of the runs; the GPU figures
pool two series (10 runs). Beside the trials the PC ran two other sessions' testcontainers
PostgreSQL (and their ryuk), nothing else of mine. Card: RTX 2070 8 GB; the container sees 6
cores and 16 GB (WSL's share).

**a. End of the spoken sentence to the first sound of the answer, through the whole stack**
(scripted thinking model; hearing `faster-whisper-base`, prints CAM++, voice `supertonic-3`
on the processor in both cases):

| | median | worst | best | first sentence after turning on |
|---|---|---|---|---|
| GPU (hearing on the card), n=10 | 4.55 s | 5.84 s | 3.77 s | 3.9 s, 7.6 s, and once 16.1 s (found 5) |
| CPU (all on the processor), n=5 | 7.12 s | 8.02 s | 6.65 s | 10.6 s (cold) |

Split, timed inside the speech container on the same sentence (4.1 s of speech), three calls
each: the page waits 0.70 s of silence before it ends the moment; hearing 0.15-0.43 s on the
card, 1.5-1.6 s on the processor; voice print 0.12-0.17 s (CAM++); the voice says the first
sentence of the reply in 1.0-1.9 s (it comes as one piece: the first chunk is the whole
sentence). The rest, about 1.5-2.5 s on the card and about 3 s on the processor, is the core:
the quick judgement's call, the turn and its writing, the sound sent to the page (the first
frame reaches the page about 0.5 s before it plays).

**b. Memory of the speech service** (`docker stats`; the card's figure is its whole use, 547 MB
before the stack):

| | GPU: RAM / card in use | CPU: RAM |
|---|---|---|
| idle, ready | 1.18 GB / 1 248 MB | 0.85 GB |
| after one use | 1.18 GB / 1 248 MB | 1.44 GB |
| two voices at once (peak of 5 runs) | 1.46-1.79 GB / 1 263 MB | 1.54-1.73 GB |
| small and turbo hearing also loaded (by a direct call) | 2.68 GB / 2 738 MB, then back to 1.39 GB / 993 MB once unloaded | (they do not hold on the processor) |

**c. Two people at once** (the owner and Dana, two browsers, two microphones playing their
sentence at the same moment, five runs): each got their own answer every time, 10 answers out
of 10, no `failed`, no `not_taken`, no sentence dropped.

| | first page: median / worst | second page: median / worst | one alone (median) |
|---|---|---|---|
| GPU | 6.36 s / 7.94 s | 7.07 s / 8.02 s | 4.55 s |
| CPU | 7.91 s / 8.73 s | 10.29 s / 11.47 s | 7.12 s |

The limit that remains: on the processor the hearing model holds one request at a time
(`at_once` 1), so the second person waits for the first one's hearing (about +3 s), and a third
would wait more; on the card hearing holds 7 at once and the two answers cost +2 to +2.5 s over
one alone (the voice on the processor holds 2 at once).

**d. A reload with no click, in a real browser:** the headless shell, and the Chrome installed
on this machine (Playwright's fresh profile, `channel="chrome"`, headless and with its window),
with the microphone granted: after a reload with no click and no key, the page's audio was
`running`, it listened by itself and played the answer. (Probably because Chrome lets a page that
is capturing the microphone play sound.) Left for the owner's hands: a real microphone, his own Chrome
profile, a page opened again after the browser was closed, a tab in the background.

**e. Latest run, `9c8477a5`** (scripted thinking model, images rebuilt, 4 voice stack tests passed
on each device, no failure, no rerun needed). The figures above are the first runs, kept as
history.

| | median | worst | best |
|---|---|---|---|
| Card, n=10, end of sentence to first sound | 4.14 s | 4.92 s | 3.90 s |
| Processor, n=5, same | 6.33 s | 6.69 s | 6.06 s |

The first reply on a cold processor start took 11.1 s. Two people at once: card, Alex 4.20-6.44 s
and Dana 4.95-6.84 s; processor, Alex 7.07-7.47 s and Dana 9.26-10.83 s. On the processor, hearing
was listed as normal (`faster-whisper-base`, real-time factor 0.46, not flagged slow in that run,
`at_once` 1); the figures on the processor move with the load of the machine (medians of 7.12 s,
5.59 s at `419f071b` and 6.33 s in the runs), so read them as a range, not a promise. The same
run on the card measured 7 candidates on this machine. Between those runs, an attempt after the
first fixes failed at setup (no network to build the images; then the speech service listed no
hearing on the processor, which is the defect 6 below).

### DEFECTS FOUND

1. **The core never sees a use the speech service gains while both run** (found 4 above). The
   pass that declares the house's speech service (`llm/speech/home.py`, `speech_home_pass`) stops
   once the service has listed any model; after that only `model_lists_pass` lists again, and
   only keys used in the last day whose list is a day old (`llm/refresh.relist_keys`). A service
   that could not hear at its first start (a busy machine: it measures again at its next start,
   `missing_by_speed`), or one that gets a graphics card, stays unheard by the core until the core
   restarts; the voice says it is unavailable meanwhile. Not fixed here: how often to ask the
   service again (each pass while a use is missing, or every hour) is a choice for the task
   that owns `home.py`.
2. **On a processor of 6 cores, hearing is at the edge of its limit** (RTF 0.43 to 0.56 for
   `faster-whisper-base` against 0.5): a start while the machine is busy can leave the house
   without hearing until the service starts again. Not a code defect (the limit is not changed
   here); worth a line for the owner.
3. **Not reproduced, to watch:** the slow and cut first sentence of found 5 (card, after larger
   hearing models had been loaded).

Found by the whole stack suite and the voice runs at `419f071b`, and fixed since:

4. **The house's speech key counted as a key to think with.** A person with no key of their own
   (taken off the general key) got an empty reply and the alert "no model left" instead of being
   asked for a key, and the page did not offer the key form. Fixed (`b8412eee`): every text call
   asks for the keys without the speech key. Two more places found by the review (`bfaa7f9f`): it
   could become the heir of the general key, or the general key itself when listed again; it no
   longer can.
5. **The end of the setup's model step deleted the house's speech key** (with the owner's
   abandoned attempts), so a fresh install had "hearing unavailable" until a voice session asked
   the core to read the list again. Fixed (`82a75d10`): the step leaves it alone.
6. **A slow or busy start on a processor left the service without hearing until a restart.** Fixed
   (`6d49176f`, decision X29 of the decisions file, with its cost): the fastest model that works
   is kept and marked slow, and measured again in the same start. A re-measure never takes away
   what the house has (`9c8477a5`).
7. **Tests, not the product:** the key count of one test, a reply read while still being
   written, and the start of the voice trials before the core listed all the service does; fixed
   in the tests. A sooner reread of a speech list still missing a use is also built (`0c933986`).

Limits that remain, each stated once:

- The page turns its voice off when hearing is missing and does not try again by itself; turning
  it on again works.
- The sooner reread of the speech list lasts only 10 minutes after the service's first list.
- A house busy at every re-measure moment keeps a slow verdict longer (the waits are capped at
  6 hours).
- The items (2), (3), (5) and (6) were not tried with a real thinking model: they stay on the
  owner's list below.
- One test of the whole suite (`test_sandbox`, a run that fills the container) failed once for want
  of memory on the machine while other sessions' containers ran, and passed alone.

## THE OWNER'S BY-HAND LIST

Pour le jour où l'assistant tourne chez toi :

1. Ouvre la page, allume la voix, parle : il te répond à voix haute, sans clavier ni souris.
2. Refais à la voix l'essai du deuxième canal du chantier A : la même discussion, une carte dite
   en mots, un oui dit à voix haute, un mot laissé qui arrive sans rien dire.
3. Parle pendant un appel, avec et sans casque.
4. Quelqu'un d'autre parle devant la page : il ne le prend pas pour toi.
5. Convenez d'une phrase secrète, donne-la, change-la.
6. Coupe-le au milieu d'une phrase ; parle à quelqu'un d'autre pendant qu'il parle.
7. Tout ce que les essais de voix n'ont pas pu faire avec un vrai modèle de réflexion : une carte
   dite en mots, un oui dit à voix haute, une phrase convenue puis changée, une autre voix devant
   ta page, « arrête d'écouter » dit avec tes mots. Dis-lui aussi « stop » pendant qu'il parle :
   l'arrêt complet vient du jugement rapide, que le modèle de l'essai ne savait pas donner.

**À RAPPELER AU COORDINATEUR : refaire à la voix l'essai à la main du deuxième canal du
chantier A.**
