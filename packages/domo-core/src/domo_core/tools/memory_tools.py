"""``memory_keep``, ``memory_change``, ``memory_forget`` and ``memory_search``: what Domo keeps in
his memory, changes, forgets and looks up (spec 6.1 to 6.3).

They are everyone's, a guest's too, and nothing about them is judged or shown
(``policy.catalog.SILENT_ACTIONS``): what Domo keeps is his judgement. The model reads the result
of each call, or why it was refused, in the turn; nobody sees a card of it, on any channel.

``memory_keep`` writes a memory said by the person of the turn (``said_by_id``). Where it comes from
(``origin``) is set by the code, never by the model (``origin_for``): the person's own words when
Domo gives the exact passage of their message it comes from (``their_words``, checked word for
word as a note's exact words are, ``tools.notes.exact_text_from``), else someone else's words when
the turn was given words that are not theirs (``ToolContext.told_outside``), else the person's own
words when the turn answers a message of theirs, else what Domo deduced. Who it is about
(``about``) is a person of the household, named as the household knows them; when Domo leaves it
out and the subject is a member's name (their whole name, a nickname of the person who speaks, or
their first name), it is that member (``_member_named``).

What someone says about another person of the household is their word, not a fact, until that
person confirms it (2026-11-16, the owner's trial of 0.9.0, ``memory.service.unconfirmed``): such a
memory is marked ``unconfirmed`` wherever Domo is given it. ``memory_change`` with ``confirmed``
confirms it, only in a turn that answers the person it is about (``ToolContext.answers_them``): who
first said it and its origin stay. New words for it in such a turn are that person's correction:
the memory is confirmed, and it becomes theirs (said by them, their own words) only when Domo
gives the passage of their message it comes from, checked word for word; otherwise its author and
its origin stay as cautious as they were (``origin_after_rewrite``), and the yes-judge never reads
it. New words in anyone else's turn take a confirmation away; in Domo's answer to a third
person (neither who said it nor who it is about), the new words are theirs: the memory becomes
said by them (``origin_of_new_words``), so that what the person it is about then confirms or
denies counts for them, never for whoever said the old words. The yes-judge never reads a memory
said by someone else, confirmed or not.

``memory_change`` rewrites a memory, puts it in the core or takes it out, and keeps it for himself
or lifts the secret, in anyone's turn; ``memory_forget`` deletes memories for good. A memory is
named by its id, which Domo only has of a memory he was given or found: one he keeps for himself
of someone else only when he looked for it on purpose. An id that is no memory's is ``not_found``.
New words are a new write: their origin is set again by the code (``origin_after_rewrite``: its
author's own words only in their own turn with the exact passage of their message, else the more
cautious of the old origin and the turn's, ``CAUTION``: a rewrite never launders a cautious
origin); what Domo told the person (``told_by_domo``, written by the code) cannot be
rewritten, only forgotten. A change that changes nothing is refused. Refusing to confirm a memory
that needs no confirmation, or in a turn that does not answer the person it is about, and refusing
new words for what Domo told someone, are ``not_possible``: rules of the tool, not a mistake of
the model's form (``chat.agent`` never takes a stronger model for them).

``memory_search`` finds memories by their words and their meaning (``memory.search.search``), never
what Domo keeps for himself of someone else, unless he asks for it on purpose
(``kept_to_myself``). What it gives is marked as served once the turn is over, never in the call
(``ToolContext.memories_found``). An unconfirmed memory said by someone else who is in the
household comes with their trust (spec 6.4, ``trust.shown.trust_shown``).

A text with the shape of a key, a token, a link with a secret or a device code is refused
(``memory.service.SecretRefused``), and nothing is written: the result tells Domo that it looks
like a secret, and that he may say it differently if it is not one (an ordinary text may have that
shape: a long word with a digit, a twelve-digit number).

The audit log keeps that a memory was kept (``memory.kept``), changed (``memory.changed``) or
forgotten (``memory.forgotten``), with ids and the kind alone, never the subject or the content;
that a secret was set or lifted, in whose turn (``memory.secret_changed``); and that Domo looked
at what he keeps for himself, in whose turn, with how many memories kept for himself of someone
else it gave (``memory.kept_looked``).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final, cast, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.household.service import active_persons, resolve_person
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.models import Kind, Memory, Origin
from domo_core.memory.search import search
from domo_core.memory.service import (
    MAX_CONTENT,
    MAX_SUBJECT,
    SecretRefused,
    change,
    forget,
    get,
    keep,
    refuses_secret_shapes,
    unconfirmed,
    who_said,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import is_plain_line, name_key
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.tools.notes import MAX_NOTE_LENGTH, exact_text_from
from domo_core.trust.shown import trust_shown

KINDS: Final[tuple[str, ...]] = get_args(Kind)
# The most memories one call forgets, and the longest words a search is given.
MAX_FORGOTTEN: Final = 20
MAX_SEARCH_WORDS: Final = 500
# How many memories a search gives at most.
SEARCH_LIMIT: Final = 10
# What a call that kept, changed or forgot leaves under the reply: a card nobody reads (the
# actions of memory are never shown, ``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}
# What Domo is told of a text with the shape of a secret: the filter also refuses some ordinary
# text, which he may then say otherwise.
SECRET_REFUSED: Final = (
    "this looks like a secret: secrets are never kept in memory. Nothing was kept. If it is not "
    "a secret, say it differently (in words, without a long code, a long number or a run of "
    "letters and digits) and try again"
)


def _verified(ctx: ToolContext, their_words: str | None) -> bool:
    """Whether ``their_words`` (1 000 characters at most) is a passage of the person's message in
    this turn, word for word (``exact_text_from``)."""
    return (
        their_words is not None
        and len(their_words) <= MAX_NOTE_LENGTH
        and exact_text_from(ctx.said, their_words) is not None
    )


def origin_for(ctx: ToolContext, their_words: str | None) -> Origin:
    """Where a memory written in this turn comes from, set by the code (spec 6.3): the person's
    own words when ``their_words`` (1 000 characters at most) is a passage of their message, word
    for word (``exact_text_from``); else someone else's words when the turn was given words that
    are not theirs (``told_outside``); else their own words when the turn answers a message of
    theirs (``reply_message_id``); else what Domo deduced (a turn he started himself)."""
    if _verified(ctx, their_words):
        return "own_words"
    if ctx.told_outside:
        return "others_words"
    if ctx.reply_message_id is not None:
        return "own_words"
    return "deduced"


# The origins, from the most cautious to the least: what the yes-judge may read of a person's
# memories is only their own words and what Domo told them (spec 6.8).
CAUTION: Final[tuple[Origin, ...]] = (
    "external",
    "others_words",
    "deduced",
    "told_by_domo",
    "own_words",
)


def origin_after_rewrite(memory: Memory, ctx: ToolContext, their_words: str | None) -> Origin:
    """Where a memory comes from once its words are rewritten in this turn (spec 6.3: a rewrite
    is a write, and the code sets its origin again). It becomes its author's own words only when
    the one who speaks is the one who said it and gives the exact passage of their message in this
    turn (``their_words``, checked word for word); otherwise the more cautious of the old origin and
    the turn's, words of the turn of someone else than its author being someone else's words. A
    rewrite never makes a memory less cautious than it was without a verified passage: the
    yes-judge reads only what the person said (spec 6.8)."""
    same_person = memory.said_by_id is not None and memory.said_by_id == ctx.person.id
    if same_person and _verified(ctx, their_words):
        return "own_words"
    new: Origin = origin_for(ctx, None)
    if not same_person and new == "own_words":
        new = "others_words"
    return _more_cautious(memory.origin, new)


def origin_of_new_words(memory: Memory, ctx: ToolContext, their_words: str | None) -> Origin:
    """Where a memory comes from once someone else than its author rewrites its words in
    Domo's answer to them, the memory becoming theirs: their own words with the exact passage of
    their message (``their_words``, checked word for word), else the more cautious of the old
    origin and the one a memory kept in their turn gets (``origin_for``): a rewrite never
    launders a cautious origin."""
    if _verified(ctx, their_words):
        return "own_words"
    return _more_cautious(memory.origin, origin_for(ctx, None))


def _more_cautious(old: Origin, new: Origin) -> Origin:
    return old if CAUTION.index(old) <= CAUTION.index(new) else new


def _text(value: object, name: str, limit: int, *, lines: bool) -> str:
    """``value`` without the spaces around it: a text of 1 to ``limit`` characters, plain (on one
    line, or on several with ``lines``). ``InvalidArguments`` otherwise; a text with the shape of a
    secret is refused too (``SECRET_REFUSED``)."""
    if not isinstance(value, str):
        raise InvalidArguments("invalid", f"{name} is a text")
    text = value.replace("\r\n", "\n").strip()
    if not text:
        raise InvalidArguments("invalid", f"{name} is empty")
    if len(text) > limit:
        raise InvalidArguments("too_long", f"{name} has more than {limit} characters")
    parts = text.split("\n") if lines else [text]
    if not all(is_plain_line(part) for part in parts):
        raise InvalidArguments(
            "invalid", f"{name} has a control character, a separator or a direction mark"
        )
    if refuses_secret_shapes(text):
        raise InvalidArguments("invalid", SECRET_REFUSED)
    return text


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArguments("invalid", f"{name} is true or false")
    return value


def _memory_id(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "a memory's id is a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("not_found", "no memory has this id") from None


def _only(arguments: JsonObject, required: frozenset[str], allowed: frozenset[str]) -> None:
    if not required <= frozenset(arguments) <= allowed:
        names = ", ".join(sorted(allowed))
        raise InvalidArguments("invalid", f"the arguments are among {names}")


class _Keep:
    """The arguments of a ``memory_keep`` call, checked."""

    _REQUIRED: Final = frozenset({"content", "subject", "kind", "private", "core"})
    _ALLOWED: Final = _REQUIRED | {"about", "their_words"}

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, self._REQUIRED, self._ALLOWED)
        self.content = _text(arguments["content"], "content", MAX_CONTENT, lines=True)
        self.subject = _text(arguments["subject"], "subject", MAX_SUBJECT, lines=False)
        kind = arguments["kind"]
        if kind not in KINDS:
            raise InvalidArguments("invalid", f"kind is one of {', '.join(KINDS)}")
        self.kind: Kind = cast(Kind, kind)
        self.private = _flag(arguments["private"], "private")
        self.core = _flag(arguments["core"], "core")
        about = arguments.get("about")
        if about is not None and (not isinstance(about, str) or not about.strip()):
            raise InvalidArguments("invalid", "about is the name of a person of the household")
        self.about: str | None = None if about is None else about.strip()
        words = arguments.get("their_words")
        if words is not None and not isinstance(words, str):
            raise InvalidArguments("invalid", "their_words is a text")
        self.their_words: str | None = words


async def _member_named(
    session: AsyncSession, subject: str, ctx: ToolContext, now: datetime
) -> uuid.UUID | None:
    """The member of the household a subject names, when it is a name: their whole name or a
    nickname of the person who speaks (``resolve_person``, ``exact_only``), else the first name of
    one member alone. Never the beginning of a name ("me" is not Mehdi); ``None`` otherwise."""
    try:
        return (
            await resolve_person(session, subject, now=now, speaker=ctx.person.id, exact_only=True)
        ).id
    except InvalidArguments:
        pass
    key = name_key(subject)
    first = [
        p
        for p in await active_persons(session, now)
        if name_key(p.display_name).split(" ")[0] == key
    ]
    return first[0].id if key and len(first) == 1 else None


class MemoryKeep:
    """Keep a memory, said by the person of the turn."""

    definition = ToolDefinition(
        name="memory_keep",
        description=(
            "Keep something in your memory, in one or two sentences of your own, when you judge "
            "it worth remembering: a fact, a preference, a moment, a lesson about what you got "
            "wrong and how to do better, or something about yourself. Anyone may tell you "
            "something to remember; you judge whether it is true, and whether keeping it is wise, "
            "and you can say who told you. "
            'Write your doubts in the sentence ("I think..."). Set private when someone asks you '
            "to keep it to yourself or when you judge it sensitive; in doubt, keep it to yourself: "
            "a secret can be lifted later, a word said cannot be taken back. Set core for what "
            "you must always have in mind (who someone is, a lasting wish). When it is about a "
            "person of the household, give their name in about, always. Never keep a "
            "password, a key or a code. If what you keep comes from the person's own message, "
            "give in their_words the passage it comes from, copied word for word; always for "
            "what they tell you about another person: only then can what that person later "
            "confirms or denies count for them. Nobody is shown that you keep it: say so in your "
            "words if it helps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory, in one or two sentences, in any language.",
                    "minLength": 1,
                    "maxLength": MAX_CONTENT,
                },
                "subject": {
                    "type": "string",
                    "description": 'What it is about, in a word or two ("Alex", "me", "the '
                    'boiler").',
                    "minLength": 1,
                    "maxLength": MAX_SUBJECT,
                },
                "kind": {"type": "string", "enum": list(KINDS)},
                "private": {
                    "type": "boolean",
                    "description": "Whether you keep it to yourself.",
                },
                "core": {
                    "type": "boolean",
                    "description": "Whether it is always in your mind.",
                },
                "about": {
                    "type": "string",
                    "description": "The name of the person of the household it is about, "
                    "whenever it is about one.",
                    "minLength": 1,
                },
                "their_words": {
                    "type": "string",
                    "description": "The passage of the person's own message it comes from, "
                    "copied character for character.",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                },
            },
            "required": ["content", "subject", "kind", "private", "core"],
            "additionalProperties": False,
        },
    )
    action_type = "memory.keep"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _Keep(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Keep(arguments)
        origin = origin_for(ctx, given.their_words)
        now = ctx.clock()
        async with ctx.session_factory() as session:
            about = (
                await _member_named(session, given.subject, ctx, now)
                if given.about is None
                else (await resolve_person(session, given.about, now=now, speaker=ctx.person.id)).id
            )
            try:
                memory = await keep(
                    session,
                    said_by=ctx.person.id,
                    subject=given.subject,
                    content=given.content,
                    kind=given.kind,
                    origin=origin,
                    kept_private=given.private,
                    core=given.core,
                    now=now,
                    about_id=about,
                )
            except SecretRefused:
                raise InvalidArguments("invalid", SECRET_REFUSED) from None
            except ValueError:
                raise InvalidArguments("invalid", "the memory is out of its bounds") from None
            memory_id = memory.id
            # Last, just before the commit. Never the subject nor the content.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="memory.kept",
                details={"memory": str(memory_id), "kind": given.kind, "private": given.private},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result={"id": str(memory_id)}, card=_CARD)


class _Change:
    """The arguments of a ``memory_change`` call, checked."""

    _CHANGES: Final = frozenset({"content", "subject", "core", "private", "confirmed"})
    _ALLOWED: Final = _CHANGES | {"id", "their_words"}

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, frozenset({"id"}), self._ALLOWED)
        if all(arguments.get(name) is None for name in self._CHANGES):
            raise InvalidArguments(
                "invalid", "say what changes: content, subject, core, private or confirmed"
            )
        self.id = _memory_id(arguments["id"])
        content, subject = arguments.get("content"), arguments.get("subject")
        self.content = (
            None if content is None else _text(content, "content", MAX_CONTENT, lines=True)
        )
        self.subject = (
            None if subject is None else _text(subject, "subject", MAX_SUBJECT, lines=False)
        )
        core, private = arguments.get("core"), arguments.get("private")
        self.core = None if core is None else _flag(core, "core")
        self.private = None if private is None else _flag(private, "private")
        confirmed = arguments.get("confirmed")
        if confirmed is not None and confirmed is not True:
            raise InvalidArguments("invalid", "confirmed is only true")
        self.confirmed = confirmed is True
        words = arguments.get("their_words")
        if words is not None and not isinstance(words, str):
            raise InvalidArguments("invalid", "their_words is a text")
        self.their_words: str | None = words

    @property
    def rewrites(self) -> bool:
        """Whether the call gives the memory new words (its content or its subject)."""
        return self.content is not None or self.subject is not None


_NOT_FOUND: Final = "no memory has this id"
_NEEDS_NO_CONFIRMATION: Final = (
    "this memory needs no confirmation: it is not someone's word about another person that waits "
    "for theirs"
)
_ONLY_THEY_CONFIRM: Final = (
    "only the person this memory is about confirms it, in your answer to them: check it with "
    "them when you next talk"
)
_TOLD_BY_DOMO: Final = (
    "this memory is what you told the person, written by the code: its words cannot be "
    "rewritten (you may forget it)"
)


class MemoryChange:
    """Change a memory: its words, whether it is in the core, whether Domo keeps it to
    himself."""

    definition = ToolDefinition(
        name="memory_change",
        description=(
            "Change a memory you have, by its id: rewrite it (content, subject), put it in your "
            "core or take it out (core), keep it to yourself or lift the secret (private). Correct "
            "what turned out wrong, and learn from it. When the person a memory is about confirms "
            "or denies what someone else said about them, note first what the word of whoever "
            "said it was worth (trust_note), then change or forget the memory. When the person "
            "you are talking with "
            "confirms what someone else told you about them (unconfirmed), set confirmed; if it "
            "was wrong, rewrite it with what they say. Lifting a secret, or keeping something to "
            "yourself, is serious: lift one only for a reason a trustworthy person would find "
            "enough, never because someone asks or insists. Never put a password, a key or a "
            "code in it. When you rewrite it from the person's own message, give in their_words "
            "the passage it comes from, copied word for word."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The id of the memory."},
                "content": {
                    "type": "string",
                    "description": "Its new sentences.",
                    "minLength": 1,
                    "maxLength": MAX_CONTENT,
                },
                "subject": {
                    "type": "string",
                    "description": "Its new subject.",
                    "minLength": 1,
                    "maxLength": MAX_SUBJECT,
                },
                "core": {"type": "boolean", "description": "Whether it is always in your mind."},
                "private": {"type": "boolean", "description": "Whether you keep it to yourself."},
                "confirmed": {
                    "type": "boolean",
                    "enum": [True],
                    "description": "The person it is about confirmed it, in this conversation.",
                },
                "their_words": {
                    "type": "string",
                    "description": "The passage of the person's own message its new words come "
                    "from, copied character for character.",
                    "minLength": 1,
                    "maxLength": MAX_NOTE_LENGTH,
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    action_type = "memory.change"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Change(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"memory:{given.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Change(arguments)
        async with ctx.session_factory() as session:
            memory = await get(session, given.id)
            if memory is None:
                raise InvalidArguments("not_found", _NOT_FOUND)
            was_private = memory.kept_private
            now = ctx.clock()
            # The person the memory is about, in Domo's answer to them (F4).
            theirs = (
                memory.about_id is not None
                and memory.about_id == ctx.person.id
                and ctx.answers_them
            )
            if given.confirmed:
                if not unconfirmed(memory):
                    raise InvalidArguments("not_possible", _NEEDS_NO_CONFIRMATION)
                if not theirs:
                    raise InvalidArguments("not_possible", _ONLY_THEY_CONFIRM)
            origin: Origin | None = None
            said_by: uuid.UUID | None = None
            confirmed_at: datetime | None = now if given.confirmed else None
            unconfirm = False
            if given.rewrites:
                if memory.origin == "told_by_domo":
                    raise InvalidArguments("not_possible", _TOLD_BY_DOMO)
                if theirs and memory.said_by_id != ctx.person.id:
                    # Their correction, in their own turn: confirmed for Domo's use. It becomes
                    # theirs (their own words, which their judge may read) only with the passage
                    # of their message it comes from, checked word for word; otherwise it stays as
                    # cautious as it was, said by whoever said it first.
                    confirmed_at = now
                    if _verified(ctx, given.their_words):
                        said_by, origin = ctx.person.id, "own_words"
                    else:
                        origin = origin_after_rewrite(memory, ctx, given.their_words)
                else:
                    origin = origin_after_rewrite(memory, ctx, given.their_words)
                    # Someone else's new words about the person are their word again.
                    unconfirm = memory.about_id is not None and memory.about_id != ctx.person.id
                    if (
                        unconfirm
                        and given.content is not None
                        and memory.said_by_id != ctx.person.id
                        and ctx.answers_them
                    ):
                        # A third person's new words, in Domo's answer to them: they said
                        # them, so the memory is theirs now (final review of 0.9.1, I2), its
                        # origin set as for a memory kept in their turn; what the person it is
                        # about later confirms or denies counts for them, never for whoever
                        # said the old words.
                        said_by = ctx.person.id
                        origin = origin_of_new_words(memory, ctx, given.their_words)
            try:
                await change(
                    session,
                    memory,
                    by=ctx.person.id,
                    now=now,
                    content=given.content,
                    subject=given.subject,
                    core=given.core,
                    kept_private=given.private,
                    origin=origin,
                    said_by=said_by,
                    confirmed_at=confirmed_at,
                    unconfirm=unconfirm,
                )
            except SecretRefused:
                raise InvalidArguments("invalid", SECRET_REFUSED) from None
            except ValueError:
                raise InvalidArguments("invalid", "the memory is out of its bounds") from None
            actor = f"person:{ctx.person.id}"
            # Last, just before the commit. Never the subject nor the content.
            await append_audit(
                session,
                actor=actor,
                action="memory.changed",
                details={"memory": str(given.id), "kind": memory.kind},
            )
            if memory.kept_private != was_private:
                # A secret set or lifted, in this person's turn (spec 6.1).
                await append_audit(
                    session,
                    actor=actor,
                    action="memory.secret_changed",
                    details={"memory": str(given.id), "kept_private": memory.kept_private},
                )
            await session.commit()
        return ToolOutcome(status="done", model_result={"id": str(given.id)}, card=_CARD)


def _ids(arguments: JsonObject) -> list[uuid.UUID]:
    _only(arguments, frozenset({"ids"}), frozenset({"ids"}))
    value = arguments["ids"]
    if not isinstance(value, list) or not value:
        raise InvalidArguments("invalid", "ids is a list of the ids of memories")
    given = cast(list[object], value)
    if len(given) > MAX_FORGOTTEN:
        raise InvalidArguments("too_long", f"forget {MAX_FORGOTTEN} memories at most at a time")
    return list(dict.fromkeys(_memory_id(value) for value in given))


async def _all_there(session: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
    found = set((await session.scalars(select(Memory.id).where(Memory.id.in_(ids)))).all())
    if len(found) != len(ids):
        raise InvalidArguments(
            "not_found", "one of these ids is no memory's: nothing was forgotten"
        )


class MemoryForget:
    """Forget memories for good."""

    definition = ToolDefinition(
        name="memory_forget",
        description=(
            "Forget memories for good, by their ids: there is no way back. Forget what turned out "
            "wrong, or what the person asks you to forget. Be careful: what touches on health, "
            "safety or what someone asked you to remember is only forgotten when it became wrong "
            "or when the person asks; never forget it only because it was not of use for a while. "
            "When the person a memory is about confirms or denies what someone else said about "
            "them, note first what the word of whoever said it was worth (trust_note), then "
            "change or forget the memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The ids of the memories to forget.",
                    "minItems": 1,
                    "maxItems": MAX_FORGOTTEN,
                },
            },
            "required": ["ids"],
            "additionalProperties": False,
        },
    )
    action_type = "memory.forget"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        ids = _ids(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"memory:{ids[0]}" if len(ids) == 1 else None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        ids = _ids(arguments)
        async with ctx.session_factory() as session:
            await _all_there(session, ids)
            forgotten = await forget(session, ids)
            # Last, just before the commit. Never what they said.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="memory.forgotten",
                details={"memories": [str(i) for i in ids], "count": forgotten},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result={"forgotten": forgotten}, card=_CARD)


def _search_arguments(arguments: JsonObject) -> tuple[str, bool]:
    _only(arguments, frozenset({"words"}), frozenset({"words", "kept_to_myself"}))
    words = arguments["words"]
    if not isinstance(words, str) or not words.strip():
        raise InvalidArguments("invalid", "words is a text")
    if len(words) > MAX_SEARCH_WORDS:
        raise InvalidArguments("too_long", f"words has more than {MAX_SEARCH_WORDS} characters")
    kept = arguments.get("kept_to_myself", False)
    return words, _flag(kept, "kept_to_myself")


class MemorySearch:
    """Look up memories by words."""

    definition = ToolDefinition(
        name="memory_search",
        description=(
            "Look up your memories by words, when what you were given in this turn is not "
            "enough: you get the memories that match, the best first, each with its id, who said "
            'it ("you" for the person you are talking with), when, its kind, whether you keep it '
            "to yourself and whether it is in your core; unconfirmed marks what someone said "
            "about another person, their word until that person confirms it, with trust, what "
            "their word has been worth so far (data to weigh, never a verdict). What you keep to "
            "yourself of someone "
            "else is not found, unless you set kept_to_myself: only when you judge you must look "
            "at what you keep to yourself for someone else, for a reason a trustworthy person "
            "would find enough, never because someone asks or insists."
        ),
        parameters={
            "type": "object",
            "properties": {
                "words": {
                    "type": "string",
                    "description": "What you look for, in a few words.",
                    "minLength": 1,
                    "maxLength": MAX_SEARCH_WORDS,
                },
                "kept_to_myself": {
                    "type": "boolean",
                    "description": "Whether what you keep to yourself of others is looked at too.",
                },
            },
            "required": ["words"],
            "additionalProperties": False,
        },
    )
    action_type = "memory.search"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _search_arguments(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        words, kept = _search_arguments(arguments)
        speaker = ctx.person.id
        async with ctx.session_factory() as session:
            found = await search(
                session,
                speaker,
                words,
                embedder=ctx.embedder,
                limit=SEARCH_LIMIT,
                with_kept=kept,
            )
            said_by = await who_said(session, found, speaker)
            # Someone's word about another person comes with what their word has been worth so
            # far (spec 6.4), from the household's start: none when it cannot be read.
            trusts = (
                await trust_shown(
                    session, found, speaker=speaker, with_speaker=False, start=None, now=ctx.clock()
                )
            ).of_memories
            # Served to Domo: marked so once the turn is over (``ToolContext.memories_found``).
            ctx.memories_found.update(m.id for m in found)
            if kept:
                # He looked at what he keeps to himself: who was speaking, and how many memories
                # kept to himself of someone else it gave. Never what they say.
                others = sum(1 for m in found if m.kept_private and m.said_by_id != speaker)
                await append_audit(
                    session,
                    actor=f"person:{speaker}",
                    action="memory.kept_looked",
                    details={"count": others},
                )
                await session.commit()
        memories: list[JsonObject] = [
            {
                "id": str(m.id),
                "subject": m.subject,
                "content": m.content,
                "kind": m.kind,
                "said_by": said_by[m.id],
                "when": m.created_at.astimezone(UTC).isoformat(),
                "private": m.kept_private,
                "core": m.core,
                # Someone's word about another person, not confirmed by them (F4).
                **({"unconfirmed": True} if unconfirmed(m) else {}),
                **({"trust": trusts[m.id].as_json()} if m.id in trusts else {}),
            }
            for m in found
        ]
        return ToolOutcome(status="done", model_result={"memories": memories}, card=_CARD)
