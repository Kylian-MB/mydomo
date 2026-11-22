"""Every text shown to someone goes through a ``Texts`` (texts/view.py), not a language.

``CatalogTexts`` reads the fixed catalogs, as ``t`` does; ``REFERENCE`` is the English one, what
models read, its dates in UTC (``plain_date``), its amounts as an English reader reads them. A
stored card no longer keeps a date already put in words: it keeps it in ``"dates"`` (ISO 8601),
and the card puts it in words when it is shown, with ``texts.date``; a card stored before still
reads.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from domo_common.i18n import t, template
from domo_common.meanings import SIGN, all_keys, default_text, holes
from domo_core.channels.page.cards import action_card
from domo_core.texts.formatting import plain_date
from domo_core.texts.view import REFERENCE, CatalogTexts, RecueilTexts, Texts
from domo_core.tools.household_admin import card_date


class _MarkedTexts:
    """The English catalog, but a date says how it was asked for: a test sees which went through
    ``date``."""

    language: str = "en"

    def __call__(self, key: str, /, **params: str) -> str:
        return t(key, "en", **params)

    def template(self, key: str, /) -> str:
        return template(key, "en")

    def date(self, moment: date | datetime, *, time: bool = True) -> str:
        if not isinstance(moment, datetime):
            return f"<{moment.isoformat()}|{'time' if time else 'day'}>"
        return f"<{moment.astimezone(UTC).isoformat()}|{'time' if time else 'day'}>"

    def amount(self, value: Decimal, currency: str) -> str:
        return f"<{value} {currency}>"


def _invite_card(params: dict[str, str], **more: object) -> dict[str, object]:
    return {"key": "action.card.done.household.invite", "params": params, **more}


def test_catalog_texts_read_like_t():
    assert CatalogTexts("fr")("login.submit") == t("login.submit", "fr")
    assert CatalogTexts("en")("chat.send") == t("chat.send", "en")
    assert CatalogTexts("fr").language == "fr"
    assert CatalogTexts("fr")("setup.owner.password_hint", min="12") == t(
        "setup.owner.password_hint", "fr", min="12"
    )
    # French has a default phrase for every text, as English does.
    assert CatalogTexts("fr")("chat.send") == t("chat.send", "fr") != t("chat.send", "en")


def test_catalog_texts_is_a_texts():
    texts: Texts = CatalogTexts("en")
    assert texts.date(datetime(2026, 11, 6, 14, 3, tzinfo=UTC)) == "2026-11-06 14:03 UTC"
    # A day alone is that very day.
    assert texts.date(date(2026, 11, 20)) == "2026-11-20"


def test_reference_is_english():
    assert REFERENCE.language == "en"
    assert REFERENCE("chat.send") == t("chat.send", "en")
    assert CatalogTexts("en") == REFERENCE


def test_plain_date_keeps_todays_format():
    paris = timezone(timedelta(hours=1))
    moment = datetime(2026, 11, 6, 15, 3, 59, tzinfo=paris)
    assert plain_date(moment) == "2026-11-06 14:03 UTC"
    assert plain_date(moment, time=False) == "2026-11-06"
    late = datetime(2026, 11, 7, 0, 30, tzinfo=paris)
    assert plain_date(late, time=False) == "2026-11-06"


def test_an_amount_is_said_with_its_currency_the_language_s_way():
    # The decimal comma is the language's (Babel), never a rule of the code.
    assert REFERENCE.amount(Decimal("20.0000"), "USD") == "$20.00"
    assert REFERENCE.amount(Decimal("0.50"), "EUR") == "€0.50"
    assert REFERENCE.amount(Decimal("0.0012"), "USD") == "$0.0012"
    assert CatalogTexts("fr").amount(Decimal("0.50"), "EUR") == "0,50 €"


def test_a_stored_card_date_is_formatted_when_shown():
    card = _invite_card({"name": "Alex"}, dates={"expires": "2026-11-06T14:03:00+00:00"})
    shown = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=_MarkedTexts(),
        for_requester=True,
    )
    assert shown.text == t(
        "action.card.done.household.invite",
        "en",
        name="Alex",
        expires="<2026-11-06T14:03:00+00:00|time>",
    )
    in_french = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=CatalogTexts("fr"),
        for_requester=True,
    )
    assert in_french.text == t(
        "action.card.done.household.invite", "fr", name="Alex", expires="2026-11-06 14:03 UTC"
    )


def test_a_stored_day_is_shown_without_a_time():
    card = {
        "key": "action.card.done.note.keep",
        "params": {"name": "Sam"},
        "dates": {"until": "2026-11-20"},
    }
    shown = action_card(
        tool_name="note_keep",
        status="done",
        card=card,
        texts=_MarkedTexts(),
        for_requester=True,
    )
    # A day is given as the day it is: at the time of any house, it stays the same day.
    assert shown.text == t("action.card.done.note.keep", "en", name="Sam", until="<2026-11-20|day>")
    assert action_card(
        tool_name="note_keep", status="done", card=card, texts=REFERENCE, for_requester=True
    ).text == t("action.card.done.note.keep", "en", name="Sam", until="2026-11-20")


def test_an_old_stored_card_still_reads():
    card = _invite_card({"name": "Alex", "expires": "2026-11-06 14:03 UTC"})
    shown = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=_MarkedTexts(),
        for_requester=True,
    )
    assert shown.text == t(
        "action.card.done.household.invite", "en", name="Alex", expires="2026-11-06 14:03 UTC"
    )


def test_a_card_whose_date_does_not_read_says_its_status():
    card = _invite_card({"name": "Alex"}, dates={"expires": "not a date"})
    shown = action_card(
        tool_name="household_invite",
        status="done",
        card=card,
        texts=REFERENCE,
        for_requester=True,
    )
    assert shown.text == t("action.card.done", "en")


def test_a_tool_keeps_a_date_as_iso_in_utc():
    paris = timezone(timedelta(hours=1))
    kept = card_date(datetime(2026, 11, 6, 15, 3, tzinfo=paris))
    assert kept == "2026-11-06T14:03:00+00:00"
    card = _invite_card({"name": "Alex"}, dates={"expires": kept})
    shown = action_card(
        tool_name="household_invite", status="done", card=card, texts=REFERENCE, for_requester=True
    )
    # What the card said when the date was kept in words.
    assert shown.text == t(
        "action.card.done.household.invite", "en", name="Alex", expires="2026-11-06 14:03 UTC"
    )


def _recueil(language: str, found: dict[str, str] | None = None) -> RecueilTexts:
    """What a reader of ``language`` reads when Domo wrote ``found`` for them (nothing else)."""
    return RecueilTexts(
        language=language, zone=ZoneInfo("UTC"), found=found or {}, personal=frozenset()
    )


def test_a_key_never_written_shows_its_default_phrase():
    key = "action.card.done.profile.rename"
    french = _recueil("fr")
    assert french(key, name="Alex") == t(key, "fr", name="Alex")
    assert french.template(key) == default_text(key, "fr") and "{name}" in french.template(key)
    assert _recueil("fr-CA")("chat.send") == t("chat.send", "fr")
    # A language with no default phrases of its own reads the English ones.
    assert _recueil("de")(key, name="Alex") == t(key, "en", name="Alex")
    assert {key} <= french.asked
    # A key with no meaning still shows the sign.
    assert french("no.such.key") == SIGN and french.template("no.such.key") == SIGN


def test_a_written_text_wins_over_the_default():
    key = "action.card.done.profile.rename"
    texts = _recueil("fr", {key: "Ton nom est maintenant {name}.", "chat.send": "Hop"})
    assert texts(key, name="Alex") == "Ton nom est maintenant Alex."
    assert texts("chat.send") == "Hop"
    assert texts("chat.title") == t("chat.title", "fr")


def test_no_page_text_is_the_sign_when_nothing_is_written():
    french = _recueil("fr")
    for key in all_keys():
        shown = french(key, **dict.fromkeys(holes(key), "x"))
        assert shown and shown != SIGN, key
