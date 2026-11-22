"""The tools of the texts people read (spec 4.7, 5.11): what each person asks of their own pages,
of someone else's with a right, of the version of a language, and Domo's choices on writing.

Every text people read is written by Domo for its reader (``texts.scheduler``), and each person
may ask for other words, in conversation:

* ``texts_find`` (``texts.find``, changes nothing, everyone's) finds the texts the person reads now
  (``texts.view.person_texts``: their own, else their language's, else the house's) that hold some
  words: ``FOUND_AT_MOST`` of them at most, the shortest first, each with its key and its group;
* ``texts_rewrite`` (``texts.rewrite``, judged, everyone's, on their own texts) has texts written
  again for them, with an indication of what to change (``INDICATION_MAX`` characters: Domo's
  words, which may carry a word they want, "Go"), on their keys (``TextWriting.rewrite``): some
  keys, the groups of the pages, or all their version (the groups they have texts of their own
  in). One key is written at once, ``REWRITE_NOW_SECONDS`` at most, so that the result says it: the
  new text, or that the checker refused it and why, the text they had staying (scenario 12); any
  more are written in the background, and the result says how many;
* ``texts_restore`` (``texts.restore``, judged, everyone's) brings back the earlier version of
  their texts (``texts.store.restore_previous``), once the writings of those texts under way for
  them have ended (``TextWriting.after_writings``: "put them back" just after "write them
  shorter" brings back the texts they had, never older ones under the shorter ones), waited for
  ``REWRITE_NOW_SECONDS`` at most, then in the background;
* ``texts_language`` (``texts.language``, judged, everyone's) changes the language they read: a
  language tag, in its usual case (``text_rules.language_tag``), never a name. The texts written
  for them in the language they read until then are dropped in the same transaction (Ruling
  R17), and those of the same groups are written again in the new one, in the background, on
  their keys. Where a text is not their own, they read the version of their language, which a
  page of theirs begins if there is none yet (``web.texts_middleware``, at most
  ``languages.NEW_LANGUAGES_PER_DAY`` new languages a day for the house). The owner's own language
  moves the house's (``house.language``, which its facts say: ``house_follows``, so that whoever
  judges or says yes reads it), once its version exists or the house may begin it now,
  on its own key (``TextWriting.begin_language``): the house never takes a language whose version
  is neither there nor being written (past the day's new languages, it stays as it is);
* ``texts_note`` (``texts.note``, judged, everyone's) keeps the note Domo writes on how to write
  to them (``WRITING_NOTE_MAX`` characters, one plain line; ``""`` drops it): the writer reads it
  as data, from their next writing on; it rewrites nothing by itself. The result gives the note it
  replaced, to the person's own Domo, so that he keeps what he learned;
* ``texts_for_someone`` (``texts.for_someone``, judged, nobody's by default, the owner's neither:
  a right opens it, socle 8) does any of the four last for someone else of the household, named;
  its facts say whom (``person``) and their role (``person_role``). It is the same code as the
  person's own tools, with the one acted on as a parameter; the texts are written on that person's
  keys. Their own texts are never changed through it;
* ``texts_rewrite_language`` (``texts.language_version``, judged, the owner's by default and given
  like any right) has the version of a language written again (the texts visitors read), on the
  house's key. A language with no version is never begun this way;
* ``writing_choose`` (``writing.choose``, changes nothing, every member's) keeps Domo's choices
  on writing with his other choices (``llm.choices``: what is written at once, and whether he
  speaks first when someone opens their page), every other choice kept.

The writing note and the indication are Domo's words about a person: they reach the writer as
data, which change only the form of a text (socle 3), and never an audit line, a log or a tool
result given to anyone else. The audit lines say who asked, for whom, how many texts and in which
language (``texts.rewrite_asked``, ``texts.version_rewrite_asked``, ``texts.restored``,
``texts.language_changed``, ``texts.note_changed``, ``writing.choices_changed``), and what the
writing refused, with why (``texts.refused``, by the writing itself).
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID

from domo_common.meanings import group_of, has_meaning, keys_of_group, written_groups
from domo_common.text_rules import LANGUAGE_TAG_MAX, language_tag
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.household.service import resolve_person
from domo_core.llm.choices import (
    ARRIVAL_VALUES,
    HOUSEHOLD_SCOPE,
    NO_CHOICES,
    WRITE_NOW_VALUES,
    Choices,
    choices_from_json,
    save_choices,
)
from domo_core.llm.models import ModelChoices
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction, Effect, FactValue, Present
from domo_core.policy.catalog import TEXT_CHANGES, TextChange
from domo_core.policy.roles import Actor
from domo_core.policy.settings import HOUSE_LANGUAGE, load_policy_settings, update_policy_setting
from domo_core.text import is_plain_line
from domo_core.texts.languages import known_languages
from domo_core.texts.models import WrittenText
from domo_core.texts.store import Reader, restore_previous, written
from domo_core.texts.view import person_texts
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.household_admin import described_target, name_from, person_named

if TYPE_CHECKING:
    from domo_core.texts.scheduler import Rewriting

# The longest note Domo keeps on how to write to someone, and the longest indication of a
# rewrite (spec 4.2, 4.3).
WRITING_NOTE_MAX: Final = 500
INDICATION_MAX: Final = 300
# The longest words looked for, and how many texts are found at most.
WORDS_MAX: Final = 200
FOUND_AT_MOST: Final = 20
# How long the rewrite of one text is waited for, to say what came of it.
REWRITE_NOW_SECONDS: Final = 10
# The most keys a call names; the longest reason of a choice.
KEYS_AT_MOST: Final = 50
WHY_MAX: Final = 200

FIND_ACTION_TYPE: Final = "texts.find"
REWRITE_ACTION_TYPE: Final = "texts.rewrite"
RESTORE_ACTION_TYPE: Final = "texts.restore"
LANGUAGE_ACTION_TYPE: Final = "texts.language"
NOTE_ACTION_TYPE: Final = "texts.note"
FOR_SOMEONE_ACTION_TYPE: Final = "texts.for_someone"
VERSION_ACTION_TYPE: Final = "texts.language_version"
CHOOSE_ACTION_TYPE: Final = "writing.choose"

# What may be done to someone else's texts (``catalog.TEXT_CHANGES``), and the arguments each
# takes besides whom and what.
What = TextChange
_SCOPE_ARGUMENTS: Final = frozenset({"keys", "groups", "all"})
_ARGUMENTS_OF: Final[Mapping[What, frozenset[str]]] = {
    "rewrite": _SCOPE_ARGUMENTS | {"indication"},
    "restore": _SCOPE_ARGUMENTS,
    "language": frozenset({"language"}),
    "note": frozenset({"note"}),
}

_WHICH_TEXTS: Final = "say which texts: keys, groups or all (true), one of them"
_NOT_A_KEY: Final = "a key is not one of the texts people read: find them with texts_find"
_NOT_A_GROUP: Final = "a group is not one of the groups of the texts people read"
_NOT_A_TAG: Final = (
    "language is a language tag: a language of two or three letters, maybe a script and a "
    "region (es, fr, pt-BR, zh-Hant), never a name"
)
_NO_VERSION: Final = (
    "this language has no version yet: it is begun when someone reads it, never this way"
)
_YOURSELF: Final = "their own texts, note and language are changed with the texts_ tools"
_NO_WRITING: Final = "the texts cannot be written from here"
_REFUSED: Final = "the checker refused the new text: the text they had stays"
_UNWRITTEN: Final = (
    "the text could not be written just now: no key they may use, or the writer did not answer"
)
_LATER: Final = "it is being written: it shows on their pages once it is ready"
_RESTORE_LATER: Final = (
    "a writing of these texts is under way: the earlier version comes back once it is over"
)
_NOTHING_TO_REWRITE: Final = (
    "they have no texts of their own yet: their pages are written for them as they see them"
)
_NO_NEW_LANGUAGE: Final = (
    "the house may begin no new language today: the house's language stays, and theirs is changed"
)


# --- the arguments ---------------------------------------------------------------------------


def _only(arguments: JsonObject, allowed: frozenset[str], tool: str) -> None:
    if set(arguments) - allowed:
        raise InvalidArguments("invalid", f"{tool} takes only: {', '.join(sorted(allowed))}")


def _line(value: object, name: str, *, longest: int, empty_ok: bool = False) -> str:
    """A text of the call: one plain line of ``longest`` characters at most, without the spaces
    around it; not blank, unless ``empty_ok`` and it is empty."""
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{name} must be a text")
    if len(value) > longest:
        raise InvalidArguments("too_long", f"{name} has more than {longest} characters")
    text = value.strip()
    if not is_plain_line(value) or (not text and not (empty_ok and value == "")):
        raise InvalidArguments("invalid", f"{name} must be one plain line of text")
    return text


def _names(value: object, name: str) -> list[str]:
    """A list of texts of the call: one at least, ``KEYS_AT_MOST`` at most, each once."""
    if not isinstance(value, list) or not value:
        raise InvalidArguments("invalid", f"{name} is a list of one or more texts")
    items = cast(list[object], value)
    if len(items) > KEYS_AT_MOST:
        raise InvalidArguments("invalid", f"{name} has more than {KEYS_AT_MOST} items")
    if not all(isinstance(item, str) for item in items):
        raise InvalidArguments("invalid", f"{name} is a list of texts")
    return list(dict.fromkeys(cast(list[str], items)))


@dataclass(frozen=True)
class _Scope:
    """Which texts a call names: some ``keys``, the keys of some ``groups``, or ``all`` the
    reader's version."""

    kind: Literal["keys", "groups", "all"]
    names: tuple[str, ...] = ()


