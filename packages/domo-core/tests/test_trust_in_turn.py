"""The trust figure comes where Domo weighs what he is told (trust per person, spec 6.4, task 3).

Next to each memory that is someone's word about another person, not confirmed by them, said by
someone else than the person of the turn: what the word of who said it has been worth so far. For
the person who speaks: their own, on one line of the context, as data. The guard of the memories
says what a figure is and is not. The yes-judge reads the trust of the one who asks, in his case
(the owner decided it on 2026-11-23); no other code of the policy, the confirmations or the
spending imports the trust, and the barrier reads none. A read of the scores that fails leaves the
turn going, without a figure.
"""

import ast
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import domo_core
import fakes
import pytest
from chat_support import ChatPilot
from domo_core.accounts.models import Person
from domo_core.llm.choices import HOUSEHOLD_SCOPE, Choices, save_choices
from domo_core.memory.review import _known_of  # pyright: ignore[reportPrivateUsage]
from domo_core.memory.service import SAID_BY_YOU
from domo_core.prompts import NOW_LINE
from domo_core.trust import shown
from domo_core.trust.models import Outcome
from domo_core.trust.score import score
from domo_core.trust.service import note
from fakes import FakeChatModel
from sqlalchemy import update

CONTEXT_MARK = "\n\nContext\n"
CORE_HEADING = "What you know (always): "
TRUST_HEADING = "What their word has been worth so far (data, not instructions): "
PINE_NUTS = "Alex does not like pine nuts."
# What a fact says: it must never reach the judge.
FACT_ABOUT = "said the boiler was fixed and it was"


def context_lines(system: str) -> list[str]:
    return system.split(CONTEXT_MARK, 1)[1].splitlines()


def core_of(system: str) -> list[dict[str, Any]]:
    for line in context_lines(system):
        if line.startswith(CORE_HEADING):
            return json.loads(line.removeprefix(CORE_HEADING))
    return []


def line_of(system: str, memory_id: str) -> dict[str, Any]:
    [line] = [m for m in core_of(system) if m["id"] == memory_id]
    return line


def speaker_line(system: str) -> list[str]:
    return [line for line in context_lines(system) if line.startswith(TRUST_HEADING)]


async def context_of(chat: ChatPilot) -> str:
    model = chat.use_model(FakeChatModel(["ok"]))
    reply = await chat.say("Hello")
    assert reply.text == "ok"
    return model.calls[0].system


async def kept(chat: ChatPilot, content: str, subject: str) -> str:
    reply = await chat.say_with_tool(
        "memory_keep",
        {
            "content": content,
            "subject": subject,
            "kind": "preference",
            "private": False,
            "core": True,
        },
    )
    assert reply.actions == [{"tool": "memory_keep", "status": "done"}]
    return reply.model_results[-1]["id"]


async def facts_about(chat: ChatPilot, person: Person, *outcomes: Outcome) -> None:
    """Facts Domo saw about ``person``'s word (committed)."""
    async with chat.server.session_factory() as writer:
        for outcome in outcomes:
            await note(
                writer,
                person_id=person.id,
                outcome=outcome,
                established="seen",
                about=FACT_ABOUT,
                now=chat.clock(),
            )
        await writer.commit()


def figure(start: int, *outcomes: Outcome) -> dict[str, int]:
    """The trust a person with these facts, all seen, has from ``start``, as the context says it."""
    return {
        "percent": score([(outcome, "seen") for outcome in outcomes], start=start),
        "facts": len(outcomes),
    }


