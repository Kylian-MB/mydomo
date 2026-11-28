"""The tools of the voice (spec 6.2, 6.5, 6.9): ``spoken_phrase_agree``, ``spoken_phrase_change``,
``spoken_phrase_remove``, ``voice_stop_listening``, ``voice_unlearn`` and
``speaker_manner_note``.

The spoken phrase is a means of proof said aloud (``proofs.kinds.SpokenPhraseProof``). These tools
never see it. Agreeing or changing it OPENS a reserved moment (``channels.voice.moments``): the
person's next two sayings (the phrase, then its repetition) go from the voice channel to the
vault, never to a model, the discussion, the journal or a log. Removing it asks the vault to
forget it (``phrase_remove``) and takes the means away (``proofs.service.remove``): the two go
together.

Agreeing and changing are the person's own and nobody else's (ruling T11), in a turn that
answers them (``ToolContext.answers_them``; words of another member, a note or a program's text
in the turn, even alone, have the judge weigh that it is their wish, told which program:
``DescribedAction.whose_judged``; a turn with no words of anyone and no text refuses it), or
that their own yes runs (``approved_by``: a call the judge held for their yes); the speaker is
them (one the code is not sure of is weighed by the judge, never refused); and, when the person
has another means of proof (a password, a passkey), after a proof by it (a clue of the speaker).
The vault's count of near misses is reset by a good agreement, so nothing else may reach that
path. Agreeing a phrase starts afresh: a phrase still stored at the vault is removed first, never
revived. A phrase said aloud can be heard by the room: the assistant says so once, to each person,
and it is remembered as told (``WEAKNESS_TOLD``, an audit entry of the person) once a first
saying reached the voice channel (``remember_the_weakness_told``): the result says whether it is
still to be said (``tell_the_weakness``).

Removing the phrase and "that was not me" asked by a person are theirs, and nobody speaking at
their place has their say (the speaker, when there is one, is them); a speaker the code is not sure
of is weighed by the judge. Someone else's phrase may be asked removed by the owner, or by an
adult for a child (``confirmations.who_agrees.may_agree_for_a_child``, the say an adult has over a
child's requests): never refused by code, always weighed by the judge, told whose phrase, their
role and whether the one who asks invited them (the owner, 2026-11-27: "the assistant judges it");
a judge that does not answer leaves it in place, no proof of the one who asks standing in for its
verdict (``DescribedAction.verdict_needed``): they may ask again later. Once removed, that
person is left a word of his own (``PHRASE_REMOVED_WORD``). Anyone else: refused.
Agreeing or changing a phrase stays its holder's alone (a proof: only they set it). The spoken
phrase is never an idea of the assistant's (a call judged as his is refused). What was
learnt of a voice, though, is his own recording (the owner, Task 27): he may erase anyone's, or an
unknown voice's, as his own idea (``act_on_my_own``, a call judged as his: ``ToolContext.acting``),
naming whose; the barrier and his judge weigh it as any idea of his, and it waits for the person it
concerns when one is asked (``policy.subjects``: the person the target names, not the person of the
turn). A known person's voice, where someone spoke or words of the house came in, is weighed by the
judge from the context (``whose_judged``: final fix wave, case 4; a person may ask it for someone
else's voice too, a parent for their child, weighed the same way); a program's or a site's text
alone is weighed so too, the judge told which program (the owner, 2026-11-26: it all depends on
the source). An unknown voice's, in any turn. Once a known person's prints went as his
idea, or on someone else's wish, he leaves them a word of his own (``Note.by_domo``, news for them),
in his words (``VOICE_ERASED_WORD``, a text of his): never silently. Stopping the listening is the
person's own words too (a program's text, alone or beside a person's words or a note: the judge
weighs whose wish it is, told which program; no text and no words: refused). It turns the person's
listening pages off at once (``channels.voice.live.LIVE_VOICES``: the page saying this reply, else
all of theirs), and keeps nothing to act on later (it is never shown:
``policy.catalog.SILENT_ACTIONS``). "That was not me" (``voice_unlearn``) takes back what was learnt
of the person's own voice since a time, or all of it (``channels.voice.prints.unlearn``); an unknown
voice he erases goes whole (``channels.voice.prints.erase_unknown``). Either cannot be undone.

``speaker_manner_note`` keeps Domo's own reading of how the one who just spoke speaks (their
words, their way of putting things), for or against their being who the code takes them for: a
weak clue of his (``identity.clues.keep_manner_clue``), read with the next sentences said there
(spec 6.5). It never changes how sure the code is (``conversation.speaker.standing``); the judge
weighs it beside the code's clues. It is never shown, calls no model, and is refused in a turn with
text from outside (``ToolContext.outside_text``), as a trust fact is.

Nothing here keeps, logs or shows a word that was said.
"""