def _scope_of(arguments: JsonObject) -> _Scope:
    given = _SCOPE_ARGUMENTS & set(arguments)
    if len(given) != 1:
        raise InvalidArguments("invalid", _WHICH_TEXTS)
    if "all" in given:
        if arguments["all"] is not True:
            raise InvalidArguments("invalid", _WHICH_TEXTS)
        return _Scope("all")
    groups = frozenset(written_groups())
    if "keys" in given:
        keys = _names(arguments["keys"], "keys")
        if not all(has_meaning(key) and group_of(key) in groups for key in keys):
            raise InvalidArguments("not_found", _NOT_A_KEY)
        return _Scope("keys", tuple(keys))
    named = _names(arguments["groups"], "groups")
    if not all(group in groups for group in named):
        raise InvalidArguments("not_found", _NOT_A_GROUP)
    return _Scope("groups", tuple(named))


def _indication_of(arguments: JsonObject) -> str | None:
    if "indication" not in arguments:
        return None
    return _line(arguments["indication"], "indication", longest=INDICATION_MAX)


def _language_of(arguments: JsonObject) -> str:
    value: object = arguments.get("language")
    tag = language_tag(value.strip()) if isinstance(value, str) else None
    if tag is None:
        raise InvalidArguments("invalid", _NOT_A_TAG)
    return tag


