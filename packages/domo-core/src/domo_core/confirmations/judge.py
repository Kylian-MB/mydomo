"""The yes-judge (spec 4): whether an action the rights allow is done at once, or what is asked
first.

Like the classifier of the replies (``confirmations.intent``), it is an isolated call to the
household's model, with a prompt of its own (``prompts/yes_judge.md``): no tool, no history of the
conversation, a short answer read strictly. What it reads is a case (``JudgeCase``) that the code
builds from the database (``build_case``), never copied from what the model that talks read: who
asks, the action as the barrier sees it and in words, what the rights say, the person's last
messages of this conversation (only theirs: provenance ``person``), what Domo had just said to them
before the last one (``asked_them``: what a "yes" of theirs answers), what they did of this kind
before (``policy.facts.person_history``), the moment, who is in the household, and their requests
that wait (a waiting note without its words, D4). Gathered in one go by
``confirmations.case.gather`` (spec 6.1), it also holds what bears on the action, each line marked
with its author and never mixed: what Domo remembers of the person and of the subject (their own
words, the owner's, someone else's with their name, his own conclusions and lessons), their trust
facts, what went wrong before on this kind of action and this thing, the state of the thing (its
last change, by whom, why when known), who speaks and how old the clues really are, and whose idea
the action is (the person asked, or an idea of Domo's with its reason). A private memory of someone
else never enters it; for an idea of his, only what belongs to the person concerned. ``asked_them``
is that one reply and no other, as the person read it. When it is not Domo's own answer to a message
of theirs written in a turn given nothing from outside (``Message.own_answer`` false: it told them a
note someone left or a request of someone else, or Domo wrote it on his own, at a wake or an
arrival), the case says so (``relayed``): the owner's decision of 2026-11-17 is that a "yes" to what
Domo just told them is understood all the same, but only for an action that reply itself plainly
names. So the judge never sees Domo's other replies, the results of the tools, an outside content, a
secret, the private words of someone else or the words of a note themselves (those reach their Domo
alone, as data, spec 4.8): only what Domo last said to the person, as they read it. Everything in it
is data, never instructions.

It answers one of six verdicts (``VERDICTS``) with a short reason that may be said to the person, on
one line, and may add a second, ``log: <reason>``, the reason kept for the journal, which may tell
what someone else said or did (``read_judgement``, ``Judgement.noted``): ``do``, ``yes``, ``warn``,
``explicit_yes``, ``proof``, ``do_not``. ``warn`` is a ``yes`` everywhere a verdict decides
(``Judgement.as_ask``); it says that the reason is something the person must know first (today: that
it cannot be undone), not a doubt about what they want, and the request keeps that
(``Asked.warned``): their answer to it, yes or no, is remembered as what Domo told them
(``memory.told``), and the judge reads it the next time (``remembered``). The audit keeps ``warn``.
When it does not answer (no model, an error, too long, an answer it cannot read), the verdict is
``do`` (``fallback``): what a person asked for and Domo understood is done, and nothing in the code
asks them again (the owner's decision of 2026-11-15); the rights, someone else's agreement and the
owner's own rules do not come from the judge and hold all the same. That is for a request the person
made: when the turn was given words from outside (a note someone left them), nobody is known to have
asked, and a judge that does not answer, asked twice, means a ``yes`` (``they_asked`` false:
``conversation.execution.consult_the_judge``, audit CNV-2). In a turn Domo started himself with a
reason (a wake, an arrival, a plan he set alone, a tool of his; never plans that all keep the
person's words) every judged call is an idea of his: a silence there is never a verdict acted on
(``execution.judge_silent``). An answer that is no verdict is the model's mistake: it is a lesson
about the judge's model (``LearnsFromFailures``, ``llm.lessons.UNREADABLE_VERDICT``), which Domo
sees in his context; it never changes which models may judge.

What is asked in the end is the most demanding of the judge's verdict, the owner's rule ("always
ask me before...") and what Domo asks more (``what_to_ask``): no type asks anything by default.
A request changed in words, or a chain taken up again, is
judged again with its history (``RequestTold``, which ``confirmations.chain.request_told``
builds), and what it asks then is ``ask_after_a_change``: words never give an explicit yes nor a
proof, and a yes already given covers what it was given to. ``do_not`` is not on that scale: the
action is refused. The verdict is audited with its reason (``audit_judgement``).
"""

import json
import uuid
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Literal, Protocol

import anyio
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.audit.log import append_audit