import re
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit
from domo_core.audit.models import AuditEntry
from domo_core.conversation.speaker import Clue, Speaker
from domo_core.gatekeeper_client import GatekeeperError
from domo_core.household.service import resolve_person
from domo_core.identity.clues import keep_manner_clue
from domo_core.llm.types import JsonObject, ToolDefinition
from domo_core.policy.actions import DescribedAction
from domo_core.policy.roles import Actor
from domo_core.proofs.kinds import phrase_ref
from domo_core.proofs.service import means_of, remove
from domo_core.text import MAX_DISPLAY_NAME_LENGTH
from domo_core.tools.context import (
    InvalidArguments,
    ToolContext,
    ToolOutcome,
    nobody_spoke_in,
)

if TYPE_CHECKING:
    from domo_core.accounts.models import Person
    from domo_core.channels.voice.moments import Purpose

# The voice channel is looked up when a tool runs, never at import: the conversation loads the tool
# registry, and loading the conversation must load no channel (its boundary test).

# The audit entry that says the person was told that a phrase said aloud can be heard by the room.
WEAKNESS_TOLD: Final = "voice.phrase_weakness_told"
# The most minutes back "that was not me" may reach: a year.
MAX_SINCE_MINUTES: Final = 366 * 24 * 60
_PHRASE: Final = "spoken_phrase"
# The most characters of why Domo reads someone's manner so (``speaker_manner_note``).
MAX_WHY_LENGTH: Final = 200

_FAILED_CARD: Final[JsonObject] = {"key": "action.card.failed", "params": {}}
_OPENED_CARD: Final = "action.card.phrase_agree_opened"
_NO_ARGUMENTS: Final = "this tool takes no argument"
_NOT_THEIR_WORDS: Final = (
    "nobody's words and no text are in this turn: this is done only from the person's own words: "
    "ask them first, and do it when they answer"
)
_NOWHERE_TO_SAY: Final = "a phrase cannot be said aloud here: this place has no microphone for it"
_NOT_SURE: Final = "it is done only for the person themself, speaking for themself"
_ASK_ANOTHER_PROOF: Final = (
    "the person has another means of proof (a password, a passkey): they give a proof by it first, "
    "and then this is done"
)
_NEVER_HIS: Final = (
    "the spoken phrase is the person's own: it is never an idea of yours, only their own wish"
)
_NOT_THEIRS: Final = (
    "a person forgets only their own voice; another's, or a voice nobody knows, goes only as your "
    "own idea"
)
_NAME_WHOSE: Final = "as your own idea, say whose voice: a person, or an unknown voice"
_NOT_THE_SAME: Final = "the name no longer means the person this was asked for: nothing was done"
# The word he leaves someone whose learnt voice he erased as his own idea (a text of his).
VOICE_ERASED_WORD: Final = "voice.prints_erased_word"
# The word he leaves someone whose spoken phrase was removed at someone else's request.
PHRASE_REMOVED_WORD: Final = "voice.phrase_removed_word"
_NOT_YOURS_TO_REMOVE: Final = (
    "someone else's phrase is removed only when the owner asks, or an adult for a child: "
    "nothing was done; that person removes it themself"
)
_A_KNOWN_VOICE: Final = "this voice is someone's: name the person instead"
_NO_SUCH_VOICE: Final = "no voice is kept under this reference"
# An unknown voice's reference, as the voice keeps it (``channels.voice.models``).
_REFERENCE: Final = re.compile(r"v-[0-9a-f]{16}")
_ALREADY: Final = "a phrase is already agreed: change it instead"
_NONE_YET: Final = "no phrase is agreed yet: agree one first"


def _described(action_type: str, target: str, *, whose_judged: bool = False) -> DescribedAction:
    """Whose voice or phrase it is (``person:<id>``, ``voice:<reference>``), nothing more: never
    a word that was said; ``whose_judged``: the judge weighs whether it is really theirs."""
    return DescribedAction(
        connector="core",
        action_type=action_type,
        target=target,
        effect="self_profile",
        destination="household",
        whose_judged=whose_judged,
    )


def _nothing(arguments: JsonObject) -> None:
    if arguments:
        raise InvalidArguments("invalid", _NO_ARGUMENTS)


def _theirs(ctx: ToolContext) -> str:
    return f"person:{ctx.person.id}"


def _not_his(actor: Actor) -> None:
    """The spoken phrase is the person's own: a call judged as the assistant's is refused."""
    if actor.subject == "domo":
        raise InvalidArguments("not_possible", _NEVER_HIS)


