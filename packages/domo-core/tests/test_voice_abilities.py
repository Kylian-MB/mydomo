"""What the voice channel can do, and how the model is told his words are said aloud."""

from domo_core.channels.api.auth import API
from domo_core.channels.page.abilities import PAGE
from domo_core.channels.voice.abilities import VOICE, VOICE_CHANNEL
from domo_core.conversation.channel import NOBODY_LISTENS, Abilities
from domo_core.prompts import abilities_line

_SPOKEN = (
    "Your words are said aloud here: short sentences, no lists, no formatting, nothing that only "
    "reads well. What is shown once stays on their page: say it is there; read it aloud only if "
    "they ask, after telling them once that the room can hear it. A secret is never said aloud."
)
_PAGE_LINE = (
    "How you are heard here. A request that waits is shown with its button. What is shown once "
    "(a link, a code) is shown here, to them alone. A key is typed in a safe form opened here. A "
    "proof can be given here by: passkey, password."
)
_API_LINE = (
    "How you are heard here. There is no button here: a yes is said in words, and what takes a "
    "button waits for their page. What is shown once (a link, a code) is shown here, to them "
    "alone. A key is never said or typed here: it is typed only in the safe form on their page, "
    "where it waits for them. Whenever you speak of that form, say it is on their page, not "
    "here. A proof can be given here by: password."
)


def test_the_voice_has_no_button_no_screen_and_hears_what_he_says_on_his_own() -> None:
    assert VOICE_CHANNEL == "voice"
    assert not VOICE.buttons
    assert not VOICE.shows_once
    assert not VOICE.safe_screen
    assert VOICE.hears_unasked
    assert VOICE.proof_kinds == frozenset({"spoken_phrase"})
    assert VOICE.spoken


def test_the_voice_line_says_his_words_are_said_aloud() -> None:
    line = abilities_line(VOICE)
    assert line.endswith(" " + _SPOKEN)
    assert "A proof can be given here by: spoken_phrase." in line


def test_the_line_of_the_page_and_of_the_api_did_not_move() -> None:
    assert abilities_line(PAGE) == _PAGE_LINE
    assert abilities_line(API) == _API_LINE
    assert not PAGE.spoken
    assert not API.spoken


def test_nobody_listens_still_reads_as_before() -> None:
    assert Abilities(False, False, False, False, frozenset()) == NOBODY_LISTENS
    assert abilities_line(NOBODY_LISTENS).startswith("Nobody is listening yet:")
    assert abilities_line(NOBODY_LISTENS, arrived=True).startswith("Your reply may reach them")
