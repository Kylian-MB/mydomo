"""The words of a reply cut into sentences to be said (spec 6.7)."""

from domo_core.channels.voice.sentences import (
    FIRST_PIECE_CHARS,
    MAX_CHARS,
    MIN_CHARS,
    Fence,
    SentenceCutter,
    speakable,
)


def test_sentences_come_out_as_soon_as_they_end():
    cutter = SentenceCutter()
    assert cutter.feed("Bonjour, je suis ") == []
    assert cutter.feed("là pour toi. Et je") == ["Bonjour, je suis là pour toi."]
    assert cutter.feed(" reste ici ! ") == ["Et je reste ici !"]
    assert cutter.flush() == []


def test_a_number_and_an_initial_do_not_cut():
    cutter = SentenceCutter()
    out = cutter.feed("Il fait 3.5 degrés ce soir selon J. Martin et le bulletin. ")
    assert out == ["Il fait 3.5 degrés ce soir selon J. Martin et le bulletin."]
    # A number split across two pieces of words is not cut either.
    cutter = SentenceCutter()
    assert cutter.feed("Le prix est de 3.") == []
    assert cutter.feed("5 euros au total. ") == ["Le prix est de 3.5 euros au total."]
    # A time is not cut at its colon.
    cutter = SentenceCutter()
    assert cutter.feed("Rendez-vous à 12:30 devant la porte. ") == [
        "Rendez-vous à 12:30 devant la porte."
    ]


def test_a_short_piece_waits_for_the_next_one_unless_flushed():
    cutter = SentenceCutter()
    assert len("Oui.") < MIN_CHARS
    assert cutter.feed("Oui. ") == []
    assert cutter.feed("Je m'en occupe tout de suite. ") == ["Oui. Je m'en occupe tout de suite."]
    cutter = SentenceCutter()
    assert cutter.feed("D'accord. ") == []
    assert cutter.flush() == ["D'accord."]


def test_the_first_piece_of_a_reply_is_cut_early_at_a_comma_then_whole_sentences():
    # Final fix wave C: the voice makes a whole piece before any sound, so the first piece of a
    # reply goes as soon as a comma comes after about FIRST_PIECE_CHARS; then whole sentences.
    cutter = SentenceCutter(early_first=True)
    first = "Je regarde ce que tu as dans ton agenda demain,"
    assert len(first) >= FIRST_PIECE_CHARS
    assert cutter.feed(first + " et je te") == [first]
    rest = "et je te dis tout dans une minute, promis, avec les heures. "
    assert cutter.feed(rest[len("et je te") :]) == [rest.strip()]
    later = "Ensuite, demain matin, il y a ta réunion avec ton équipe, à dix heures. "
    assert cutter.feed(later) == [later.strip()]
    assert cutter.flush() == []


def test_the_early_first_piece_waits_for_enough_words_and_keeps_numbers_whole():
    # A comma too early: the sentence end comes first and is the piece, as today.
    cutter = SentenceCutter(early_first=True)
    assert cutter.feed("Bonjour, je suis là pour toi. ") == ["Bonjour, je suis là pour toi."]
    # Never inside a number: "21,5" is no place to cut.
    cutter = SentenceCutter(early_first=True)
    text = "La température dehors est montée jusqu'à 21,5 degrés cet après-midi. "
    assert cutter.feed(text) == [text.strip()]
    # A comma not yet followed by anything waits: the digits of a number may come next.
    cutter = SentenceCutter(early_first=True)
    assert cutter.feed("La température dehors est montée jusqu'à 21,") == []
    assert cutter.feed("5 degrés. ") == ["La température dehors est montée jusqu'à 21,5 degrés."]


def test_without_the_option_nothing_is_cut_early():
    cutter = SentenceCutter()
    text = "Je regarde ce que tu as dans ton agenda demain, et je te dis tout. "
    assert cutter.feed(text) == [text.strip()]


def test_a_long_sentence_is_cut_at_a_comma():
    cutter = SentenceCutter()
    text = "a" * 150 + ", " + "b" * 200
    assert len(text) > MAX_CHARS
    assert cutter.feed(text) == ["a" * 150 + ","]
    assert cutter.flush() == ["b" * 200]


def test_a_long_sentence_without_a_comma_is_cut_at_a_space():
    cutter = SentenceCutter()
    words = " ".join(["mot"] * 120)
    out = cutter.feed(words)
    assert out and all(len(s) <= MAX_CHARS for s in out)
    assert " ".join(out + cutter.flush()) == words