def _their_own_words(ctx: ToolContext) -> bool:
    """Whether the judge must weigh that the call is really the person's wish
    (``DescribedAction.whose_judged``), else ``InvalidArguments``. Not weighed: the turn answers
    the person themself with nothing else in it, or their own yes runs a call held for it.
    Weighed (the owner's rule, 2026-11-26: it all depends on the source, and the judge weighs it
    like a person would): words not theirs were read in the turn (a member's note, someone's
    request, an approver's word: ``told_outside``; a note born of a source nobody of the house is
    known for, a voice nobody knows or a program's text read before he wrote it:
    ``told_unknown``, which the judge is told by name), beside the person's words or in a turn
    with none (a wake, the note alone); or a plan of theirs whose time came keeps their own words
    (``asked_before``); and a program's, a site's or a tool's text (``tool_woke``, ``outside``)
    beside any of these or alone (the owner, 2026-11-26: the assistant tells each source apart,
    and the judge weighs it like a person would; he is told which kinds, which program and what
    it is built to reach, and that nobody wrote when nobody did: ``ToolContext.outside_sources``).
    Refused: a turn with no words of anyone (nobody asked, no note) and no such text."""
    if ctx.approved_by == ctx.person.id:
        return False
    if not _words_of_someone(ctx) and not _a_programs_text(ctx):
        raise InvalidArguments("not_possible", _NOT_THEIR_WORDS)
    return ctx.reply_message_id is None or ctx.outside_text or ctx.told_unknown


def _words_of_someone(ctx: ToolContext) -> bool:
    """Whether the turn holds words of a person or a note (his own or a member's, whatever its
    mark), something to judge: the person's message (``reply_message_id``), words told to the
    turn (``told_outside``, ``told_unknown``), or a plan of theirs keeping their words
    (``asked_before``). A program's, a site's or a tool's raw text is none of them."""
    return (
        ctx.reply_message_id is not None or ctx.told_outside or ctx.told_unknown or ctx.asked_before
    )


def _themself(ctx: ToolContext) -> None:
    """The speaker, when there is one, is the person themself: removing a phrase or forgetting a
    voice is for the person alone, never for someone speaking at their place nor for another
    person. A voice the code is not sure of is the judge's to weigh (the barrier marks it
    ``judge_identity``: these types are never ordinary), never refused here (final fix wave,
    case 2)."""
    speaker = ctx.speaker
    if speaker is not None and speaker.person_id != ctx.person.id:
        raise InvalidArguments("not_possible", _NOT_SURE)


def _proof_by_another_means(clues: tuple[Clue, ...], other_means: set[str]) -> bool:
    return any(
        clue.kind == "proof"
        and clue.weighs == "for"
        and clue.source == "code"
        and clue.means in other_means
        for clue in clues
    )


async def _may_agree(session: AsyncSession, ctx: ToolContext, *, change: bool) -> bool:
    """Whether ``ctx`` may open the two sayings of a phrase (ruling T11), else
    ``InvalidArguments`` (``not_possible``); whether the judge weighs that it is theirs
    (``_their_own_words``)."""
    judged = _their_own_words(ctx)
    if _PHRASE not in ctx.abilities.proof_kinds:
        raise InvalidArguments("not_possible", _NOWHERE_TO_SAY)
    speaker = ctx.speaker
    # Someone else, or nobody: never. Not sure of them: the judge weighs it (case 2).
    if speaker is None or speaker.person_id != ctx.person.id:
        raise InvalidArguments("not_possible", _NOT_SURE)
    held = {mean.kind for mean in await means_of(session, ctx.person, ctx.clock())}
    if change and _PHRASE not in held:
        raise InvalidArguments("not_possible", _NONE_YET)
    if not change and _PHRASE in held:
        raise InvalidArguments("not_possible", _ALREADY)
    other = held - {_PHRASE}
    if other and not _proof_by_another_means(speaker.clues, other):
        raise InvalidArguments("not_possible", _ASK_ANOTHER_PROOF)
    return judged


async def _weakness_still_to_tell(session: AsyncSession, person_id: uuid.UUID) -> bool:
    told = await session.scalar(
        select(AuditEntry.id)
        .where(AuditEntry.actor == f"person:{person_id}", AuditEntry.action == WEAKNESS_TOLD)
        .limit(1)
    )
    return told is None


async def remember_the_weakness_told(session: AsyncSession, person_id: uuid.UUID) -> None:
    """The voice channel received a first saying of a phrase from ``person_id``: the weakness was
    told (the assistant says it when he opens the moment), and it is remembered, once
    (``WEAKNESS_TOLD``). Not committed: the audit log holds a lock until the transaction ends, so
    the caller commits soon after."""
    if await _weakness_still_to_tell(session, person_id):
        await append_audit(session, actor=f"person:{person_id}", action=WEAKNESS_TOLD, details={})