def _note_of(arguments: JsonObject) -> str:
    """The note of the call, ``""`` to drop it."""
    if "note" not in arguments:
        raise InvalidArguments("invalid", 'note is required ("" drops it)')
    return _line(arguments["note"], "note", longest=WRITING_NOTE_MAX, empty_ok=True)


def _what_of(arguments: JsonObject) -> What:
    value: object = arguments.get("what")
    for what in TEXT_CHANGES:
        if what == value:
            return what
    raise InvalidArguments("invalid", f"what is one of: {', '.join(TEXT_CHANGES)}")


@dataclass(frozen=True)
class _Ask:
    """What a call asks of someone's texts, its arguments checked."""

    what: What
    scope: _Scope | None = None
    indication: str | None = None
    language: str | None = None
    note: str | None = None


def _ask_of(what: What, arguments: JsonObject, tool: str) -> _Ask:
    """The call's ask ``what`` of a person's texts, from ``arguments`` (those of ``what`` only).
    Raises ``InvalidArguments``."""
    _only(arguments, _ARGUMENTS_OF[what], tool)
    match what:
        case "rewrite":
            return _Ask(what, _scope_of(arguments), _indication_of(arguments))
        case "restore":
            return _Ask(what, _scope_of(arguments))
        case "language":
            return _Ask(what, language=_language_of(arguments))
        case "note":
            return _Ask(what, note=_note_of(arguments))


# --- whose texts --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Whose:
    """Whose texts a call changes, as its cards and its audit lines say it: the person's own
    (``someone`` is ``None``), or someone else's (their name), or the version of a language
    (``language``)."""

    person_id: UUID | None
    someone: str | None = None
    language: str | None = None

    def card(
        self, status: Literal["done", "failed"], what: str, more: str = "", **params: str
    ) -> JsonObject:
        """The card of an outcome: ``action.card.<status>.texts.<what><more>``, for someone else
        ``texts.for_someone.<what>``, for a version ``texts.language_version``."""
        if self.person_id is None:
            name = "texts.language_version"
        elif self.someone is None:
            name = f"texts.{what}"
        else:
            name = f"texts.for_someone.{what}"
            params = {**params, "name": self.someone}
        shown: JsonObject = dict(params)
        return {"key": f"action.card.{status}.{name}{more}", "params": shown}

    def details(self) -> JsonObject:
        """Whom an audit line is about."""
        if self.person_id is None:
            return {"language": self.language}
        return {"person": f"person:{self.person_id}"}


async def _reader_of(session: AsyncSession, person_id: UUID) -> Reader | None:
    """The person as a reader of texts: in the language they read now (the house's until a page
    of theirs kept one); ``None`` when they are no longer in the database."""
    person = await session.get(Person, person_id)
    if person is None:
        return None
    language = person.language or (await load_policy_settings(session)).house_language
    return Reader(person_id, language)