def test_list_marks_and_emphasis_are_not_said():
    assert speakable("- une pomme") == "une pomme"
    assert speakable("1. une poire") == "une poire"
    assert speakable("Un **très** bon _choix_") == "Un très bon choix"
    assert speakable("Regarde https://example.org/a?b=1 maintenant") == "Regarde maintenant"
    assert speakable("Voir [la page](https://example.org/x) ici") == "Voir la page ici"
    assert speakable("## Titre\nVoir `rm -rf` ici") == "Titre Voir ici"
    assert speakable("snake_case reste") == "snake_case reste"
    assert speakable("https://example.org") == ""


def test_the_end_flushes_what_is_left():
    cutter = SentenceCutter()
    assert cutter.feed("Il me reste encore un peu de texte") == []
    assert cutter.flush() == ["Il me reste encore un peu de texte"]
    assert cutter.flush() == []


def test_french_spaces_before_marks_do_not_cut_early():
    cutter = SentenceCutter()
    # "Tu viens ?" is too short to be a sentence of its own: it goes with what follows.
    assert cutter.feed("Tu viens ? Oui.") == ["Tu viens ? Oui."]
    cutter = SentenceCutter()
    assert cutter.feed("Tu viens avec nous ce soir ? ") == ["Tu viens avec nous ce soir ?"]


def test_a_line_break_cuts():
    cutter = SentenceCutter()
    assert cutter.feed("Voici ce que j'ai trouvé\nPremier point à voir\n") == [
        "Voici ce que j'ai trouvé",
        "Premier point à voir",
    ]


def test_an_initial_is_a_capital_before_a_name_only():
    cutter = SentenceCutter()
    assert cutter.feed("Il y en a bien assez. Allons-y vite. ") == [
        "Il y en a bien assez.",
        "Allons-y vite.",
    ]
    cutter = SentenceCutter()
    assert cutter.feed("Prends l'option A. puis dis-moi ce que tu veux. ") == [
        "Prends l'option A.",
        "puis dis-moi ce que tu veux.",
    ]
    cutter = SentenceCutter()
    assert cutter.feed("Choose option A. then tell me which one. ") == [
        "Choose option A.",
        "then tell me which one.",
    ]
    cutter = SentenceCutter()
    assert cutter.feed("Ask J. Martin about it today. ") == ["Ask J. Martin about it today."]
    # An initial at the end of a piece waits for the next word.
    cutter = SentenceCutter()
    assert cutter.feed("Ask the owner, J. ") == []
    assert cutter.feed("Martin, today. ") == ["Ask the owner, J. Martin, today."]


def test_code_tables_and_emoji_are_not_said():
    assert speakable("Voici :\n```python\nprint(1)\n```\nVoilà") == "Voici : Voilà"
    assert speakable("Avant ```code sans fin") == "Avant"
    assert speakable("| a | b |\n|---|---|\n| 1 | 2 |") == "a b 1 2"
    assert speakable("Super \U0001f389 travail \U0001f44d") == "Super travail"


def test_a_code_block_that_spans_sentences_is_not_said():
    fence = Fence()
    assert fence.outside("Regarde ça. ```py") == "Regarde ça. "
    assert fence.outside("x = 1. y = 2.") == ""
    assert fence.outside("```\nEt voilà.") == "\nEt voilà."


def test_a_fence_of_four_backticks_leaves_nothing_spoken():
    assert speakable("````\ncode\n````") == ""
    fence = Fence()
    assert fence.outside("````\ncode ici.\n````") == ""
    assert not fence.open
    assert fence.outside("```x") == ""
    assert fence.open


def test_inline_code_is_followed_across_sentences():
    fence = Fence()
    assert fence.outside("Lance `rm -rf.") == "Lance "
    assert fence.outside(" puis encore` maintenant.") == " maintenant."
    assert fence.outside("Une `ligne` finie. Et `une autre\nsuite") == "Une  finie. Et \nsuite"


def test_a_backtick_used_as_an_apostrophe_hides_nothing():
    fence = Fence()
    assert fence.outside("Voici l`arbre de la rue.") == "Voici l'arbre de la rue."
    # An opening backtick with no closing one in this piece or the next: the next piece is said.
    assert fence.outside("Il dit `salut") == "Il dit "
    assert fence.outside(" tout le monde ici.") == " tout le monde ici."
    assert fence.outside("Et la suite aussi.") == "Et la suite aussi."


def test_the_fence_can_be_started_again():
    fence = Fence()
    assert fence.outside("avant ```py") == "avant "
    assert fence.open
    fence.reset()
    assert not fence.open
    assert fence.outside("après") == "après"