async def _open_the_sayings(
    ctx: ToolContext, purpose: "Purpose", card_key: str, *, afresh: bool
) -> ToolOutcome:
    """Open the moment of the two sayings of ``ctx.person``: after the checks, and, for a phrase
    agreed afresh, after the vault forgot any phrase it still held (never revived). The
    assistant is told whether the room's hearing is still to be said, and it is remembered as
    said."""
    from domo_core.channels.voice.moments import open_moment

    now = ctx.clock()
    async with ctx.session_factory() as session:
        await _may_agree(session, ctx, change=purpose == "phrase_change")
    if afresh:
        try:
            await ctx.gatekeeper.phrase_remove(phrase_ref(ctx.person.id))
        except GatekeeperError:
            return ToolOutcome(status="failed", model_result={"reason": "vault"}, card=_FAILED_CARD)
    async with ctx.session_factory() as session:
        await open_moment(
            session, person_id=ctx.person.id, microphone=None, purpose=purpose, now=now
        )
        tell = await _weakness_still_to_tell(session, ctx.person.id)
        await session.commit()
    result: JsonObject = {"moment": purpose, "tell_the_weakness": tell}
    return ToolOutcome(status="done", model_result=result, card=_card(card_key))


def _card(key: str) -> JsonObject:
    return {"key": key, "params": {}}


_WHAT_ABOUT_THE_PHRASE: Final = (
    "The phrase itself is never repeated, written or asked in the discussion: it is said aloud, "
    "and goes straight to the vault, never to you. "
)
_AGREE_RULES: Final = (
    "A phrase said aloud can be heard by whoever is in the room: say that weakness once, in plain "
    "words and in their language, the first time you agree one with them (the result says "
    "whether it is still to be told: tell_the_weakness), and not again. They may refuse a "
    "spoken phrase and ask for another proof instead. When they have another means of proof (a "
    "password, a passkey), they must have given it first: the call is refused otherwise. "
    "Refused where no phrase can be said (no microphone), and when the speaker is someone else."
)