# The case and how it is built live in ``confirmations.case``; their names stay importable here.
from domo_core.confirmations.case import (
    JUDGE_MESSAGE_CHARS,
    JUDGE_MESSAGES,
    NOT_FOR_THE_JUDGE,
    JudgeCase,
    RequestTold,
    RightsSay,
    StepTold,
    YesTold,
    build_case,
    owner_rule_of,
    planned_step_told,
    step_told,
)
from domo_core.confirmations.steps import PlannedStep
from domo_core.llm.lessons import UNREADABLE_VERDICT
from domo_core.llm.tasks import TaskKind
from domo_core.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatModel,
    ModelCallError,
    ToolCall,
)
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Ask, Asked, NeverAsk, most_demanding, most_demanding_asked
from domo_core.policy.roles import Actor
from domo_core.text import is_plain_line

__all__ = [
    "JUDGE_MESSAGE_CHARS",
    "JUDGE_MESSAGES",
    "JUDGE_OUTPUT_TOKENS",
    "JUDGE_SECONDS",
    "MAX_JUDGE_REASON_LENGTH",
    "NOT_FOR_THE_JUDGE",
    "VERDICTS",
    "Fallback",
    "JudgeCase",
    "Judgement",
    "LearnsFromFailures",
    "RequestTold",
    "RightsSay",
    "StepTold",
    "Verdict",
    "YesTold",
    "ask_after_a_change",
    "audit_judgement",
    "build_case",
    "fallback",
    "given_by_the_change",
    "judge",
    "judge_prompt",
    "owner_rule_of",
    "planned_step_told",
    "read_judgement",
    "step_told",
    "what_to_ask",
]

Verdict = Literal["do", "yes", "warn", "explicit_yes", "proof", "do_not"]
# From "do it" to "do not": ``do``, ``yes``, ``explicit_yes`` and ``proof`` are a scale, ``warn``
# is a ``yes`` to the code (it says that the reason is something the person must know first), and
# ``do_not`` is not on it.
VERDICTS: Final[tuple[Verdict, ...]] = ("do", "yes", "warn", "explicit_yes", "proof", "do_not")
# Why the judge did not answer: no model to ask, the call failed, it took too long, or what it
# said is no verdict.
Fallback = Literal["no_model", "error", "timeout", "unreadable"]

# How long the judge may take before its verdict is the fallback's.
JUDGE_SECONDS: Final = 20.0
# The judge answers on one or two short lines, asked without thinking first
# (``ChatModel.stream``): the line itself took 20 to 45 tokens with a real model, in French
# (2026-11-15). The room is for a model that thinks anyway (one that cannot be told not to, or
# of a provider whose adapter does not ask): those runs took 150 to 330 tokens, thinking
# included, and 80 cut most of them before the verdict. A ceiling, not a cost: only what is
# written is paid.
JUDGE_OUTPUT_TOKENS: Final = 500
# The longest reason kept, the one said (the column ``ask_why`` of a request) and the one noted.
MAX_JUDGE_REASON_LENGTH: Final = 200


@dataclass(frozen=True)
class Judgement:
    """What the judge said: its ``verdict`` and its ``reason`` (one plain line, 200 characters at
    most; ``""`` when none was given), the sentence that may be said to the person: a request's
    ``ask_why``, a card and a tool's result read it, and only it. ``noted`` is the reason kept for
    the journal (``audit_judgement``), which may tell what someone else said or did; it is the
    ``reason`` itself when the judge gave none apart. ``fallback`` is set when the judge did not
    answer: the verdict is then ``do``, or ``yes`` when the person is not known to have asked
    (``fallback``). ``decided`` says that the case it judged left someone's agreement to it
    (``RightsSay.judge_decides``, as the case held it): only then may its ``do`` spare that
    agreement (``conversation.execution``)."""

    verdict: Verdict
    reason: str = ""
    fallback: Fallback | None = None
    noted: str = ""
    decided: bool = False

    def __post_init__(self) -> None:
        if not self.noted:
            object.__setattr__(self, "noted", self.reason)

    @property
    def source(self) -> Literal["model", "fallback"]:
        """Where the verdict comes from: the judge's model, or the fallback."""
        return "model" if self.fallback is None else "fallback"

    def as_ask(self) -> Ask | None:
        """What the verdict asks of the person: nothing for ``do``, the ask itself for ``yes``,
        ``explicit_yes`` and ``proof``, a ``yes`` for ``warn``, and nothing for ``do_not`` (it is
        refused, not asked)."""
        match self.verdict:
            case "yes" | "explicit_yes" | "proof":
                return self.verdict
            case "warn":
                return "yes"
            case "do" | "do_not":
                return None

    @property
    def warns(self) -> bool:
        """Whether the judge asks the yes only to tell the person something they must know first
        (``warn``), and said it (a reason): the request is ``warned``, and their answer to it is
        remembered (``memory.told``)."""
        return self.verdict == "warn" and bool(self.reason)


