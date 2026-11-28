"""Every number of the voice clue in one place (spec 6.5).

The bounds that turn a cosine similarity into a degree are not here: they are measured by the
speech service for each print model and read from it (``llm.speech.client.SpeechClient.
print_thresholds``). What follows are the choices of the core: where a degree weighs for or
against, how long a print takes to learn, how fast it moves. They are starting values, to be
measured again in the trials of the voice.
"""

from datetime import timedelta
from typing import Final

from domo_core.llm.speech.client import PrintThresholds

# The bounds of a made-up print model: for tests and trials only. A real print model's come from
# its service; with none, the voice is "not learned yet", never "against".
STARTING_BOUNDS: Final = PrintThresholds(print_low=0.25, same_voice=0.50, print_high=0.75)

# The degrees (0..1): from ``FOR_AT`` the voice weighs for; from ``AGAINST_AT`` to ``FOR_AT`` it
# is unsure (a low degree, not against); below ``AGAINST_AT``, with the voice learnt, against.
FOR_AT: Final = 0.60
AGAINST_AT: Final = 0.30

# Under ``MIN_SECONDS`` of sound a voice cannot be judged: its degree is capped at ``SHORT_CAP``
# and it is never against.
MIN_SECONDS: Final = 1.5
SHORT_CAP: Final = 0.40

# Utterances this close (seconds) add up into one print.
JOIN_WITHIN_S: Final = 30

# A voice is learnt from ``LEARN_UTTERANCES`` like utterances heard on ``LEARN_VISITS`` visits, a
# visit being a time ``VISIT_GAP`` after the last utterance.
LEARN_UTTERANCES: Final = 3
LEARN_VISITS: Final = 2
VISIT_GAP: Final = timedelta(hours=1)
# A candidate starts again from a new voice after this many unlike utterances in a row (the
# owner, 2026-11-28: one unlike sentence between like ones is not counted, nothing is thrown away),
# or at once on a clearly far one (under the print model's ``print_low``: "oui, tout de suite").
RESTART_AFTER_UNLIKE: Final = 2

# How far a print moves toward a voice it recognises: the recent one fast, the base slowly.
BASE_WEIGHT: Final = 0.02
RECENT_WEIGHT: Final = 0.20

# A print kept for a cold lives this long after its last update.
PASSING_LIVES: Final = timedelta(days=3)

# What the prints of another microphone are worth (their degree is multiplied by it).
OTHER_MICROPHONE: Final = 0.8

# Intonation fits when the pitch and the rate are within this many spreads of the habits.
INTONATION_SPREADS: Final = 2.0
# Intonation counts in the degree by at most this much (up when it fits, down when it does not):
# microphones differ, so the voice alone is not the only means of recognising a person.
INTONATION_ADJUST: Final = 0.10
# A habit of pitch or rate starts to spread at this part of its first value and never goes under
# the floor (a part of the mean): a spread of nothing would make every voice a misfit.
HABIT_FIRST_SPREAD: Final = 0.15
HABIT_SPREAD_FLOOR: Final = 0.05

# A voice nobody knows is forgotten this long after it was last heard.
UNKNOWN_LIVES: Final = timedelta(days=30)
