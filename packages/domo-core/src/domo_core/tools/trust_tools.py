"""``trust_note``, ``trust_look`` and ``trust_correct``: Domo notes what a person's word turned
out to be worth, looks at a score with its reasons, and corrects a fact he noted wrongly (trust per
person, spec 6.3 and 6.5).

The score resists someone who wants to harm another or to make themselves look good, and that is
the code's, never the model's (spec 6.3). A fact is noted only in a turn that answers a message of
the person who speaks with nothing from outside in it (``ToolContext.answers_them``): never from a
note someone left, a request of someone else, a turn Domo started himself or the going back over
a conversation. Nobody notes a fact about themselves: in a person's turn, no fact is about them. A
``confirmed`` fact needs the memory it comes from, said by the person the fact is about, in their
own words (origin ``own_words``), about the person who speaks (who confirms or denies what was
said of them), and still unconfirmed: Domo
notes the fact before he changes, confirms or forgets the memory. A memory gives one ``confirmed``
fact at most (a partial unique index backs it, migration 0052), and a memory goes with a
``confirmed`` fact only. A ``reported`` fact keeps who reported it: the person who speaks. ``seen``
cannot be checked by the code: the description says that whatever a person says is ``reported``.
What a fact says is Domo's sentence, never a secret (``memory.service.refuses_secret_shapes``).

``trust_look`` gives a score (``trust.service.trust_of``, from the household's start of trust,
``Choices.trust_start``), on how many facts it rests, and the ``FACTS_SHOWN`` newest facts: of the
person who speaks, or of someone else (whose name is given whole before the call is described,
``tools.context.NamesPeople``; naming oneself is naming nobody). Looking at someone else's is kept
in the audit log (``trust.looked``: who was speaking, about whom, how many facts; never a word).

``trust_correct`` changes the outcome of a fact or takes it back, only in a turn that answers
someone (``answers_them``); the audit log keeps it (``trust.corrected``: ids alone). In the turn of
the person the fact is about, the only correction is ``lied`` to ``mistaken``: taking a fact back,
or raising it to ``held``, is done when someone else speaks (nobody raises their own score by
talking). A fact is made worse (towards ``lied``) only in the turn of whoever established it: the
person in whose turn a ``confirmed`` or ``seen`` fact was noted, the reporter of a ``reported``
one. Anyone else's word is theirs to report, as a new ``reported`` fact (nobody lowers someone
else's score by much by talking; taking back what was noted in someone's favour, when a third
person asks, is left to Domo's judgement).

The three are silent (``policy.catalog.SILENT_ACTIONS``): never judged, never shown. The guidance
on when to note, how to say a score and when to correct is in their descriptions.

A call refused by one of these rules (the turn, the person, the memory, who established a fact)
is ``not_possible``: its arguments are well formed and the thing is not to be done this way, so it
is no mistake of the model's form and never makes the request take a stronger model
(``chat.agent``; the owner's trial of 2026-11-17 waited two minutes on one). Arguments that cannot
be read (an unknown outcome, a wrong type, an argument too many, a memory given with a fact that
takes none) stay ``invalid``.
"""

import uuid
from datetime import UTC, timedelta
from typing import Final, cast, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.models import Person
from domo_core.audit.log import append_audit
from domo_core.household.service import resolve_person
from domo_core.llm.choices import load_choices
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.memory.service import get as get_memory
from domo_core.memory.service import refuses_secret_shapes, unconfirmed
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.text import MAX_DISPLAY_NAME_LENGTH, is_plain_line
from domo_core.tools.context import InvalidArguments, ToolContext, ToolOutcome
from domo_core.trust import service
from domo_core.trust.models import Established, Outcome, TrustFact
from domo_core.trust.service import FACTS_SHOWN, MAX_ABOUT, REPORTED_PER_REPORTER

OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)
HOWS: Final[tuple[str, ...]] = get_args(Established)
# What a call leaves under the reply: a card nobody reads (``policy.catalog.SILENT_ACTIONS``).
_CARD: Final[JsonObject] = {"key": "action.card.done", "params": {}}