async def _locked(session: AsyncSession, person_id: UUID) -> Person | None:
    return (
        await session.execute(
            select(Person)
            .where(Person.id == person_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _keys_in(
    session: AsyncSession, reader: Reader, scope: _Scope, *, whole_groups: bool
) -> list[str]:
    """The keys ``scope`` names for ``reader``: its keys, the keys of its groups, or, for all,
    the reader's own texts (``whole_groups``: every key of the groups they have texts in)."""
    match scope.kind:
        case "keys":
            return list(scope.names)
        case "groups":
            return [key for group in scope.names for key in keys_of_group(group)]
        case "all":
            own = sorted((await written(session, [reader]))[reader])
            if not whole_groups:
                return own
            groups = dict.fromkeys(group_of(key) for key in own)
            return [key for group in groups for key in keys_of_group(group)]


# --- what is done, whoever it is for --------------------------------------------------------


async def _rewrite(
    ctx: ToolContext, reader: Reader, whose: _Whose, scope: _Scope, indication: str | None
) -> ToolOutcome:
    """Have the texts ``scope`` names written again for ``reader``, with ``indication``: one at
    once (``REWRITE_NOW_SECONDS`` at most), any more in the background."""
    writing = ctx.text_writing
    if writing is None:
        return ToolOutcome(
            "failed",
            {"reason": "not_written", "detail": _NO_WRITING},
            whose.card("failed", "rewrite", ".unwritten"),
        )
    async with ctx.session_factory() as session:
        keys = await _keys_in(session, reader, scope, whole_groups=True)
        if not keys:
            # Nothing asked: no line of the journal.
            return ToolOutcome(
                "done",
                {"count": 0, "detail": _NOTHING_TO_REWRITE},
                whose.card("done", "rewrite", ".none"),
            )
        details: JsonObject = {**whose.details(), "scope": scope.kind, "count": len(keys)}
        actor = f"person:{ctx.person.id}"
        if whose.person_id is None:
            await append_audit(
                session, actor=actor, action="texts.version_rewrite_asked", details=details
            )
        else:
            await append_audit(session, actor=actor, action="texts.rewrite_asked", details=details)
        await session.commit()
    rewriting = writing.rewrite(reader, keys, indication=indication)
    if len(keys) > 1 or rewriting.task is None:
        return ToolOutcome(
            "done",
            {"count": len(keys), "in_background": True},
            whose.card("done", "rewrite", count=str(len(keys))),
        )
    finished, _ = await asyncio.wait({rewriting.task}, timeout=REWRITE_NOW_SECONDS)
    return _one_rewritten(keys[0], rewriting, whose, done=bool(finished))


def _one_rewritten(key: str, rewriting: "Rewriting", whose: _Whose, *, done: bool) -> ToolOutcome:
    """What came of the rewrite of one text, as the tool tells it."""
    if not done:
        return ToolOutcome(
            "done",
            {"key": key, "count": 1, "in_background": True, "detail": _LATER},
            whose.card("done", "rewrite", count="1"),
        )
    if key in rewriting.texts:
        return ToolOutcome(
            "done",
            {"key": key, "text": rewriting.texts[key]},
            whose.card("done", "rewrite", ".one"),
        )
    if key in rewriting.refused:
        return ToolOutcome(
            "failed",
            {"reason": "refused", "key": key, "why": rewriting.refused[key], "detail": _REFUSED},
            whose.card("failed", "rewrite", ".refused"),
        )
    return ToolOutcome(
        "failed",
        {"reason": "not_written", "key": key, "detail": _UNWRITTEN},
        whose.card("failed", "rewrite", ".unwritten"),
    )


async def _restore(ctx: ToolContext, reader: Reader, whose: _Whose, scope: _Scope) -> ToolOutcome:
    """Bring back the earlier version of the texts ``scope`` names for ``reader``, once the
    writings of their groups under way for the reader have ended (a rewrite asked just before
    lands first, and its text is the one that goes): ``REWRITE_NOW_SECONDS`` at most, then in the
    background."""
    async with ctx.session_factory() as session:
        keys = await _keys_in(session, reader, scope, whole_groups=False)
    done: list[list[str]] = []

    async def restore() -> None:
        done.append(await _restore_now(ctx, reader, whose, keys))

    writing = ctx.text_writing
    if writing is None or not keys:
        await restore()
    else:
        task = writing.after_writings(reader, (group_of(key) for key in keys), restore)
        finished, _ = await asyncio.wait({task}, timeout=REWRITE_NOW_SECONDS)
        if not finished:
            return ToolOutcome(
                "done",
                {"in_background": True, "detail": _RESTORE_LATER},
                whose.card("done", "restore", ".later"),
            )
    if not done:
        # It failed: what it was is logged.
        return ToolOutcome(
            "failed", {"reason": "not_restored"}, {"key": "action.card.failed", "params": {}}
        )
    restored = done[0]
    if not restored:
        return ToolOutcome("done", {"restored": []}, whose.card("done", "restore", ".none"))
    return ToolOutcome(
        "done",
        {"restored": restored},
        whose.card("done", "restore", count=str(len(restored))),
    )


async def _restore_now(
    ctx: ToolContext, reader: Reader, whose: _Whose, keys: list[str]
) -> list[str]:
    """Bring back the earlier version of ``keys`` for ``reader`` now, with its line of the
    journal: the keys brought back."""
    async with ctx.session_factory() as session:
        restored = await restore_previous(session, reader, keys)
        if restored:
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="texts.restored",
                details={**whose.details(), "count": len(restored)},
            )
        await session.commit()
    return restored


async def _change_language(
    ctx: ToolContext, person_id: UUID, whose: _Whose, language: str
) -> ToolOutcome:
    """Have the person read ``language``: their own texts of the language they read until then
    are dropped (Ruling R17), and those of the same groups written again in the new one, in the
    background. The owner's own language moves the house's (``_move_the_house``)."""
    async with ctx.session_factory() as session:
        person = await _locked(session, person_id)
        if person is None:
            raise InvalidArguments("not_found", "nobody in the household has this name")
        house = (await load_policy_settings(session)).house_language
        before = person.language or house
        changed = person.language != language
        keys: list[str] = []
        if language != before:
            own = (await written(session, [Reader(person.id, before)]))[Reader(person.id, before)]
            groups = dict.fromkeys(group_of(key) for key in sorted(own))
            keys = [key for group in groups for key in keys_of_group(group)]
            await session.execute(delete(WrittenText).where(WrittenText.person_id == person.id))
        person.language = language
        is_the_owner = person.role == "owner"
        if changed:
            # Last, just before the commit: the audit log holds a lock until the transaction ends.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="texts.language_changed",
                details={**whose.details(), "language": language},
            )
        await session.commit()
    result: JsonObject = {"language": language, "changed": changed}
    if whose.someone is None and is_the_owner and language != house:
        moved = await _move_the_house(ctx, language)
        result["house_language"] = language if moved else house
        if not moved:
            result["detail"] = _NO_NEW_LANGUAGE
    if keys and ctx.text_writing is not None:
        ctx.text_writing.rewrite(Reader(person_id, language), keys)
        result["rewritten_in_background"] = len(keys)
    return ToolOutcome("done", result, whose.card("done", "language"))


async def _move_the_house(ctx: ToolContext, language: str) -> bool:
    """The house's language becomes ``language``, the owner's own, once its version exists or the
    house may begin it now (on its own key, in the background): whether it moved."""
    async with ctx.session_factory() as session:
        has_a_version = language in await known_languages(session)
    if not has_a_version:
        writing = ctx.text_writing
        if writing is None or not await writing.begin_language(language, (), host=None):
            return False
    async with ctx.session_factory() as session:
        old, new = await update_policy_setting(
            session, key=HOUSE_LANGUAGE, value=language, by=ctx.person.id, now=ctx.clock()
        )
        await append_audit(
            session,
            actor=f"person:{ctx.person.id}",
            action="policy.updated",
            details={"key": HOUSE_LANGUAGE, "old": old, "new": new},
        )
        await session.commit()
    return True


async def _change_note(ctx: ToolContext, person_id: UUID, whose: _Whose, note: str) -> ToolOutcome:
    """Keep ``note`` as the note Domo writes on how to write to the person (``""`` drops it).
    The note it replaced is given back to the person's own Domo only."""
    async with ctx.session_factory() as session:
        person = await _locked(session, person_id)
        if person is None:
            raise InvalidArguments("not_found", "nobody in the household has this name")
        before = person.writing_note
        person.writing_note = note or None
        await append_audit(
            session,
            actor=f"person:{ctx.person.id}",
            action="texts.note_changed",
            details=whose.details(),
        )
        await session.commit()
    result: JsonObject = {"note": "kept" if note else "dropped"}
    if whose.someone is None:
        result["before"] = before
    return ToolOutcome("done", result, whose.card("done", "note", "" if note else ".cleared"))


async def _do(ctx: ToolContext, person_id: UUID, whose: _Whose, ask: _Ask) -> ToolOutcome:
    """Do ``ask`` for the person of ``person_id``: the same code for their own texts and, with a
    right, for someone else's."""
    if ask.language is not None:
        return await _change_language(ctx, person_id, whose, ask.language)
    if ask.note is not None:
        return await _change_note(ctx, person_id, whose, ask.note)
    async with ctx.session_factory() as session:
        reader = await _reader_of(session, person_id)
    if reader is None:
        raise InvalidArguments("not_found", "nobody in the household has this name")
    assert ask.scope is not None
    if ask.what == "rewrite":
        return await _rewrite(ctx, reader, whose, ask.scope, ask.indication)
    return await _restore(ctx, reader, whose, ask.scope)


def _own(
    action_type: str,
    actor: Actor,
    effect: Effect = "self_profile",
    facts: Mapping[str, FactValue] | None = None,
) -> DescribedAction:
    """A call on the person's own texts, as the barrier sees it."""
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=f"person:{actor.person_id}",
        effect=effect,
        destination="household",
        facts={} if facts is None else facts,
    )