def fallback(why: Fallback, *, they_asked: bool = True) -> Judgement:
    """The verdict when the judge did not answer, with no reason (never a ``do_not``): ``do``
    when the person asked (``they_asked``: the turn answers a message of theirs and was given
    nothing from outside): the request was asked for and understood, and a judge that is silent
    asks nothing. ``yes`` otherwise: nobody is known to have asked for what Domo is about to do
    (the turn answers the person but was told words that are not theirs), so they say yes first.
    In a turn he started himself with a reason the fallback decides nothing: each judged call
    there is an idea of his, which a silent judge leaves undone
    (``conversation.execution.judge_silent``)."""
    return Judgement("do" if they_asked else "yes", "", why)


@cache
def judge_prompt() -> str:
    """The system prompt of the judge (``prompts/yes_judge.md``), read once."""
    path = resources.files("domo_core.prompts").joinpath("yes_judge.md")
    return path.read_text(encoding="utf-8").strip()


def _reason_of(text: str) -> str:
    """The reason of a verdict: the first line of ``text``, without the spaces around it, cut at
    ``MAX_JUDGE_REASON_LENGTH``; ``""`` unless it is one plain line (no control character, no
    direction mark: it is shown on a card)."""
    lines = text.strip().splitlines()
    reason = lines[0].strip()[:MAX_JUDGE_REASON_LENGTH].rstrip() if lines else ""
    return reason if is_plain_line(reason) else ""


# The word that begins the judge's second line: the reason kept for the journal.
_NOTED: Final = "log"


def _verdict_line(line: str) -> tuple[Verdict, str] | None:
    """``line`` read as ``<verdict>`` or ``<verdict>: <reason>`` (see ``read_judgement``)."""
    said, _, reason = line.partition(":")
    word = said.strip().casefold()
    for verdict in VERDICTS:
        if word == verdict:
            return verdict, _reason_of(reason)
    return None


def _is_noted(line: str | None) -> bool:
    """Whether ``line`` is a ``log:`` line, known by its word alone (its reason may be empty or
    not plain)."""
    if line is None:
        return False
    word, colon, _ = line.partition(":")
    return bool(colon) and word.strip().casefold() == _NOTED


def _noted_of(line: str | None) -> str:
    """The reason of a ``log: <reason>`` line (``_reason_of``); ``""`` for any other line."""
    return _reason_of(line.partition(":")[2]) if line is not None and _is_noted(line) else ""


def read_judgement(text: str, *, they_asked: bool = True) -> Judgement:
    """The judge's answer, read strictly: ``<verdict>`` or ``<verdict>: <reason>``, the verdict
    one of ``VERDICTS`` (case folded, without the spaces around it), the reason its first line
    (``_reason_of``), what may be said to the person. The line right after it may be
    ``log: <reason>``, the reason kept for the journal (``Judgement.noted``, read the same way);
    without it, the journal keeps the reason said. When the answer does not begin with a
    verdict, its last line (or the one before a last ``log:`` line) is read the same way: a model
    that is asked not to think first sometimes reasons aloud before its line (seen on a real one,
    2026-11-15). When both its first and that last line are verdicts that differ ("do: ..." then
    "warn: ..."), the more demanding is read (``_MOST_DEMANDING_LAST``), never the first by its
    place. Anything else is ``fallback("unreadable")``: a ``do`` when the person asked
    (``they_asked``), a ``yes`` otherwise."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return fallback("unreadable", they_asked=they_asked)
    candidates = [0]
    if len(lines) > 1:
        last = len(lines) - 1
        candidates.append(last - 1 if _is_noted(lines[last]) and last > 1 else last)
    read: list[Judgement] = []
    for at in candidates:
        found = _verdict_line(lines[at])
        if found is not None:
            verdict, reason = found
            after = lines[at + 1] if at + 1 < len(lines) else None
            read.append(Judgement(verdict, reason, noted=_noted_of(after)))
    if not read:
        return fallback("unreadable", they_asked=they_asked)
    return max(read, key=lambda judgement: _MOST_DEMANDING_LAST.index(judgement.verdict))


# The verdicts from the least demanding to the most, when an answer holds two that differ: a
# refusal is the most cautious of all.
_MOST_DEMANDING_LAST: Final[tuple[Verdict, ...]] = (
    "do",
    "yes",
    "warn",
    "explicit_yes",
    "proof",
    "do_not",
)


class LearnsFromFailures(Protocol):
    """What keeps the lessons of a turn's calls (``llm.picker.ModelPicker.note_failure``)."""

    async def note_failure(self, task: TaskKind, reason: str) -> None:
        """The last call of ``task`` did it badly, for ``reason``. It never raises."""
        ...