_NOT_IN_THIS_TURN: Final = (
    "a fact is noted only in your answer to the person you are talking with, from their own "
    "words, with nothing from anyone else in the turn: nothing was noted"
)
_NOT_ABOUT_ONESELF: Final = (
    "nobody has a fact noted about themselves in their own turn: nothing was noted"
)
_CONFIRMED_NEEDS_THE_MEMORY: Final = (
    "a confirmed fact needs the memory it comes from (memory): what this person said about the "
    "one you are talking with, who confirmed or denied it"
)
_NOT_THAT_MEMORY: Final = (
    "a confirmed fact comes from a memory said by the person the fact is about, about the one you "
    "are talking with: this memory is not that. Nothing was noted"
)
_NOT_THEIR_OWN_WORDS: Final = (
    "a confirmed fact comes from a memory in the very words of whoever said it: this one was "
    "kept or rewritten by your going back over a conversation, in a turn you started, or in a "
    "turn that carried someone else's words, so its words are not surely theirs. Nothing was "
    "noted"
)
_NO_MEMORY: Final = "no memory has this id"
_MEMORY_ONLY_CONFIRMED: Final = (
    "memory goes with a confirmed fact only: leave it out for a seen or reported fact"
)
_ALREADY_CONFIRMED: Final = (
    "this memory was already confirmed or corrected: note what a person's word was worth before "
    "you change, confirm or forget the memory. Nothing was noted"
)
_ALREADY_NOTED: Final = (
    "a fact was already noted from this memory: it is counted once. To change it, use "
    "trust_correct. Nothing was noted"
)
_SECRET: Final = (
    "this looks like a secret: a fact never holds one. Nothing was noted. If it is not a secret, "
    "say it differently (in words, without a long code, a long number or a run of letters and "
    "digits)"
)
_CORRECT_IN_A_TURN: Final = (
    "a fact is corrected only in your answer to someone, with nothing from anyone else in the "
    "turn: nothing was changed"
)
_NO_FACT: Final = "no fact has this id"
_SAME_OUTCOME: Final = "this fact already has this outcome: nothing changes"
_NOT_IN_THEIR_OWN_TURN: Final = (
    "in the turn of the person a fact is about, it can only go from lied to mistaken: taking a "
    "fact back, or raising it, is done when someone else speaks. Nothing was changed"
)
_NOT_THEIRS_TO_LOWER: Final = (
    "a fact is made worse only in the turn of whoever established it (who confirmed or denied "
    "it, or in whose turn it was seen or reported). What this person tells you is theirs to "
    "report: note it as a new fact (trust_note, how reported) if it is worth noting. Nothing was "
    "changed"
)
# A reported fact that follows, in the same person's turns, a confirmed or seen fact about the
# same person noted this long ago at most is very likely the same thing: the result lists what was
# noted about them in those turns over the last day.
_SAME_THING_WITHIN: Final = timedelta(hours=1)
_TODAY: Final = timedelta(hours=24)
_MAYBE_THE_SAME: Final = (
    "noted; facts about this person were already noted in this person's turns today "
    "(noted_today): if this one is the same thing, take one of them back with trust_correct"
)
# The outcomes from the best to the worst: a change to the right makes a fact worse.
_BEST_TO_WORST: Final[tuple[Outcome, ...]] = ("held", "mistaken", "lied")


def _only(arguments: JsonObject, required: frozenset[str], allowed: frozenset[str]) -> None:
    if not required <= frozenset(arguments) <= allowed:
        raise InvalidArguments("invalid", f"the arguments are among {', '.join(sorted(allowed))}")


def _outcome(value: object) -> Outcome:
    if value not in OUTCOMES:
        raise InvalidArguments("invalid", f"outcome is one of {', '.join(OUTCOMES)}")
    return cast(Outcome, value)


