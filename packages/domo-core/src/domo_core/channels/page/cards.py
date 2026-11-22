"""A card as a page shows it: the sentence of the card, and the buttons and links only a page has.

``action_card`` turns the card an action keeps into an ``ActionCard``, for the reader it is shown
to: its sentence comes from ``conversation.cards``, and a page adds what it alone can give, the
link shown once, the forms of a key, the buttons that someone else's yes left for the one who asked.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, cast

from domo_common.meanings import has_meaning

from domo_core.conversation.cards import action_source, card_sentence
from domo_core.texts.cards import HeadSource, KeptHead
from domo_core.texts.view import REFERENCE, Texts
from domo_core.tools.key_tools import key_check_button_of, key_new_button_of
from domo_core.tools.model_key import key_form_of, own_rekey_button_of, rekey_button_of
from domo_core.tools.spending_tools import admin_button_of, own_admin_button_of

_INVITATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class CardButton:
    """What a step of a chain left for the one who asked, as a button on the chain's card: the
    ``step`` (from 1), and the invitation whose link it shows once (``invitation``), the address
    where it opens the form for a key given again (``rekey_url``), the one where it opens the
    form of a new key (``key_new_url``), or the one where it opens the form of an admin key
    (``admin_url``)."""

    step: int
    invitation: str | None
    rekey_url: str | None
    key_new_url: str | None = None
    admin_url: str | None = None


@dataclass(frozen=True)
class ActionCard:
    """An action as the page shows it (``fragments/action_card.html``): its ``status``, the
    ``tool`` the model called, the ``text`` of its card in the reader's texts, and ``once``,
    the part that is only shown by the answer that executed it, with ``link``, the address it
    gives, if any (an invitation's link), shown as a link, or ``key_form``, the path of the
    vault's form for the model's key, shown in a frame, and ``key_status_url``, the address that
    follows it (``fragments/key_form.html``).

    What someone else's yes left for the one who asked to do is a button on their card, which only
    they are given: ``invitation`` (the invitation whose link it shows once,
    ``household.routes``), ``rekey_url`` (where it opens the form for a key given again,
    ``channels.page.key_routes``), ``key_new_url`` (where it opens the form of a new key) and
    ``admin_url`` (where it opens the form of an admin key); for a chain, one per step that left one
    (``buttons``). ``key_check_url`` is where the button of a new key of
    a server without a key checks it, for the one who asked (``tools.key_tools``). ``rights`` are
    the person's own rights that a card of the household tells the owner of, under the sentence
    ``rights_title``.

    ``head`` is the head Domo wrote for the reader in place of ``text`` (``texts.cards``), for
    an action done, refused or that failed when he chose to write those on the moment; ``source``
    what a card built for the writing of a head holds of the action (``texts.cards.HeadSource``)."""

    kind: ClassVar[str] = "action"

    status: str
    tool: str
    text: str
    once: str | None = None
    link: str | None = None
    key_form: str | None = None
    key_status_url: str | None = None
    invitation: str | None = None
    rekey_url: str | None = None
    key_new_url: str | None = None
    key_check_url: str | None = None
    admin_url: str | None = None
    rights_title: str | None = None
    rights: tuple[str, ...] = ()
    buttons: tuple[CardButton, ...] = ()
    head: str | None = None
    source: HeadSource | None = None

    @property
    def head_parts(self) -> tuple[str | None, ...]:
        """What the head of the card takes the place of: its sentence."""
        return (self.text,)


def _link_of(card_once: Mapping[str, object]) -> str | None:
    """The address a part shown once gives (``"link"``), if it is a web address."""
    link = card_once.get("link")
    if isinstance(link, str) and link.startswith(("https://", "http://")):
        return link
    return None


def _invitation_of(card: Mapping[str, object]) -> str | None:
    """The invitation a card gives the one who asked the button for (``"invitation"``), if it is
    an identifier: a page never posts anywhere else."""
    invitation = card.get("invitation")
    if isinstance(invitation, str) and _INVITATION_ID.fullmatch(invitation):
        return invitation
    return None


def card_buttons(buttons: object, confirmation_id: uuid.UUID | None) -> tuple[CardButton, ...]:
    """The buttons of the steps of a chain that a page may show, from what its card carries
    (``"buttons"``: a list of ``{"step", "invitation", "rekey", "key_new", "admin_key"}``,
    ``steps.StepButton.as_json``),
    in their order. Each value is checked as for a button of one action: an invitation that is an
    identifier (``_invitation_of``), a provider's id whose form opens for the yes given to the
    request ``confirmation_id`` (``rekey_button_of``), or, for a chain that ran at once (no
    request), for whoever posts as the barrier lets them (``own_rekey_button_of``), the page of
    a new key (``key_new_button_of``), or the form of an admin key, as for a key given again
    (``admin_button_of``, ``own_admin_button_of``). Anything else is left out: a page never posts
    anywhere else."""
    if not isinstance(buttons, list):
        return ()
    shown: list[CardButton] = []
    for item in cast(list[object], buttons):
        if not isinstance(item, dict):
            continue
        button = cast(dict[str, object], item)
        step = button.get("step")
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            continue
        invitation = _invitation_of(button)
        rekey_url = (
            own_rekey_button_of(button)
            if confirmation_id is None
            else rekey_button_of(button, confirmation_id)
        )
        key_new_url = key_new_button_of(button, confirmation_id)
        admin_url = (
            own_admin_button_of(button)
            if confirmation_id is None
            else admin_button_of(button, confirmation_id)
        )
        if any(url is not None for url in (invitation, rekey_url, key_new_url, admin_url)):
            shown.append(CardButton(step, invitation, rekey_url, key_new_url, admin_url))
    return tuple(shown)


def _rights_of(card: Mapping[str, object], texts: Texts) -> tuple[str | None, tuple[str, ...]]:
    """The rights a card tells (``"rights"``: a sentence of the catalog, and the summaries of the
    rights), if it is what the tools put there."""
    rights = card.get("rights")
    if not isinstance(rights, dict):
        return None, ()
    shown = cast(dict[str, object], rights)
    key, items = shown.get("key"), shown.get("items")
    if not isinstance(key, str) or not has_meaning(key) or not isinstance(items, list):
        return None, ()
    summaries = tuple(item for item in cast(list[object], items) if isinstance(item, str))
    try:
        title = texts(key)
    except (KeyError, IndexError, ValueError):
        return None, ()
    return (title, summaries) if summaries else (None, ())


def action_card(
    *,
    tool_name: str,
    status: str,
    card: Mapping[str, object],
    texts: Texts,
    card_once: Mapping[str, object] | None = None,
    for_requester: bool,
    confirmation_id: uuid.UUID | None = None,
    head: KeptHead | None = None,
    holed: Texts | None = None,
) -> ActionCard:
    """The card of an action, for the page. A card whose sentence cannot be read says what the
    status says. ``card_once`` is the part shown only by the answer that executed it: its
    sentence, and the address it gives (``"link"``) or the vault form it opened (``"form_path"``
    and ``"status_url"``, see ``tools.model_key.key_form_of``), if any.

    The buttons that someone else's yes left for the one who asked (``"invitation"``: show the
    link, ``"rekey"``: open the form for the key), and those the steps of a chain left
    (``"buttons"``, ``card_buttons``), are on the card of ``for_requester`` only: the others (the
    one who said yes) see what was done, not the button. Every caller says which one it is
    showing the card to: no default lets a button through. The key's button names the request it
    is on the card of (``confirmation_id``): the form opens for the yes given to it. An action that
    ran at once, with no request (``confirmation_id`` ``None``), on a channel that could not open
    its form (the API's), has the button that opens it for whoever posts as the barrier lets them
    (``own_rekey_button_of``, ``own_admin_button_of``), as a chain that ran at once does.

    ``head`` is the head kept for the reader (``texts.cards``), in place of its sentence while it
    still says what this card says to them in their language (``KeptHead.shown_on``). With
    ``holed`` (the reader's texts), the card is the one its head is written from: its sentence in
    ``texts`` (the reference texts) with the judge's reason and its dates left as holes, which
    its ``source`` holds in the reader's words."""
    text = card_sentence(card, texts, holed=holed is not None) or texts(f"action.card.{status}")
    once = None if card_once is None else card_sentence(card_once, texts)
    link = None if card_once is None or once is None else _link_of(card_once)
    key_form = None if card_once is None or once is None else key_form_of(card_once)
    rights_title, rights = _rights_of(card, texts)
    return ActionCard(
        status=status,
        tool=tool_name,
        text=text,
        once=once,
        link=link,
        key_form=None if key_form is None else key_form[0],
        key_status_url=None if key_form is None else key_form[1],
        invitation=_invitation_of(card) if for_requester else None,
        rekey_url=(
            (
                own_rekey_button_of(card)
                if confirmation_id is None
                else rekey_button_of(card, confirmation_id)
            )
            if for_requester
            else None
        ),
        key_new_url=key_new_button_of(card, confirmation_id) if for_requester else None,
        key_check_url=key_check_button_of(card) if for_requester else None,
        admin_url=(
            (
                own_admin_button_of(card)
                if confirmation_id is None
                else admin_button_of(card, confirmation_id)
            )
            if for_requester
            else None
        ),
        rights_title=rights_title,
        rights=rights,
        buttons=card_buttons(card.get("buttons"), confirmation_id) if for_requester else (),
        head=None if head is None or holed is not None else _shown_head(head, status, card, texts),
        source=None if holed is None else action_source(card, holed),
    )


def _shown_head(
    head: KeptHead, status: str, card: Mapping[str, object], texts: Texts
) -> str | None:
    """``head`` when the card of an action (``card``, with ``status``) still says, to the reader
    of ``texts``, what it said when the head was written (``KeptHead.shown_on``)."""
    written_from = action_card(
        tool_name="",
        status=status,
        card=card,
        texts=REFERENCE,
        for_requester=False,
        holed=texts,
    )
    return head.shown_on(written_from, texts.language)