# --- the definitions --------------------------------------------------------------------------

_KEYS_SCHEMA: Final[JsonObject] = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": KEYS_AT_MOST,
    "description": "The keys of the texts, as texts_find gives them.",
}
_GROUPS_SCHEMA: Final[JsonObject] = {
    "type": "array",
    "items": {"type": "string", "enum": list(written_groups())},
    "description": "Whole groups of texts (a page, a kind of card), as texts_find gives them.",
}
_ALL_SCHEMA: Final[JsonObject] = {
    "type": "boolean",
    "description": "true: every text of the version (the groups that have texts of their own).",
}
_INDICATION_SCHEMA: Final[JsonObject] = {
    "type": "string",
    "maxLength": INDICATION_MAX,
    "description": (
        "What to change, in your words, for the writer: it changes only the form, never what a "
        "text says (an exact word they want goes in it, as 'Call it: Go')."
    ),
}
_LANGUAGE_SCHEMA: Final[JsonObject] = {
    "type": "string",
    "maxLength": LANGUAGE_TAG_MAX,
    "description": "A language tag: es, fr, de, pt-BR, zh-Hant...",
}
_NOTE_SCHEMA: Final[JsonObject] = {
    "type": "string",
    "maxLength": WRITING_NOTE_MAX,
    "description": (
        "The whole note, as it should be now, in one line: it replaces the one before; an empty "
        "text drops it."
    ),
}
_SCOPE_TOLD: Final = (
    "Name the texts with keys (one or more), groups (whole pages or kinds of cards) or all (their "
    "whole version), one of them."
)