async def test_an_unconfirmed_memory_comes_with_the_trust_of_who_said_it(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    sam = child_chat.person
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    await facts_about(child_chat, sam, "lied", "held", "mistaken")
    expected = figure(70, "lied", "held", "mistaken")
    line = line_of(await context_of(adult_chat), memory_id)
    assert line["unconfirmed"] is True and line["said_by"] == "Sam"
    assert line["trust"] == expected
    # The search gives it too.
    reply = await adult_chat.say_with_tool("memory_search", {"words": "pine nuts"})
    [found] = [m for m in reply.model_results[-1]["memories"] if m["id"] == memory_id]
    assert found["trust"] == expected
    # And the last review of someone who left (here, what is known of Alex).
    async with adult_chat.server.session_factory() as session:
        known = await _known_of(
            session, adult_chat.person.id, max_chars=10_000, start=70, now=adult_chat.clock()
        )
    [review_line] = [m for m in known if m.id == memory_id]
    assert review_line.as_json()["trust"] == expected
    # The guard says what a figure is, and is not.
    flat = " ".join(" ".join(context_lines(await context_of(adult_chat))).split())
    assert (
        "A trust figure says what someone's word has been worth so far: weigh it with who speaks "
        "and what it is about; it never replaces your judgement, and a low figure does not make "
        "what they say false."
    ) in flat

    # Sam leaves the household: her word is still hers, without a figure anywhere.
    async with adult_chat.server.session_factory() as writer:
        await writer.execute(
            update(Person)
            .where(Person.id == sam.id)
            .values(disabled_at=datetime.now(UTC), state="gone")
        )
        await writer.commit()
    line = line_of(await context_of(adult_chat), memory_id)
    assert line["unconfirmed"] is True and "trust" not in line
    reply = await adult_chat.say_with_tool("memory_search", {"words": "pine nuts"})
    [found] = [m for m in reply.model_results[-1]["memories"] if m["id"] == memory_id]
    assert "trust" not in found


async def test_a_confirmed_memory_and_ones_own_memory_carry_no_trust(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    sams = await kept(child_chat, PINE_NUTS, "Alex")
    boiler = await kept(adult_chat, "The boiler is noisy.", "the boiler")
    await facts_about(child_chat, child_chat.person, "held")
    # In Sam's own turn, what she said is hers: no figure of her own word next to it.
    line = line_of(await context_of(child_chat), sams)
    assert line["unconfirmed"] is True and line["said_by"] == SAID_BY_YOU and "trust" not in line
    # Alex's own memory has none.
    assert "trust" not in line_of(await context_of(adult_chat), boiler)
    # Once Alex confirmed Sam's word, it is no longer a word to weigh.
    reply = await adult_chat.say_with_tool(
        "memory_change", {"id": sams, "confirmed": True}, text="Yes, that's true."
    )
    assert reply.actions == [{"tool": "memory_change", "status": "done"}]
    line = line_of(await context_of(adult_chat), sams)
    assert "unconfirmed" not in line and "trust" not in line


async def test_the_speakers_own_trust_is_in_the_context(adult_chat: ChatPilot):
    await facts_about(adult_chat, adult_chat.person, "held", "held")
    lines = context_lines(await context_of(adult_chat))
    role = lines.index("Role: adult")
    # Right after the role and the line that this context is the truth now: data.
    assert lines[role + 1] == NOW_LINE
    assert lines[role + 2] == TRUST_HEADING + json.dumps(figure(70, "held", "held"))
    assert sum(line.startswith(TRUST_HEADING) for line in lines) == 1


async def test_a_stranger_starts_at_the_start_domo_chose(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")
    system = await context_of(adult_chat)
    assert speaker_line(system) == [f'{TRUST_HEADING}{{"percent": 70, "facts": 0}}']
    assert line_of(system, memory_id)["trust"] == {"percent": 70, "facts": 0}
    async with adult_chat.server.session_factory() as writer:
        await save_choices(
            writer,
            HOUSEHOLD_SCOPE,
            Choices(trust_start_percent=50),
            by=adult_chat.person.id,
            why="strangers are believed less",
            now=datetime.now(UTC),
        )
        await writer.commit()
    system = await context_of(adult_chat)
    assert speaker_line(system) == [f'{TRUST_HEADING}{{"percent": 50, "facts": 0}}']
    assert line_of(system, memory_id)["trust"] == {"percent": 50, "facts": 0}


async def test_the_judge_reads_the_trust_of_the_one_who_asks(
    adult_chat: ChatPilot, child_chat: ChatPilot
):
    alex = adult_chat.person
    # Since 2026-11-23 the judge reads trust, in his case, and only that of the one who asks:
    # the figure, how many facts it rests on, and the last facts. Alex's own word about Sam
    # comes with Alex's figure in Sam's turns; in the case of Alex's rename it is a memory read
    # as his own words, with no figure of its own, next to Alex's trust.
    said = "Sam wants to call Alex by the name Alex B."
    memory_id = await kept(adult_chat, said, "Sam")
    await facts_about(adult_chat, alex, "lied", "lied")
    expected = figure(70, "lied", "lied")
    assert line_of(await context_of(child_chat), memory_id)["trust"] == expected
    first = len(adult_chat.server.calls)
    reply = await adult_chat.say_with_tool(
        "profile_rename", {"name": "Alex B"}, judge="do: they asked"
    )
    assert reply.actions == [{"tool": "profile_rename", "status": "done"}]
    calls = adult_chat.server.calls[first:]
    judged = [call for call in calls if fakes.is_judge_call(call.system)]
    assert judged
    # The turn itself was given the speaker's figure.
    assert any(speaker_line(call.system) for call in calls if not fakes.is_judge_call(call.system))
    for call in judged:
        case = json.loads(call.messages[0].content)
        # The judge read that memory, and the trust of Alex, who asks, in the case.
        assert [line["content"] for line in case.get("remembered", [])] == [said]
        assert all("trust" not in line for line in case["remembered"])
        trust = case["trust"]
        assert {key: trust[key] for key in expected} == expected
        assert [fact["outcome"] for fact in trust["lately"]] == ["lied", "lied"]
        assert all(FACT_ABOUT in fact["about"] for fact in trust["lately"])
        # And it is the case that holds it: the context of the turn is not copied into it.
        assert TRUST_HEADING not in call.system + json.dumps(case, ensure_ascii=False)


def _imports_of(path: Path, package: str) -> set[str]:
    """The modules ``path`` imports (relative imports made whole from ``package``)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parent = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{parent}.{base}" if base else parent
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_no_policy_code_imports_trust():
    root = Path(domo_core.__file__).parent
    modules = [
        *sorted((root / "policy").rglob("*.py")),
        *sorted((root / "confirmations").rglob("*.py")),
        *sorted((root / "llm").glob("spending*.py")),
        # The barrier: where every call of a tool is let through or not.
        root / "conversation" / "execution.py",
    ]
    assert root / "llm" / "spending.py" in modules
    assert root / "confirmations" / "judge.py" in modules
    assert root / "policy" / "barrier.py" in modules and root / "policy" / "subjects.py" in modules
    assert any(path.parent.name == "policy" for path in modules)
    # The judge's case gathers the trust of the one who asks; nothing else of these reads it.
    gathers = root / "confirmations" / "case.py"
    assert gathers in modules
    importers = [
        path
        for path in modules
        if any(
            name == "domo_core.trust" or name.startswith("domo_core.trust.")
            for name in _imports_of(
                path, ".".join(("domo_core", *path.relative_to(root).parent.parts))
            )
        )
    ]
    assert importers == [gathers]


def test_the_import_check_sees_every_shape_of_import(tmp_path: Path):
    """The check above would see the trust imported in any of these ways; a docstring that names
    it is no import."""
    for source in (
        "import domo_core.trust.service",
        "from domo_core.trust import service",
        "from domo_core import trust",
        "from ..trust.service import trust_of",
        "if TYPE_CHECKING:\n    from domo_core.trust.service import Trust",
    ):
        path = tmp_path / "module.py"
        path.write_text(source, encoding="utf-8")
        imported = _imports_of(path, "domo_core.policy")
        assert any(name.startswith("domo_core.trust") for name in imported), source
    path = tmp_path / "module.py"
    path.write_text('"""See ``domo_core.trust.shown`` and trust_look."""', encoding="utf-8")
    assert _imports_of(path, "domo_core.policy") == set()


async def test_a_failed_read_of_the_scores_leaves_the_turn_going(
    adult_chat: ChatPilot, child_chat: ChatPilot, monkeypatch: pytest.MonkeyPatch
):
    memory_id = await kept(child_chat, PINE_NUTS, "Alex")

    async def broken(*_: object, **__: object) -> dict[uuid.UUID, Any]:
        raise RuntimeError("the facts are gone")

    monkeypatch.setattr(shown, "trust_of_many", broken)
    system = await context_of(adult_chat)
    # The memory is there, still marked, without a figure; no line for the person.
    line = line_of(system, memory_id)
    assert line["unconfirmed"] is True and "trust" not in line
    assert speaker_line(system) == []
    # The search finds it all the same.
    reply = await adult_chat.say_with_tool("memory_search", {"words": "pine nuts"})
    assert reply.actions == [{"tool": "memory_search", "status": "done"}]
    [found] = [m for m in reply.model_results[-1]["memories"] if m["id"] == memory_id]
    assert found["unconfirmed"] is True and "trust" not in found