async def judge(
    model: ChatModel | None,
    case: JudgeCase,
    *,
    learns: LearnsFromFailures | None = None,
    they_asked: bool = True,
) -> Judgement:
    """The verdict of the judge on ``case``.

    The call holds one user message, the case as JSON, under the judge's prompt, with no tool,
    ``max_output_tokens=JUDGE_OUTPUT_TOKENS`` and ``think=False`` (one line, asked without
    thinking first), and it has ``JUDGE_SECONDS`` to answer. Without a
    model, the verdict is the fallback's (``no_model``); so it is when the call fails
    (``error``), takes too long (``timeout``), calls a tool or says no verdict (``unreadable``):
    ``do`` when the person asked (``they_asked``: the turn answers a message of theirs and was
    given nothing from outside), ``yes`` otherwise (``fallback``). An unreadable answer
    is told to ``learns``, a lesson about the judge's model (``UNREADABLE_VERDICT``)."""
    judgement = await _verdict_of(model, case, they_asked)
    if judgement.fallback == "unreadable" and learns is not None:
        await learns.note_failure("judge", UNREADABLE_VERDICT)
    return judgement


async def _verdict_of(model: ChatModel | None, case: JudgeCase, they_asked: bool) -> Judgement:
    """The judge's call and its verdict (see ``judge``)."""
    if model is None:
        return fallback("no_model", they_asked=they_asked)
    pieces: list[str] = []
    with anyio.move_on_after(JUDGE_SECONDS):
        try:
            stream = model.stream(
                system=judge_prompt(),
                messages=[ChatMessage("user", json.dumps(case.as_json(), ensure_ascii=False))],
                max_output_tokens=JUDGE_OUTPUT_TOKENS,
                # One line: nothing to think over that the line does not say.
                think=False,
            )
            async with aclosing(stream) as items:
                async for item in items:
                    if isinstance(item, ToolCall):
                        return fallback("unreadable", they_asked=they_asked)
                    if isinstance(item, ChatChunk):
                        pieces.append(item.text)
        except ModelCallError:
            return fallback("error", they_asked=they_asked)
        return read_judgement("".join(pieces), they_asked=they_asked)
    return fallback("timeout", they_asked=they_asked)


def what_to_ask(
    judgement: Judgement | None,
    *,
    rule: Ask | None,
    raised: tuple[Ask, str] | None,
    never_ask: NeverAsk | None = None,
) -> Asked | None:
    """What the person is asked before the action (spec 4.4): the most demanding of the judge's
    verdict, the owner's ``rule`` and what Domo ``raised`` (the ask and his reason); ``None``
    when nothing is asked. Who asked it is the first of ``rule``, ``judge`` and ``domo`` that
    asks that much, with the judge's reason or Domo's (``None`` for a rule, and for an empty
    reason). No ``judgement`` (an action no judge weighs, with an owner's rule on it) asks what
    the rule and Domo's raise ask.

    With the owner's "not at all" (``never_ask.at_all``, spec 6.3), the judge's verdict and
    Domo's raise count only when they ask for a proof: a yes or an explicit yes of theirs is
    dropped. A rule counts as it is. A ``do_not`` is not asked: ``ValueError``."""
    if judgement is not None and judgement.verdict == "do_not":
        raise ValueError("a do_not is refused, not asked")
    judged = None if judgement is None else judgement.as_ask()
    domo = None if raised is None else raised[0]
    if never_ask is not None and never_ask.at_all:
        judged = judged if judged == "proof" else None
        domo = domo if domo == "proof" else None
    ask = most_demanding(rule, judged, domo)
    if ask is None:
        return None
    if ask == rule:
        return Asked(ask, "rule", None)
    if judgement is not None and ask == judged:
        return Asked(ask, "judge", judgement.reason or None, judgement.warns)
    return Asked(ask, "domo", (raised[1] if raised is not None else "") or None)


# What words never give: a change in words is a plain yes (F2).
_WORDS_NEVER_GIVE: Final[frozenset[Ask]] = frozenset({"explicit_yes", "proof"})


