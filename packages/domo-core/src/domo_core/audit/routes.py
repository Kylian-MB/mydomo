"""The audit page: the latest entries of the log, and whether its hash chain still verifies.

Owner only. The page marks the verdict in the HTML with ``data-chain-status="ok"`` or
``data-chain-status="broken"`` (the end-to-end tests read it, whatever the language), and each
entry with ``data-entry-id``. The whole chain is verified at every visit, not only the entries
that are shown: the cost grows with the log, which is fine at the scale of a household.

Each entry is one line in Domo's words (spec 4.5): the text of its kind of line,
``audit.line.<action>`` (``audit.line.other`` for an action that has none), with who did it
(``who``: the person's name, ``audit.who.system`` for Domo himself, ``audit.who.household``
for the household (a tool of the house its trigger launched), a sign for someone the
household does not know), when (``when``: in the reader's words, at the time of the house) and the
facts of its details that its kind of line shows (``LINE_FACTS``): names, numbers, days, amounts,
and the words of a decision. A limit taken away has its own line (``spending.limit_removed``),
never one that reads as a limit set. Never the raw type or the JSON of the details, which stay in
the database (the chain is unchanged), and never a secret, a reference to one or the address of a
form: the facts are read from details that hold none. A fact that is missing, or that is not what
it should be (hand-edited data), is a sign, and so is a hole of a kept text that the page has no
value for (a text kept for an older line): a line never breaks the page. The lines are read from
the texts Domo wrote, never written on the moment: showing the page costs no call (the reader's
own version of the lines is written in the background, as for any page).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Literal, cast
from uuid import UUID

from domo_common.meanings import SIGN, has_meaning
from domo_common.text_rules import HOLE
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from domo_core.accounts.models import Person
from domo_core.audit.log import details_as_text, readable_ts, verify_audit_chain
from domo_core.audit.models import AuditEntry
from domo_core.naming import DEFAULT_NAME, name_shown
from domo_core.policy.settings import load_policy_settings
from domo_core.texts.formatting import babel_locale
from domo_core.texts.view import Texts
from domo_core.web.deps import get_session, require_owner
from domo_core.web.language import request_texts
from domo_core.web.templating import render

router = APIRouter()

AUDIT_PATH = "/audit"

# How many of the latest entries the page shows.
AUDIT_PAGE_SIZE = 200

# How a fact is said: as it is written (a name, a model, an address), a count, the name of a
# person (by their id), a day, an amount in US dollars, an amount in the currency of the details
# (``currency``), the name of a language (by its tag), or the words of a decision
# (``audit.<kind>.<value>``: ``outcome``, ``verdict``, ``barrier``).
type FactKind = Literal[
    "text", "count", "person", "day", "usd", "amount", "language", "outcome", "verdict", "barrier"
]

# The facts each kind of line shows, besides who and when: for each hole, the detail it is read
# from and how it is said. Never a detail that holds a secret, a reference to one or a form.
LINE_FACTS: Final[Mapping[str, Mapping[str, tuple[str, FactKind]]]] = {
    "auth.password_changed": {"count": ("revoked_sessions", "count")},
    "provider.added": {"host": ("host", "text"), "count": ("models", "count")},
    "key.started": {"host": ("host", "text")},
    # The address a tool of Domo's sent a request to: its host alone, never a path. A request
    # refused before or instead of being sent has its own line (``LINE_WHEN``), with its code.
    "madetool.out_done": {"host": ("host", "text")},
    "madetool.out_refused": {"host": ("host", "text"), "code": ("refused", "text")},
    # Going out (step 2): an address by its host, an account by its name; never a path, a value
    # nor a form's link.
    "outward.address_opened": {"host": ("host", "text")},
    "outward.address_closed": {"host": ("host", "text")},
    "outward.account_asked": {"name": ("name", "text")},
    "outward.account_removed": {"name": ("name", "text")},
    "outward.account_shared": {"name": ("name", "text")},
    "key.added": {"host": ("host", "text"), "count": ("models", "count")},
    "chat.turn": {"model": ("model", "text"), "cost": ("cost_usd", "usd")},
    "catalog.refreshed": {"count": ("models", "count")},
    "spending.real_read": {"count": ("days", "count")},
    "spending.limit_set": {"amount": ("amount", "amount")},
    "models.level_set": {"model": ("model", "text")},
    "policy.decision": {"decision": ("decision", "barrier")},
    "judge.verdict": {"decision": ("verdict", "verdict")},
    "confirmation.decided": {"decision": ("outcome", "outcome")},
    "chain.continued": {"count": ("steps", "count")},
    "chain.paused": {"step": ("step", "count")},
    "chain.arrived": {"step": ("step", "count")},
    "chain.resumed": {"name": ("arrived", "person")},
    "chain.stopped": {"step": ("step", "count")},
    "rights.paused": {"name": ("person_id", "person"), "count": ("count", "count")},
    "rights.writer_timeout": {"count": ("seconds", "count")},
    "household.invited": {"name": ("name", "text")},
    "household.invitation_cancelled": {"name": ("name", "text")},
    "household.link_given_again": {"name": ("name", "text")},
    "household.bring_back_offered": {"name": ("person_id", "person")},
    "household.role_changed": {"name": ("name", "text")},
    "household.stay_changed": {"name": ("name", "text")},
    "household.removed": {"name": ("name", "text")},
    "household.ownership_transferred": {"name": ("to_name", "text")},
    "successors.death_noted": {"name": ("name", "text")},
    "successors.death_undone": {"name": ("name", "text")},
    "note.left": {"name": ("recipient", "person")},
    "note.withdrawn": {"name": ("recipient_id", "person")},
    "note.kept": {"name": ("recipient_id", "person"), "day": ("until", "day")},
    "notes.erased": {"name": ("recipient", "person"), "count": ("count", "count")},
    "texts.written": {"count": ("count", "count"), "language": ("language", "language")},
    # Whose texts, how many, and the language: never a writing note nor an indication.
    "texts.rewrite_asked": {"count": ("count", "count"), "name": ("person", "person")},
    "texts.version_rewrite_asked": {
        "count": ("count", "count"),
        "language": ("language", "language"),
    },
    "texts.restored": {"count": ("count", "count"), "name": ("person", "person")},
    "texts.language_changed": {
        "name": ("person", "person"),
        "language": ("language", "language"),
    },
    "texts.note_changed": {"name": ("person", "person")},
    # How many memories: never what they say.
    "memory.forgotten": {"count": ("count", "count")},
    "memory.kept_looked": {"count": ("count", "count")},
    # About whom, and how many lines: never what reached them.
    "delivery.looked": {"name": ("about", "person"), "count": ("count", "count")},
    # About whom, and how many facts: never what a fact says.
    "trust.looked": {"name": ("about", "person"), "count": ("count", "count")},
    "trust.corrected": {"name": ("about", "person")},
    # Which microphone, and for how long: never a word of the voice.
    "voice.stranger_paused": {"name": ("microphone", "text"), "count": ("minutes", "count")},
}

# A kind of line said otherwise when one of its details is a text: that detail, and the line said
# then (``audit.line.<line>``, its facts in ``LINE_FACTS``). A request out that was refused was
# never sent: it never reads "sent".
LINE_WHEN: Final[Mapping[str, tuple[str, str]]] = {
    "madetool.out_done": ("refused", "madetool.out_refused"),
}

_PERSON_PREFIX: Final = "person:"
_CENT: Final = Decimal("0.01")


@dataclass(frozen=True)
class AuditRow:
    """One entry, ready to be shown: its id and its line."""

    id: int
    line: str


@dataclass(frozen=True)
class _Reader:
    """What a line is said with: the reader's texts and the names of the household."""

    texts: Texts
    names: Mapping[UUID, str]
    domo: str | None = None

    def person(self, value: object) -> str:
        """The name of the person ``value`` names (``person:<id>``, or the id alone)."""
        if not isinstance(value, str):
            return SIGN
        try:
            person_id = UUID(value.removeprefix(_PERSON_PREFIX))
        except ValueError:
            return SIGN
        return self.names.get(person_id, SIGN)

    def who(self, actor: str) -> str:
        if actor == "system":
            return self.domo or self.texts("audit.who.system")
        if actor == "household":
            # A tool of the house its trigger launched: nobody's rights were judged.
            return self.texts("audit.who.household")
        return self.person(actor) if actor.startswith(_PERSON_PREFIX) else SIGN

    def when(self, ts: datetime | None) -> str:
        return SIGN if ts is None else self.texts.date(ts)

    def fact(self, details: Mapping[str, object], detail: str, kind: FactKind) -> str:
        value = details.get(detail)
        match kind:
            case "person":
                return self.person(value)
            case "text":
                return value if isinstance(value, str) and value.strip() else SIGN
            case "count":
                return (
                    str(value) if isinstance(value, int) and not isinstance(value, bool) else SIGN
                )
            case "day":
                return self._day(value)
            case "usd":
                return self._amount(value, "USD")
            case "amount":
                return self._amount(value, details.get("currency"))
            case "language":
                return self._language(value)
            case _:
                key = f"audit.{kind}.{value}"
                return self.texts(key) if isinstance(value, str) and has_meaning(key) else SIGN

    def _day(self, value: object) -> str:
        try:
            return self.texts.date(date.fromisoformat(str(value)), time=False)
        except ValueError:
            return SIGN

    def _amount(self, value: object, currency: object) -> str:
        if not isinstance(value, str) or not isinstance(currency, str):
            return SIGN
        try:
            amount = Decimal(value)
            if not amount.is_finite():
                return SIGN
            # Cents as cents (``$20.00``), and a smaller amount without its trailing zeros.
            amount = (
                amount.quantize(_CENT) if amount == amount.quantize(_CENT) else amount.normalize()
            )
            return self.texts.amount(amount, currency)
        except (InvalidOperation, ValueError):
            return SIGN

    def _language(self, value: object) -> str:
        if not isinstance(value, str):
            return SIGN
        known = babel_locale(value)
        if known.language != value.split("-", 1)[0].lower():
            return SIGN
        return known.get_display_name(babel_locale(self.texts.language)) or SIGN

    def line(self, ts: datetime | None, actor: str, action: str, raw: str) -> str:
        """The line of an entry: the text of its kind of line, its holes filled; a hole it has no
        value for (a text kept for an older line, say) is a sign, never an error of the page."""
        key = f"audit.line.{action}"
        facts: dict[str, str] = {}
        if has_meaning(key):
            details = _details(raw)
            said = action
            if action in LINE_WHEN:
                detail, other = LINE_WHEN[action]
                value = details.get(detail)
                if isinstance(value, str) and value.strip():
                    said, key = other, f"audit.line.{other}"
            facts = {
                hole: self.fact(details, detail, kind)
                for hole, (detail, kind) in LINE_FACTS.get(said, {}).items()
            }
        else:
            key = "audit.line.other"
        unknown = dict.fromkeys(HOLE.findall(self.texts.template(key)), SIGN)
        return self.texts(
            key, **{**unknown, **facts, "who": self.who(actor), "when": self.when(ts)}
        )