class SpokenPhraseAgree:
    """Open the two sayings of a new spoken phrase (the person's own)."""

    definition = ToolDefinition(
        name="spoken_phrase_agree",
        description=(
            "Agree with the person a secret phrase that they will say aloud to prove who they are "
            "(a proof said aloud). Only for themselves, in a turn that answers their own words: "
            "when they ask for it, or after you propose it and they accept. It opens a short "
            "reserved moment: the next two things they say (the phrase, then the same phrase "
            "again) go straight to the vault, not to you, not into the discussion, the journal "
            "or a log. So tell them, in your reply, to say it now and then once more; do not "
            "answer what they say next. " + _WHAT_ABOUT_THE_PHRASE + _AGREE_RULES
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "proof.phrase_agree"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _nothing(arguments)
        _not_his(actor)
        async with ctx.session_factory() as session:
            judged = await _may_agree(session, ctx, change=False)
        return _described(self.action_type, _theirs(ctx), whose_judged=judged)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _nothing(arguments)
        _not_his(ctx.acting)
        return await _open_the_sayings(ctx, "phrase_agree", _OPENED_CARD, afresh=True)


class SpokenPhraseChange:
    """Open the two sayings of another phrase, in place of the one agreed (the person's own)."""

    definition = ToolDefinition(
        name="spoken_phrase_change",
        description=(
            "Change the secret phrase the person says aloud to prove who they are, once one is "
            "agreed. It works as agreeing one: a short reserved moment in which the next two "
            "things they say (the new phrase, then again) go straight to the vault, not to you; "
            "the old phrase stays valid until the new one is agreed. Refused when none is agreed "
            "(agree one first). " + _WHAT_ABOUT_THE_PHRASE + _AGREE_RULES
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "proof.phrase_change"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _nothing(arguments)
        _not_his(actor)
        async with ctx.session_factory() as session:
            judged = await _may_agree(session, ctx, change=True)
        return _described(self.action_type, _theirs(ctx), whose_judged=judged)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _nothing(arguments)
        _not_his(ctx.acting)
        return await _open_the_sayings(ctx, "phrase_change", _OPENED_CARD, afresh=False)


def _phrase_named(arguments: JsonObject) -> str | None:
    """The name of the person whose phrase is removed, or ``None``: the person of the turn."""
    if not set(arguments) <= {"person"}:
        raise InvalidArguments("invalid", 'the only argument is "person"')
    name = arguments.get("person")
    if name is not None and (
        not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_DISPLAY_NAME_LENGTH
    ):
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    return name


async def _whose_phrase(
    arguments: JsonObject, ctx: ToolContext
) -> tuple["Person", JsonObject, bool]:
    """Whose phrase a call removes, its facts, and whether the judge weighs that it is really the
    wish of whoever asks (``whose_judged``). The person's own: as before (their own words, the
    speaker them). Someone else's: only the owner's, or an adult's for a child, request, and always
    weighed (the owner, 2026-11-27); anyone else is refused."""
    from domo_core.confirmations.who_agrees import may_agree_for_a_child

    name = _phrase_named(arguments)
    judged = _their_own_words(ctx)
    _themself(ctx)
    asker = ctx.person
    whose = asker
    if name is not None:
        async with ctx.session_factory() as session:
            whose = await resolve_person(session, name, now=ctx.clock(), speaker=asker.id)
    is_self = whose.id == asker.id
    if not is_self:
        has_a_say = asker.role == "owner" or (
            whose.role == "child" and may_agree_for_a_child(asker.role, asker.id, whose.id)
        )
        if not has_a_say:
            raise InvalidArguments("not_possible", _NOT_YOURS_TO_REMOVE)
        judged = True
    facts: JsonObject = {
        "person": f"person:{whose.id}",
        "person_role": whose.role,
        "is_self": is_self,
        "invited_them": not is_self and whose.invited_by == asker.id,
    }
    return whose, facts, judged


class SpokenPhraseRemove:
    """Remove the spoken phrase: at the vault, and as a means of proof (the person's own, or
    someone else's on the owner's or an adult's for a child request, weighed by the judge)."""

    definition = ToolDefinition(
        name="spoken_phrase_remove",
        description=(
            "Remove the secret phrase the person says aloud to prove who they are: the vault "
            "forgets it and it can no longer be used as a proof. It cannot be brought back: "
            "agreeing a phrase again takes two new sayings. Say so once if they may not know. "
            "From their own words in answer to them. Asked for someone else's phrase (person: "
            "their name), by the head of the household or by an adult for a child, it is weighed "
            "before it is done and that person is left a word of yours about it; refused for "
            "anyone else. " + _WHAT_ABOUT_THE_PHRASE
        ),
        parameters={
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": (
                        "Whose phrase, by name, when it is not the phrase of the person you talk "
                        "with."
                    ),
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
            },
            "additionalProperties": False,
        },
    )
    action_type = "proof.phrase_remove"
    # The argument that names someone of the household (``tools.context.NamesPeople``).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _not_his(actor)
        whose, facts, judged = await _whose_phrase(arguments, ctx)
        return DescribedAction(
            connector="core",
            action_type=self.action_type,
            target=f"person:{whose.id}",
            effect="self_profile",
            destination="household",
            facts=facts,
            whose_judged=judged,
            # Someone else's: only the judge's verdict lets it go; silent, nothing is removed.
            verdict_needed=facts["is_self"] is False,
        )

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _not_his(ctx.acting)
        whose, _, _ = await _whose_phrase(arguments, ctx)
        described = ctx.described_action
        if described is not None and described.target != f"person:{whose.id}":
            # A request that waited runs on what it was described for, or not at all.
            raise InvalidArguments("not_found", _NOT_THE_SAME)
        now = ctx.clock()
        # The vault first: should it not answer, the means stays and the person may try again.
        try:
            had_a_phrase = await ctx.gatekeeper.phrase_remove(phrase_ref(whose.id))
        except GatekeeperError:
            return ToolOutcome(status="failed", model_result={"reason": "vault"}, card=_FAILED_CARD)
        theirs = whose.id != ctx.person.id
        word_for: uuid.UUID | None = None
        async with ctx.session_factory() as session:
            held = {mean.kind for mean in await means_of(session, whose, now)}
            await remove(session, whose.id, _PHRASE, now)
            if theirs and (had_a_phrase or _PHRASE in held):
                # Never silently: the person whose phrase it was is told, in his words.
                told = await _leave_them_a_word(session, whose.id, now, PHRASE_REMOVED_WORD)
                word_for = whose.id if told else None
            await session.commit()
        if word_for is not None and ctx.notify is not None:
            ctx.notify(word_for)
        result: JsonObject = {"had_a_phrase": had_a_phrase, "had_a_means": _PHRASE in held}
        if theirs:
            result["word_left"] = word_for is not None
        card = "action.card.phrase_removed"
        if theirs:
            # "They will be told" only when a word was left to them.
            card = (
                "action.card.phrase_removed_theirs"
                if word_for is not None
                else "action.card.phrase_removed_theirs_untold"
            )
        return ToolOutcome(status="done", model_result=result, card=_card(card))


class VoiceStopListening:
    """Turn the microphone off after this reply (the person's own words, never shown)."""

    definition = ToolDefinition(
        name="voice_stop_listening",
        description=(
            "Turn the microphone off for the person you are talking with, once this reply is "
            "said. Use it when they say, in any words, to stop listening or "
            "that they are done speaking aloud. Answer in plain words that you stopped; they turn "
            "it on again themselves. Nothing is kept."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    action_type = "voice.stop_listening"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _nothing(arguments)
        judged = _their_own_words(ctx)
        return _described(self.action_type, _theirs(ctx), whose_judged=judged)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        _nothing(arguments)
        _their_own_words(ctx)
        # At once: the page saying this reply, else every listening page of the person. Nothing is
        # kept to act on later: a page turned on again afterwards is the person's own gesture.
        from domo_core.channels.voice.live import LIVE_VOICES

        told = await LIVE_VOICES.stop_listening(ctx.person.id, ctx.domo_message_id)
        return ToolOutcome(
            status="done",
            model_result={"pages_turned_off": told},
            card=_card("action.card.done"),
        )


_UNLEARN_ARGUMENTS: Final = frozenset({"since_minutes", "person", "voice"})


def _unlearn_from(arguments: JsonObject) -> tuple[int | None, str | None, str | None]:
    """Since how many minutes (``None``: all of it), and whose: a person's name, an unknown voice's
    reference, or neither (the person of the turn)."""
    if "since_minutes" not in arguments or not set(arguments) <= _UNLEARN_ARGUMENTS:
        raise InvalidArguments(
            "invalid", 'the arguments are "since_minutes", and "person" or "voice"'
        )
    minutes = _since_from(arguments["since_minutes"])
    name, voice = arguments.get("person"), arguments.get("voice")
    if name is not None and (
        not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_DISPLAY_NAME_LENGTH
    ):
        raise InvalidArguments("invalid", "person is the name of someone of the household")
    if voice is not None and (not isinstance(voice, str) or not _REFERENCE.fullmatch(voice)):
        raise InvalidArguments("invalid", "voice is the reference of a voice nobody knows")
    if name is not None and voice is not None:
        raise InvalidArguments("invalid", 'give "person" or "voice", not both')
    if voice is not None and minutes is not None:
        raise InvalidArguments(
            "invalid", "a voice nobody knows is erased whole: since_minutes null"
        )
    return minutes, name, voice


async def _whose_voice(arguments: JsonObject, ctx: ToolContext, *, his: bool) -> tuple[str, bool]:
    """The target of a call (``person:<id>``, ``voice:<reference>``) once its guards hold, and
    whether the judge weighs that it is really the wish of whoever asks (``whose_judged``): a
    person's own wish is for their own voice alone (their own words, themself, sure for the
    whole of it); an idea of the assistant's names whose voice, a person of the household now or
    a voice nobody knows."""
    _, name, voice = _unlearn_from(arguments)
    if not his:
        judged = _their_own_words(ctx)
        if voice is not None:
            raise InvalidArguments("not_possible", _NOT_THEIRS)
        _themself(ctx)
        if name is not None:
            async with ctx.session_factory() as session:
                named = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
            if named.id != ctx.person.id:
                # Someone else's voice learnt badly (a parent for their child, the owner for
                # anyone): the judge weighs it from the context, never a refusal here (final fix
                # wave, case 4); the person concerned is left a word.
                return f"person:{named.id}", True
        return _theirs(ctx), judged
    if voice is not None:
        from domo_core.channels.voice.prints import whose_reference

        async with ctx.session_factory() as session:
            kept, person_id = await whose_reference(session, voice)
        if not kept:
            raise InvalidArguments("not_found", _NO_SUCH_VOICE)
        if person_id is not None:
            raise InvalidArguments("not_possible", _A_KNOWN_VOICE)
        return f"voice:{voice}", False
    if name is None:
        raise InvalidArguments("invalid", _NAME_WHOSE)
    judged = False
    if not _replayed(ctx) and not await _nobody_spoke(ctx):
        # Someone spoke, or words came in: the judge weighs his idea from the context (case 4),
        # words of an unknown source carried in a note, or a program's, a site's or a tool's text
        # alone, told to him by name (the owner's rule, 2026-11-26: it all depends on the source).
        judged = True
    async with ctx.session_factory() as session:
        named = await resolve_person(session, name, now=ctx.clock(), speaker=ctx.person.id)
    return f"person:{named.id}", judged


def _a_programs_text(ctx: ToolContext) -> bool:
    """Whether a program's, a tool's or a site's text is in this turn (``tool_woke``,
    ``outside``): weighed by the judge, told which, never refused for its source."""
    return ctx.tool_woke or ctx.outside.raised


def _replayed(ctx: ToolContext) -> bool:
    """Whether this call was checked when it was first asked and is described again or run after
    a yes (``ToolContext.checked_first``): the turn rule held then, and the target it was
    described for is checked instead. A step of a chain checked in its turn never is."""
    return ctx.checked_first


async def _nobody_spoke(ctx: ToolContext) -> bool:
    """Whether nobody spoke in this turn and no words from outside are in it: a turn he acts in on
    his own or began himself (``acts_alone``), no sentence heard, nothing told from outside, no
    words that came while it ran (``ToolContext.heard_meanwhile``), and no plan due in it that a
    person set (only his own, ``Plan.made_by_domo``, are nobody's)."""
    if not nobody_spoke_in(ctx):
        return False
    if not ctx.due_plans:
        return True
    from domo_core.plans.models import Plan

    ids = {uuid.UUID(plan.id) for plan in ctx.due_plans}
    async with ctx.session_factory() as session:
        his = await session.scalars(
            select(Plan.id).where(Plan.id.in_(ids), Plan.made_by_domo.is_(True))
        )
        return set(his) == ids


async def _leave_them_a_word(
    session: AsyncSession, person_id: uuid.UUID, now: datetime, key: str = VOICE_ERASED_WORD
) -> bool:
    """A word of his own for ``person_id``, whose learnt voice was erased (``VOICE_ERASED_WORD``)
    or whose phrase was removed (``PHRASE_REMOVED_WORD``) by someone else, in their language
    (the text ``key`` of his), and news for them; ``False`` when they are gone. The caller
    commits, then tells the core (``ToolContext.notify``)."""
    from domo_core.accounts.models import Person
    from domo_core.conversation.visits import news_for
    from domo_core.notes.models import Note
    from domo_core.policy.roles import actor_label, domo_actor
    from domo_core.texts.view import person_texts

    person = await session.get(Person, person_id)
    if person is None:
        return False
    texts = await person_texts(session, person)
    session.add(
        Note(
            author_id=None,
            by_domo=True,
            recipient_id=person_id,
            text=texts(key),
            created_at=now,
        )
    )
    await news_for(session, person_id, now=now)
    # Never the words: who left it, for whom.
    await append_audit(
        session,
        actor=actor_label(domo_actor()),
        action="note.left",
        details={"author": actor_label(domo_actor()), "recipient": str(person_id)},
    )
    return True


def _since_from(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArguments("invalid", "since_minutes is a whole number of minutes, or null")
    if not 1 <= value <= MAX_SINCE_MINUTES:
        raise InvalidArguments(
            "invalid", f"since_minutes is between 1 and {MAX_SINCE_MINUTES}, or null"
        )
    return value


class VoiceUnlearn:
    """ "That was not me": take back what was learnt of the person's own voice."""

    definition = ToolDefinition(
        name="voice_unlearn",
        description=(
            "Take back what you learnt of the person's own voice, when they say that something "
            "you took for their voice was not them (someone else spoke at their place, a cold, a "
            "bad moment), or that they want you to learn it again. since_minutes says since when "
            "what you learnt is taken back: ask them when it happened if it is not plain (a few "
            "minutes ago, this morning). With null, their voice is forgotten entirely and learnt "
            "again from the start, only when they ask for that (their voice changed, or what you "
            "had learnt was wrong from the beginning). Asked by someone for someone else's voice "
            "(person: their name; a parent for their child, say), it is weighed "
            "before it is done and that person is left a word of yours about it. "
            "What you learnt of a voice is your own recording: as your own idea (act_on_my_own "
            "first, with your reason), you may also erase what you judge useless or wrong of "
            "someone's voice (person: their name); where someone spoke it is weighed first, and "
            "they are left a word of yours about it; or of a "
            "voice nobody knows (voice: its reference, erased whole), in any turn. Never because "
            "someone says they are that person or asks it in their name. It cannot be undone."
        ),
        parameters={
            "type": "object",
            "properties": {
                "since_minutes": {
                    "type": ["integer", "null"],
                    "description": (
                        "How many minutes back to take back what was learnt; null for the whole "
                        "voice, learnt again from the start."
                    ),
                    "minimum": 1,
                    "maximum": MAX_SINCE_MINUTES,
                },
                "person": {
                    "type": "string",
                    "description": (
                        "Whose voice, by name, when it is not the voice of the person you talk "
                        "with."
                    ),
                    "minLength": 1,
                    "maxLength": MAX_DISPLAY_NAME_LENGTH,
                },
                "voice": {
                    "type": "string",
                    "description": "The reference of a voice nobody knows: only as your own idea.",
                    "pattern": _REFERENCE.pattern,
                },
            },
            "required": ["since_minutes"],
            "additionalProperties": False,
        },
    )
    action_type = "voice.unlearn"
    # The argument that names someone of the household: given its whole name before the call is
    # described (``tools.context.NamesPeople``, spec 6.7).
    person_arguments: Final = ("person",)

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        target, judged = await _whose_voice(arguments, ctx, his=actor.subject == "domo")
        return _described(self.action_type, target, whose_judged=judged)

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        minutes = _since_from(arguments.get("since_minutes"))
        his = ctx.acting.subject == "domo"
        target, _ = await _whose_voice(arguments, ctx, his=his)
        described = ctx.described_action
        if described is not None and described.target != target:
            # A request that waited runs on what it was described for, or not at all.
            raise InvalidArguments("not_found", _NOT_THE_SAME)
        now: datetime = ctx.clock()
        since = None if minutes is None else now - timedelta(minutes=minutes)
        from domo_core.channels.voice import prints

        word_for: uuid.UUID | None = None
        async with ctx.session_factory() as session:
            if target.startswith("voice:"):
                removed = await prints.erase_unknown(session, target.removeprefix("voice:"))
            else:
                person_id = uuid.UUID(target.removeprefix("person:"))
                removed = await prints.unlearn(
                    session, person_id=person_id, since=since, microphone=None
                )
                if (his or person_id != ctx.person.id) and removed:
                    # What his erasure would buy (a voice "not learnt yet" reads sure at the
                    # page) is taken away: to be learnt again (ruling R).
                    await prints.mark_to_relearn(session, person_id, now)
                    word_for = (
                        person_id if await _leave_them_a_word(session, person_id, now) else None
                    )
            await session.commit()
        if word_for is not None and ctx.notify is not None:
            ctx.notify(word_for)
        result: JsonObject = {"removed": removed, "since_minutes": minutes}
        if his or target != _theirs(ctx):
            result["word_left"] = word_for is not None
        return ToolOutcome(
            status="done", model_result=result, card=_card("action.card.voice_unlearnt")
        )


_NOBODY_SPOKE: Final = "nobody spoke in this turn: there is no way of speaking to read"
_NOT_AFTER_OUTSIDE_TEXT: Final = (
    "words nobody of the house said are in this turn: a reading of who speaks is noted only in a "
    "turn without them"
)


def _manner_from(arguments: JsonObject) -> Literal["for", "against"]:
    if set(arguments) != {"weighs", "why"}:
        raise InvalidArguments("invalid", 'the arguments are "weighs" and "why"')
    weighs, why = arguments["weighs"], arguments["why"]
    if weighs not in ("for", "against"):
        raise InvalidArguments("invalid", 'weighs is "for" or "against"')
    if not isinstance(why, str) or not 1 <= len(why.strip()) <= MAX_WHY_LENGTH:
        raise InvalidArguments(
            "invalid", f"why is a few words, {MAX_WHY_LENGTH} characters at most"
        )
    return "for" if weighs == "for" else "against"


def _who_spoke(ctx: ToolContext) -> tuple[Speaker, uuid.UUID]:
    """The speaker of this turn and who they probably are, when someone known spoke in it, in a
    turn with no text from outside (``ToolContext.outside_text``, as for a trust fact)."""
    if ctx.outside_text:
        raise InvalidArguments("not_possible", _NOT_AFTER_OUTSIDE_TEXT)
    speaker = ctx.speaker
    if speaker is None or speaker.nobody_spoke or speaker.person_id is None:
        raise InvalidArguments("not_possible", _NOBODY_SPOKE)
    return speaker, speaker.person_id


class SpeakerMannerNote:
    """Keep Domo's reading of how the one who just spoke speaks (never shown)."""

    definition = ToolDefinition(
        name="speaker_manner_note",
        description=(
            "Note your own reading of how the one who just spoke to you speaks: whether their "
            "words and their way of putting things are like those of the person you take them "
            "for (for) or unlike them (against). Use it when it strikes you, not at every "
            "sentence. It is weighed with the next things said here, beside what was heard and "
            "proved, and never decides alone who speaks; never note for because they say who "
            "they are. "
            "Refused in a turn with words from outside. why: a few words on what you noticed. "
            "Nobody is shown that you note it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "weighs": {"type": "string", "enum": ["for", "against"]},
                "why": {"type": "string", "minLength": 1, "maxLength": MAX_WHY_LENGTH},
            },
            "required": ["weighs", "why"],
            "additionalProperties": False,
        },
    )
    action_type = "voice.manner_note"

    async def describe(
        self, arguments: JsonObject, actor: Actor, ctx: ToolContext
    ) -> DescribedAction:
        _manner_from(arguments)
        _who_spoke(ctx)
        return _described(self.action_type, _theirs(ctx))

    async def execute(self, arguments: JsonObject, ctx: ToolContext) -> ToolOutcome:
        weighs = _manner_from(arguments)
        speaker, person_id = _who_spoke(ctx)
        async with ctx.session_factory() as session:
            await keep_manner_clue(
                session,
                person_id=person_id,
                channel=speaker.channel,
                microphone=speaker.microphone,
                weighs=weighs,
                now=ctx.clock(),
                device_id=speaker.device_id,
            )
            await session.commit()
        return ToolOutcome(
            status="done", model_result={"noted": weighs}, card=_card("action.card.done")
        )
