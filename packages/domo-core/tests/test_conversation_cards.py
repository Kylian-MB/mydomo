"""A card is said in words by the conversation, with no page."""

from domo_core.conversation.cards import card_sentence
from domo_core.texts.view import CatalogTexts, t


def test_a_card_is_said_in_words_without_a_page():
    texts = CatalogTexts("fr")
    card = {"key": "action.card.done.note.leave", "params": {"name": "Alex"}}
    assert card_sentence(card, texts) == t("action.card.done.note.leave", "fr", name="Alex")
    assert card_sentence({"key": "no.such.key", "params": {}}, texts) is None