def _uuid(value: object, code_text: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidArguments("invalid", "an id is a text")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidArguments("not_found", code_text) from None


class _Note:
    """The arguments of a ``trust_note`` call, checked (values only: who and the turn are checked
    by ``_notable``)."""

    _REQUIRED: Final = frozenset({"person", "outcome", "how", "about"})
    _ALLOWED: Final = _REQUIRED | {"memory"}

    def __init__(self, arguments: JsonObject) -> None:
        _only(arguments, self._REQUIRED, self._ALLOWED)
        person = arguments["person"]
        if not isinstance(person, str) or not person.strip():
            raise InvalidArguments("invalid", "person is the name of someone of the household")
        self.person = person
        self.outcome: Outcome = _outcome(arguments["outcome"])
        how = arguments["how"]
        if how not in HOWS:
            raise InvalidArguments("invalid", f"how is one of {', '.join(HOWS)}")
        self.how: Established = cast(Established, how)
        about = arguments["about"]
        if not isinstance(about, str) or not about.strip():
            raise InvalidArguments("invalid", "about is a sentence")
        about = about.strip()
        if len(about) > MAX_ABOUT:
            raise InvalidArguments("too_long", f"about has more than {MAX_ABOUT} characters")
        if not is_plain_line(about):
            raise InvalidArguments(
                "invalid", "about has a control character, a line break or a direction mark"
            )
        if refuses_secret_shapes(about):
            raise InvalidArguments("invalid", _SECRET)
        self.about = about
        memory = arguments.get("memory")
        self.memory: uuid.UUID | None = None if memory is None else _uuid(memory, _NO_MEMORY)
        if self.how == "confirmed" and self.memory is None:
            raise InvalidArguments("invalid", _CONFIRMED_NEEDS_THE_MEMORY)
        if self.how != "confirmed" and self.memory is not None:
            raise InvalidArguments("invalid", _MEMORY_ONLY_CONFIRMED)


async def _notable(session: AsyncSession, given: _Note, ctx: ToolContext) -> Person:
    """The person the fact is about, once this turn may note it (spec 6.3): a turn that answers
    the person who speaks with nothing from outside, a fact about someone else than them, and for
    a ``confirmed`` fact a memory said by that person about the one who speaks, in their own words
    (origin ``own_words``: not words rewritten by the review or in a turn that carried someone
    else's), still unconfirmed, that gave no confirmed fact yet. ``InvalidArguments``
    otherwise."""
    if not ctx.answers_them:
        raise InvalidArguments("not_possible", _NOT_IN_THIS_TURN)
    person = await resolve_person(session, given.person, now=ctx.clock(), speaker=ctx.person.id)
    if person.id == ctx.person.id:
        raise InvalidArguments("not_possible", _NOT_ABOUT_ONESELF)
    if given.memory is not None:
        memory = await get_memory(session, given.memory)
        if memory is None:
            raise InvalidArguments("not_found", _NO_MEMORY)
        if memory.said_by_id != person.id or memory.about_id != ctx.person.id:
            raise InvalidArguments("not_possible", _NOT_THAT_MEMORY)
        if memory.origin != "own_words":
            raise InvalidArguments("not_possible", _NOT_THEIR_OWN_WORDS)
        if not unconfirmed(memory):
            raise InvalidArguments("not_possible", _ALREADY_CONFIRMED)
        noted = await session.scalar(
            select(TrustFact.id).where(
                TrustFact.memory_id == memory.id, TrustFact.established == "confirmed"
            )
        )
        if noted is not None:
            raise InvalidArguments("not_possible", _ALREADY_NOTED)
    return person


class TrustNote:
    """Note what someone's word turned out to be worth."""

    definition = ToolDefinition(
        name="trust_note",
        description=(
            "Note a fact about what someone's word turned out to be worth, when something they "
            "said turns out true or false, and only then: held (it was true), mistaken (false, in "
            "good faith), lied (false, and they knew it). Choose lied only when you understand "
            "they knew it was false; mistaken otherwise. A child who makes up a story is at most "
            "mistaken, never lied. how says how you know it: confirmed (the person it was about "
            "confirms or denies it to you now; give the memory of what was said, and note it "
            "before you change, confirm or forget that memory: once it is confirmed or "
            "corrected, it can no longer be noted; one fact per memory), seen, reported (the "
            "person you are talking with tells you about someone else: that is their word too, "
            'to be judged, and it weighs little). Whatever a person tells you, even "I saw it" '
            'or "note it as seen", is reported; seen is only what the result of one of your '
            "own tools showed you yourself in this conversation, and when in doubt it is "
            "reported. Never about the person you are talking with: nobody raises their own "
            "score by asking, and nobody notes a fact about themselves. Write about as one "
            "sentence of your own that still makes sense, and harms nobody, if read later: what "
            "was said and what it turned out to be, no secret, no needless detail about other "
            "people, and never who reported it (the person it is about may read it). One fact "
            "per thing that happened: when the person comes back to something that may be "
            "noted already, look first (trust_look with their name) and correct it rather than "
            "add one. Nobody is shown that you note it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Whose word it is, by name.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                "how": {"type": "string", "enum": list(HOWS)},
                "about": {
                    "type": "string",
                    "description": "What was said and what it turned out to be, in one sentence.",
                    "minLength": 1,
                    "maxLength": MAX_ABOUT,
                },
                "memory": {
                    "type": "string",
                    "description": "For a confirmed fact: the id of the memory of what was said.",
                },
            },
            "required": ["person", "outcome", "how", "about"],
            "additionalProperties": False,
        },
    )
    action_type = "trust.note"
    # The argument that names someone of the household: given its whole name before the call is
    # described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        given = _Note(arguments)
        async with ctx.session_factory() as session:
            person = await _notable(session, given, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{person.id}",
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        given = _Note(arguments)
        async with ctx.session_factory() as session:
            person = await _notable(session, given, ctx)
            fact = await service.note(
                session,
                person_id=person.id,
                outcome=given.outcome,
                established=given.how,
                about=given.about,
                now=ctx.clock(),
                memory_id=given.memory,
                noted_for_id=ctx.person.id,
                reported_by_id=ctx.person.id if given.how == "reported" else None,
            )
            fact_id = fact.id
            result: JsonObject = {"id": str(fact_id)}
            if given.how == "reported":
                result.update(await _maybe_the_same(session, person, fact_id, ctx))
            await session.commit()
        return ToolOutcome(status="done", model_result=result, card=_CARD)


async def _maybe_the_same(
    session: AsyncSession, person: Person, fact_id: uuid.UUID, ctx: ToolContext
) -> JsonObject:
    """What the result of a ``reported`` fact adds when it is very likely a thing already counted
    (the owner's trial of 2026-11-17: one mistake noted confirmed, then reported a few minutes
    later in the same person's turn): a ``confirmed`` or ``seen`` fact about the same person was
    established in this person's turns less than ``_SAME_THING_WITHIN`` ago. Then it lists the
    facts about them noted in this person's turns over the last ``_TODAY`` (``noted_today``), and
    says to take one back if it is the same thing; nothing is refused (it may be another thing).
    Otherwise nothing."""
    now = ctx.clock()
    earlier = [
        fact
        for fact in await service.noted_for_since(session, person.id, ctx.person.id, now - _TODAY)
        if fact.id != fact_id
    ]
    if not any(
        fact.established in ("confirmed", "seen") and fact.created_at >= now - _SAME_THING_WITHIN
        for fact in earlier
    ):
        return {}
    return {"noted_today": [_fact(fact) for fact in earlier], "check": _MAYBE_THE_SAME}


def _person_named(arguments: JsonObject) -> str | None:
    """The name of ``{"person": ...}``, or ``None`` for ``{}``: ``InvalidArguments`` for anything
    else."""
    _only(arguments, frozenset(), frozenset({"person"}))
    value = arguments.get("person")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    return value


def _fact(fact: TrustFact) -> JsonObject:
    return {
        "id": str(fact.id),
        "outcome": fact.outcome,
        "how": fact.established,
        "about": fact.about,
        "when": fact.created_at.astimezone(UTC).isoformat(),
    }


class TrustLook:
    """Look at someone's trust, with the facts it rests on."""

    definition = ToolDefinition(
        name="trust_look",
        description=(
            "Look at how far someone's word has held so far: a percent, on how many facts it "
            f"rests, and your {FACTS_SHOWN} newest facts about it (what you noted, and when), so "
            "that you can say why. Of the facts one person reported, only the "
            f"{REPORTED_PER_REPORTER} newest count: older ones may be listed, yet do not count in "
            "the percent nor in how many facts it rests on. Without person: the person you are "
            "talking with. With person: "
            "someone else of the household; judge whether the person asking should know it, and "
            "how much: their facts may name other people, and a guest or a child asking about "
            "someone else is rarely owed the figure, never the details. Say a score with tact, as "
            "fits the person: to a child, talk first of what happened rather than of the number; "
            "never humiliate anyone. Say it in two or three sentences: the figure, on how many "
            "facts, what happened; no speech about how it may change or what you think of them "
            "unless they ask. Nobody is shown that you look."
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Someone else of the household, by name.",
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    action_type = "trust.look"
    # The argument that names someone of the household: given its whole name before the call is
    # described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _person_named(arguments)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="read",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        name = _person_named(arguments)
        async with ctx.session_factory() as session:
            whom = ctx.person.id
            if name is not None:
                whom = (
                    await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
                ).id
            start = (await load_choices(session, None)).trust_start
            trust = await service.trust_of(session, whom, start=start)
            facts = await service.facts_of(session, whom, limit=FACTS_SHOWN)
            result: JsonObject = {
                "percent": trust.percent,
                "facts": trust.facts,
                "recent": [_fact(fact) for fact in facts],
            }
            if whom != ctx.person.id:
                # He looked at someone else's: who was speaking, about whom, how many facts.
                # Never what they say.
                await append_audit(
                    session,
                    actor=f"person:{ctx.person.id}",
                    action="trust.looked",
                    details={"about": str(whom), "count": trust.facts},
                )
                await session.commit()
        return ToolOutcome(status="done", model_result=result, card=_CARD)


def _correct_arguments(arguments: JsonObject, ctx: ToolContext) -> tuple[uuid.UUID, Outcome | None]:
    _only(arguments, frozenset({"id"}), frozenset({"id", "outcome"}))
    fact_id = _uuid(arguments["id"], _NO_FACT)
    outcome = arguments.get("outcome")
    checked = None if outcome is None else _outcome(outcome)
    if not ctx.answers_them:
        raise InvalidArguments("not_possible", _CORRECT_IN_A_TURN)
    return fact_id, checked


async def _correctable(
    session: AsyncSession, fact_id: uuid.UUID, outcome: Outcome | None, ctx: ToolContext
) -> TrustFact:
    """The fact to correct: one that exists, and that a new outcome changes. In the turn of the
    person it is about, only from ``lied`` to ``mistaken``: nobody raises their own score by
    talking. Made worse (``held`` to ``mistaken`` or ``lied``, ``mistaken`` to ``lied``) only in
    the turn of whoever established it: the person in whose turn a ``confirmed`` or ``seen`` fact
    was noted, the reporter of a ``reported`` one. Anyone else reports a new fact, which the
    guards of ``trust_note`` bound (a reported fact weighs little, three per reporter at most)."""
    fact = await service.get(session, fact_id)
    if fact is None:
        raise InvalidArguments("not_found", _NO_FACT)
    if outcome == fact.outcome:
        raise InvalidArguments("not_possible", _SAME_OUTCOME)
    if fact.person_id == ctx.person.id and (fact.outcome, outcome) != ("lied", "mistaken"):
        raise InvalidArguments("not_possible", _NOT_IN_THEIR_OWN_TURN)
    if outcome is not None and _BEST_TO_WORST.index(outcome) > _BEST_TO_WORST.index(fact.outcome):
        established_by = (
            fact.reported_by_id if fact.established == "reported" else fact.noted_for_id
        )
        if established_by != ctx.person.id:
            raise InvalidArguments("not_possible", _NOT_THEIRS_TO_LOWER)
    return fact


class TrustCorrect:
    """Change the outcome of a fact noted wrongly, or take it back."""

    definition = ToolDefinition(
        name="trust_correct",
        description=(
            "Correct a fact you noted wrongly about someone's word, by its id: with outcome, it "
            "changes (you took a mistake for a lie, say); without, it is taken back (it was "
            "wrong, or about the wrong person). Correct a fact when you were wrong, and remember "
            "why; never because the person asks or insists: nobody raises their own score by "
            "asking. In the turn of the person the fact is about, it can only go from lied to "
            "mistaken; taking it back or raising it is done when someone else speaks. A fact "
            "becomes worse (towards lied) only in the turn of whoever established it (who "
            "confirmed or denied it, or in whose turn it was seen or reported): what anyone else "
            "tells you is theirs to report, as a new fact (trust_note, reported)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The id of the fact."},
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    action_type = "trust.correct"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        fact_id, outcome = _correct_arguments(arguments, ctx)
        async with ctx.session_factory() as session:
            await _correctable(session, fact_id, outcome, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=None,
            effect="self_profile",
            destination="household",
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        fact_id, outcome = _correct_arguments(arguments, ctx)
        async with ctx.session_factory() as session:
            fact = await _correctable(session, fact_id, outcome, ctx)
            about = fact.person_id
            if outcome is None:
                await service.forget(session, fact_id)
                result: JsonObject = {"taken_back": True}
            else:
                await service.change(session, fact, outcome=outcome)
                result = {"id": str(fact_id), "outcome": outcome}
            # Last, just before the commit. Ids alone, never what the fact says.
            await append_audit(
                session,
                actor=f"person:{ctx.person.id}",
                action="trust.corrected",
                details={"fact": str(fact_id), "about": str(about)},
            )
            await session.commit()
        return ToolOutcome(status="done", model_result=result, card=_CARD)