def ask_after_a_change(
    judgement: Judgement,
    *,
    old_ask: Asked | None,
    steps: Sequence[PlannedStep],
    new: Sequence[bool],
    raised: tuple[Ask, str] | None,
    never_ask: NeverAsk | None,
    yes_already_given: bool,
    yes_gave: Ask | None = None,
    chain_rule: Ask | None = None,
) -> Asked | None:
    """What a request changed in words, or a chain taken up again, asks now (spec 4.7, F2), once
    the judge judged it (``judgement``, never a ``do_not``: ``ValueError``): the most demanding of
    the judge's verdict, what Domo ``raised``, what the request asked before (``old_ask``) and
    the owner's rules on its steps (``PlannedStep.rule``); ``None`` when nothing is asked.

    ``yes_already_given`` says whether a yes was given to it:

    * not for a change (``confirmation_amend``): the request still waits, nobody said yes yet. The
      rules count over every step still to run, and so does the owner's rule on chains
      (``chain_rule``); what the request asked stays the least it asks when it was an explicit
      yes or a proof (words never give those);
    * yes for a chain taken up again (``chain_continue``): that yes covers what it was given to, so
      the rules count in full for the steps changed or added (``new``, one for each of
      ``steps``), and for a step kept only when they ask more than that yes gave (``yes_gave``,
      ``service.what_the_yes_gave``: ``None``, nothing, when the judge let the chain go): a rule
      the owner added since asks what the yes never gave. What was asked before was given.

    With the owner's "not at all" (``never_ask.at_all``), the judge's verdict and Domo's raise
    count only when they ask for a proof, as in ``what_to_ask``. Who asked it is, on a tie, the
    first of the judge, Domo, the request before and the owner's rule: what the judge or Domo
    asks of a change is what makes it asked again (``given_by_the_change``)."""
    if judgement.verdict == "do_not":
        raise ValueError("a do_not is refused, not asked")
    if len(new) != len(steps):
        raise ValueError("a change says, for each of its steps, whether it is new")

    def counted(ask: Ask | None, is_new: bool) -> Ask | None:
        """``ask`` as it counts for a step: in full when nobody said yes yet or the step is
        new; for a step kept under a yes given, only when it asks more than that yes gave."""
        if is_new or not yes_already_given or ask is None:
            return ask
        return ask if yes_gave is None or most_demanding(ask, yes_gave) != yes_gave else None

    pairs = list(zip(steps, new, strict=True))
    rules: list[Ask | None] = [counted(step.rule, is_new) for step, is_new in pairs]
    if not yes_already_given:
        rules.append(chain_rule)
    rule = most_demanding(*rules)
    judged = judgement.as_ask()
    domo = None if raised is None else raised[0]
    if never_ask is not None and never_ask.at_all:
        judged = judged if judged == "proof" else None
        domo = domo if domo == "proof" else None
    before = (
        old_ask
        if old_ask is not None and old_ask.ask in _WORDS_NEVER_GIVE and not yes_already_given
        else None
    )
    return most_demanding_asked(
        None
        if judged is None
        else Asked(judged, "judge", judgement.reason or None, judgement.warns),
        None if domo is None or raised is None else Asked(domo, "domo", raised[1] or None),
        before,
        None if rule is None else Asked(rule, "rule", None),
    )


def given_by_the_change(asked: Asked | None) -> bool:
    """Whether the words of a change (a yes in words) give what ``asked`` asks
    (``ask_after_a_change``): nothing, or the plain yes of a rule of the owner. A yes the judge
    or Domo asks again of the change, an explicit yes and a proof are not given by it: the
    change is then a new request."""
    return asked is None or (asked.ask == "yes" and asked.by == "rule")


async def audit_judgement(
    session: AsyncSession,
    *,
    actor: Actor,
    tool_name: str,
    action: DescribedAction,
    judgement: Judgement,
    action_id: uuid.UUID | None = None,
) -> None:
    """Write the audit entry ``judge.verdict`` (the tool, the action type and target, the verdict,
    the reason kept for the journal, ``Judgement.noted``, and whether it came from the model or
    the fallback) in the caller's transaction: like ``append_audit``, nothing is committed, and
    the caller writes it last. ``action_id`` is the action stored with it, when there is one
    (``conversation.execution.store_action``): what ties the verdict to that action, for the day
    it is undone (``learning.undone``)."""
    details: dict[str, JsonValue] = {
        "tool": tool_name,
        "action_type": action.action_type,
        "target": action.target,
        "verdict": judgement.verdict,
        "reason": judgement.noted,
        "source": judgement.source,
    }
    if action_id is not None:
        details["action_id"] = str(action_id)
    await append_audit(
        session, actor=f"person:{actor.person_id}", action="judge.verdict", details=details
    )