def _details(raw: str) -> Mapping[str, object]:
    """The details of an entry as stored; nothing for details that do not decode, or are not an
    object (hand-edited data: the page reports the tampering, it does not fail on it)."""
    try:
        details: object = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return cast(dict[str, object], details) if isinstance(details, dict) else {}


@router.get(AUDIT_PATH)
async def audit_page(
    request: Request,
    person: Annotated[Person, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    # Columns, not entities: what the database holds, not what the session remembers. The time
    # and the details are read in a form that a hand-edited row cannot make fail.
    latest = await session.execute(
        select(AuditEntry.id, readable_ts(), AuditEntry.actor, AuditEntry.action, details_as_text())
        .order_by(AuditEntry.id.desc())
        .limit(AUDIT_PAGE_SIZE)
    )
    names = await session.execute(select(Person.id, Person.display_name))
    named = name_shown(person.calls_domo, (await load_policy_settings(session)).house_domo_name)
    # Nobody gave him a name: the text of the house's language says it.
    domo = None if named == DEFAULT_NAME else named
    reader = _Reader(request_texts(request), {id_: name for id_, name in names.all()}, domo)
    rows = [
        AuditRow(id=entry_id, line=reader.line(ts, actor, action, raw))
        for entry_id, ts, actor, action, raw in latest.all()
    ]
    check = await verify_audit_chain(session)
    return render(
        request, "audit.html", person=person, rows=rows, check=check, limit=AUDIT_PAGE_SIZE
    )