class TextsFind:
    """Find the texts the person reads that hold some words."""

    definition = ToolDefinition(
        name="texts_find",
        description=(
            "Find the texts the person reads on their pages (buttons, titles, cards, messages) "
            "that hold some words, as they read them: each with its key and its group, "
            f"{FOUND_AT_MOST} at most, the shortest first. Use it before texts_rewrite or "
            "texts_restore, to know which text they mean."
        ),
        parameters={
            "type": "object",
            "properties": {
                "words": {
                    "type": "string",
                    "maxLength": WORDS_MAX,
                    "description": "Words of the text, as the person reads it.",
                }
            },
            "required": ["words"],
            "additionalProperties": False,
        },
    )
    action_type = FIND_ACTION_TYPE

    @staticmethod
    def _words(arguments: JsonObject) -> list[str]:
        _only(arguments, frozenset({"words"}), "texts_find")
        return _line(arguments.get("words"), "words", longest=WORDS_MAX).casefold().split()

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        self._words(arguments)
        return _own(self.action_type, actor, "read")

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        words = self._words(arguments)
        async with ctx.session_factory() as session:
            person = await session.get(Person, ctx.person.id)
            if person is None:
                raise InvalidArguments("not_found", "nobody in the household has this name")
            texts = await person_texts(session, person)
        found = sorted(
            (
                (key, text)
                for key, text in texts.found.items()
                if all(word in text.casefold() for word in words)
            ),
            key=lambda item: (len(item[1]), item[0]),
        )
        shown: list[JsonObject] = [
            {"key": key, "group": group_of(key), "text": text}
            for key, text in found[:FOUND_AT_MOST]
        ]
        return ToolOutcome(
            "done",
            {"found": list(shown), "more": len(found) > FOUND_AT_MOST},
            {"key": "action.card.done.texts.find", "params": {}},
        )


class TextsRewrite:
    """Have some of the person's texts written again, with an indication."""

    definition = ToolDefinition(
        name="texts_rewrite",
        description=(
            "Have texts of the person's own pages written again for them, when they ask for "
            "other words ('call this button Go', 'shorter cards'), or when you see it would "
            "help. It changes their pages only. " + _SCOPE_TOLD + " The indication says what "
            "to change; to change how you write to them for good, keep it in their writing note "
            "(texts_note) too. Each text is checked like any other: it must keep its meaning "
            "(a button that confirms is never called 'Cancel'). One text is written at once and "
            "the result gives it, or why it was refused (the text they had stays); more are "
            "written in the background, and the result says how many."
        ),
        parameters={
            "type": "object",
            "properties": {
                "keys": _KEYS_SCHEMA,
                "groups": _GROUPS_SCHEMA,
                "all": _ALL_SCHEMA,
                "indication": _INDICATION_SCHEMA,
            },
            "additionalProperties": False,
        },
    )
    action_type = REWRITE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _ask_of("rewrite", arguments, "texts_rewrite")
        return _own(self.action_type, actor)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ask = _ask_of("rewrite", arguments, "texts_rewrite")
        return await _do(ctx, ctx.person.id, _Whose(ctx.person.id), ask)


class TextsRestore:
    """Bring back the earlier version of some of the person's texts."""

    definition = ToolDefinition(
        name="texts_restore",
        description=(
            "Bring back the earlier version of texts of the person's own pages, when they want "
            "them as they were. " + _SCOPE_TOLD + " The result lists the texts brought back: "
            "one with no earlier version stays as it is."
        ),
        parameters={
            "type": "object",
            "properties": {"keys": _KEYS_SCHEMA, "groups": _GROUPS_SCHEMA, "all": _ALL_SCHEMA},
            "additionalProperties": False,
        },
    )
    action_type = RESTORE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _ask_of("restore", arguments, "texts_restore")
        return _own(self.action_type, actor)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ask = _ask_of("restore", arguments, "texts_restore")
        return await _do(ctx, ctx.person.id, _Whose(ctx.person.id), ask)


class TextsLanguage:
    """Change the language the person reads."""

    definition = ToolDefinition(
        name="texts_language",
        description=(
            "Change the language the person's pages are written in, when they ask for it or "
            "clearly speak another one: a language tag. Their own texts are written again in it, "
            "in the background, on their keys; until then they read the version of that "
            "language, which their next page begins if there is none yet (a few new languages a "
            "day for the whole house). If the person is the one who owns the household, the "
            "house's language follows theirs (the language of visitors), once the house may "
            "write it: the result says so."
        ),
        parameters={
            "type": "object",
            "properties": {"language": _LANGUAGE_SCHEMA},
            "required": ["language"],
            "additionalProperties": False,
        },
    )
    action_type = LANGUAGE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        ask = _ask_of("language", arguments, "texts_language")
        follows = False
        if actor.role == "owner":
            # The owner's own language moves the house's: who judges or says yes reads it.
            async with ctx.session_factory() as session:
                follows = ask.language != (await load_policy_settings(session)).house_language
        return _own(self.action_type, actor, facts={"house_follows": follows})

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ask = _ask_of("language", arguments, "texts_language")
        return await _do(ctx, ctx.person.id, _Whose(ctx.person.id), ask)


class TextsNote:
    """Keep the note Domo writes on how to write to the person."""

    definition = ToolDefinition(
        name="texts_note",
        description=(
            "Keep your note on how to write to the person, when you learn it: 'tu' or 'vous' (or "
            "what their language uses), the tone, what to avoid ('no jokes about cards'), a word "
            f"they want; {WRITING_NOTE_MAX} characters at most, in your words. The writer of their "
            "pages reads it from their next writing on: it changes the form of their texts, never "
            "what they say, and rewrites nothing by itself (texts_rewrite does). The result gives "
            "the note it replaced: keep in the new one what still holds."
        ),
        parameters={
            "type": "object",
            "properties": {"note": _NOTE_SCHEMA},
            "required": ["note"],
            "additionalProperties": False,
        },
    )
    action_type = NOTE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _ask_of("note", arguments, "texts_note")
        return _own(self.action_type, actor)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ask = _ask_of("note", arguments, "texts_note")
        return await _do(ctx, ctx.person.id, _Whose(ctx.person.id), ask)


class TextsForSomeone:
    """Change someone else's texts, writing note or language (with a right)."""

    definition = ToolDefinition(
        name="texts_for_someone",
        description=(
            "Change the texts, the writing note or the language of someone else of the household, "
            "named (what: rewrite, restore, language or note, with the arguments of texts_rewrite, "
            "texts_restore, texts_language or texts_note). Only with a right given for it: by "
            "default nobody may change someone else's pages. Their texts are written on their "
            "keys. The person's own texts, note and language are changed with the texts_ tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Their name in the household."},
                "what": {"type": "string", "enum": list(TEXT_CHANGES)},
                "keys": _KEYS_SCHEMA,
                "groups": _GROUPS_SCHEMA,
                "all": _ALL_SCHEMA,
                "indication": _INDICATION_SCHEMA,
                "language": _LANGUAGE_SCHEMA,
                "note": _NOTE_SCHEMA,
            },
            "required": ["person", "what"],
            "additionalProperties": False,
        },
    )
    action_type = FOR_SOMEONE_ACTION_TYPE
    # The argument that names someone of the household: given its whole name before the call
    # is described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    @staticmethod
    def fixed_refusal(actor: Actor, facts: Mapping[str, FactValue], present: Present) -> str | None:
        """What no right moves (``RefusesWhateverTheRights``): it is for someone else."""
        if facts.get("person") == f"person:{actor.person_id}":
            return _YOURSELF
        return None

    @staticmethod
    def _arguments(arguments: JsonObject) -> tuple[str, _Ask]:
        what = _what_of(arguments)
        name = name_from(arguments.get("person"), "person")
        rest = {key: value for key, value in arguments.items() if key not in ("person", "what")}
        return name, _ask_of(what, rest, f"texts_for_someone with what={what}")

    def _facts(self, person: Person, actor: Actor) -> dict[str, FactValue]:
        facts: dict[str, FactValue] = {
            "person": f"person:{person.id}",
            "person_role": person.role,
        }
        refused = self.fixed_refusal(actor, facts, {})
        if refused is not None:
            raise InvalidArguments("invalid", refused)
        return facts

    async def acts_on(self, arguments: JsonObject, ctx: ToolContext) -> str | None:
        """Whom the call names (``ActsOn``), by the name alone; nobody when the tool does not
        accept its arguments."""
        try:
            name, _ = self._arguments(arguments)
        except InvalidArguments:
            return None
        return await person_named(name, ctx)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        name, _ = self._arguments(arguments)
        async with ctx.session_factory() as session:
            person = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="message",
            destination="household",
            facts=self._facts(person, actor),
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name, ask = self._arguments(arguments)
        async with ctx.session_factory() as session:
            person = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
        described_target(ctx, f"person:{person.id}")
        self._facts(person, ctx.actor)
        return await _do(ctx, person.id, _Whose(person.id, someone=person.display_name), ask)


class TextsRewriteLanguage:
    """Have texts of the version of a language written again."""

    definition = ToolDefinition(
        name="texts_rewrite_language",
        description=(
            "Have texts of the version of a language written again: the texts visitors read, and "
            "anyone whose own texts are not written yet. " + _SCOPE_TOLD + " The indication says "
            "what to change; each text is checked like any other. Only a language that has a "
            "version already. One text is written at once and the result gives it, or why it was "
            "refused; more are written in the background."
        ),
        parameters={
            "type": "object",
            "properties": {
                "language": _LANGUAGE_SCHEMA,
                "keys": _KEYS_SCHEMA,
                "groups": _GROUPS_SCHEMA,
                "all": _ALL_SCHEMA,
                "indication": _INDICATION_SCHEMA,
            },
            "required": ["language"],
            "additionalProperties": False,
        },
    )
    action_type = VERSION_ACTION_TYPE

    @staticmethod
    def _arguments(arguments: JsonObject) -> tuple[str, _Ask]:
        language = _language_of(arguments)
        rest = {key: value for key, value in arguments.items() if key != "language"}
        return language, _ask_of("rewrite", rest, "texts_rewrite_language")

    @staticmethod
    async def _has_a_version(ctx: ToolContext, language: str) -> None:
        async with ctx.session_factory() as session:
            if language not in await known_languages(session):
                raise InvalidArguments("not_found", _NO_VERSION)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        language, _ = self._arguments(arguments)
        await self._has_a_version(ctx, language)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"language:{language}",
            effect="message",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        language, ask = self._arguments(arguments)
        await self._has_a_version(ctx, language)
        assert ask.scope is not None
        whose = _Whose(None, language=language)
        return await _rewrite(ctx, Reader(None, language), whose, ask.scope, ask.indication)


def _moments_of(value: object) -> tuple[str, ...]:
    """The moments a text is written at once, each once, in the order of ``WRITE_NOW_VALUES``."""
    if not isinstance(value, list):
        raise InvalidArguments("invalid", "write_now is a list")
    items = cast(list[object], value)
    if not all(isinstance(item, str) and item in WRITE_NOW_VALUES for item in items):
        raise InvalidArguments("invalid", f"write_now holds only: {', '.join(WRITE_NOW_VALUES)}")
    return tuple(known for known in WRITE_NOW_VALUES if known in items)


@dataclass(frozen=True)
class _WritingChoice:
    """A ``writing_choose`` call, read: what it changes (``None``: left as it is) and why."""

    why: str
    write_now: tuple[str, ...] | None
    arrival: str | None

    def changes(self) -> JsonObject:
        changed: JsonObject = {}
        if self.write_now is not None:
            changed["write_now"] = list(self.write_now)
        if self.arrival is not None:
            changed["arrival"] = self.arrival
        return changed

    def applied_to(self, choices: Choices) -> Choices:
        """``choices`` with this call's changes, every other choice kept."""
        return replace(
            choices,
            write_now=choices.write_now if self.write_now is None else self.write_now,
            arrival=choices.arrival if self.arrival is None else self.arrival,
        )


def _writing_choice_of(arguments: JsonObject) -> _WritingChoice:
    _only(arguments, frozenset({"write_now", "arrival", "why"}), "writing_choose")
    why = _line(arguments.get("why"), "why", longest=WHY_MAX)
    write_now = _moments_of(arguments["write_now"]) if "write_now" in arguments else None
    arrival: str | None = None
    if "arrival" in arguments:
        value: object = arguments["arrival"]
        if not isinstance(value, str) or value not in ARRIVAL_VALUES:
            raise InvalidArguments("invalid", f"arrival is one of: {', '.join(ARRIVAL_VALUES)}")
        arrival = value
    if write_now is None and arrival is None:
        raise InvalidArguments("invalid", "writing_choose was asked to change nothing")
    return _WritingChoice(why, write_now, arrival)


class WritingChoose:
    """Change Domo's choices on writing."""

    definition = ToolDefinition(
        name="writing_choose",
        description=(
            "Change your own choices on writing, for the whole household. write_now: when the "
            "top of a card is written at once for each of its readers, rather than taken from "
            "their texts (requests: the requests that wait for a yes; done: what you did for "
            "them; an empty list: never). arrival: whether you may speak first when someone "
            "opens their page (every_visit, when_new: only when there is something new for "
            "them, never). It changes nothing in the household: no yes is needed, and each "
            "change is written down with why. Writing at once costs a call for each reader."
        ),
        parameters={
            "type": "object",
            "properties": {
                "write_now": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(WRITE_NOW_VALUES)},
                    "description": "What is written at once (an empty list: nothing).",
                },
                "arrival": {
                    "type": "string",
                    "enum": list(ARRIVAL_VALUES),
                    "description": "Whether you may speak first when someone opens their page.",
                },
                "why": {
                    "type": "string",
                    "maxLength": WHY_MAX,
                    "description": "Why, in a few words (written down with the change).",
                },
            },
            "required": ["why"],
            "additionalProperties": False,
        },
    )
    action_type = CHOOSE_ACTION_TYPE

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _writing_choice_of(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=HOUSEHOLD_SCOPE,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        choice = _writing_choice_of(arguments)
        async with ctx.session_factory() as session:
            row = (
                await session.execute(
                    select(ModelChoices)
                    .where(ModelChoices.scope == HOUSEHOLD_SCOPE)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            before = NO_CHOICES if row is None else choices_from_json(row.choices)
            now = choice.applied_to(before)
            await save_choices(
                session, HOUSEHOLD_SCOPE, now, by=ctx.person.id, why=choice.why, now=ctx.clock()
            )
            # Last, just before the commit: the audit log holds a lock until it ends.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="writing.choices_changed",
                details={"changes": choice.changes(), "why": choice.why},
            )
            await session.commit()
        return ToolOutcome(
            "done",
            {"write_now": list(now.write_now), "arrival": now.arrival},
            {"key": "action.card.done.writing.choose", "params": {}},
        )
