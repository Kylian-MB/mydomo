"""The child mark and 'visiting' are read from the person's record, not from the role's name."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from domo_core.accounts.actors import actor_of, is_visiting, marks_of
from domo_core.notes.models import Note
from domo_core.notes.upkeep import erase_notes_of_departed_guests
from domo_core.policy import barrier
from domo_core.policy.actions import DescribedAction, FactValue
from domo_core.policy.barrier import NeedsApproval
from domo_core.policy.catalog import ACTION_FACTS, DEFAULT_POLICY
from domo_core.policy.facts import facts_of
from domo_core.policy.rights import NO_RIGHTS, RightsTable
from domo_core.policy.roles import Actor
from domo_core.policy.subjects import Subject, concerned_of
from domo_core.tools.context import tools_for
from domo_core.tools.registry import TOOLS
from sqlalchemy import select

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
SOURCES = Path(__file__).resolve().parents[1] / "src" / "domo_core"


def _leave() -> DescribedAction:
    facts: dict[str, FactValue] = {
        name: "x" for name, kind in ACTION_FACTS["household.leave"].items() if kind == "text"
    }
    return DescribedAction("core", "household.leave", "person:x", "permissions", "household", facts)


def _decide(actor: Actor):
    action = _leave()
    return barrier.decide(
        actor,
        action,
        DEFAULT_POLICY,
        NO_RIGHTS,
        facts_of(actor, action, name="Someone", local_now=None),
    )


def test_a_visiting_child_still_needs_an_adult():
    # Socle 3: a child loses no protection by being a visitor.
    decision = _decide(Actor(uuid4(), "guest", child=True, visiting=True))
    assert decision == NeedsApproval("adult", judge_first=True)


async def test_the_mark_comes_from_the_record_whatever_the_role(add_person):
    marked = await add_person("Paul", "adult", is_child=True)
    plain = await add_person("Dana", "adult")
    assert actor_of(marked).child is True
    assert actor_of(plain).child is False
    assert marks_of(marked)["child"] is True
    # The built-in child role carries the mark too.
    assert actor_of(await add_person("Tom", "child")).child is True


async def test_a_guest_role_reads_as_visiting(add_person):
    guest = await add_person("Sam", "guest")
    assert actor_of(guest).visiting is True
    assert actor_of(guest).resident is False
    assert is_visiting(guest)
    visiting = await add_person("Tom", "adult", standing="visiting")
    assert actor_of(visiting).visiting is True
    assert actor_of(visiting).resident is False
    home = await add_person("Dana", "adult")
    assert actor_of(home).visiting is False
    assert actor_of(home).resident is True


def test_no_actor_is_built_from_a_role_outside_actors_py():
    allowed = {"accounts/actors.py", "policy/roles.py"}
    built = re.compile(r"Actor\([^)]*as_role\(|Actor\(\s*[\w.]+(?:\(\))?,\s*[\w.]*role")
    replaced = re.compile(r"replace\(\s*[\w.]*actor\w*\s*,[^)]*role=")
    found = []
    for path in SOURCES.rglob("*.py"):
        name = path.relative_to(SOURCES).as_posix()
        if name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if built.search(text) or replaced.search(text):
            found.append(name)
    assert found == []


async def test_notes_left_for_a_visitor_are_erased_at_the_end_of_the_stay_whatever_the_role(
    session, add_person
):
    alex = await add_person("Alex", "owner")
    visitor = await add_person(
        "Sam", "adult", standing="visiting", state="gone", disabled_at=NOW - timedelta(days=1)
    )
    session.add(Note(author_id=alex.id, recipient_id=visitor.id, text="Welcome"))
    await session.flush()
    await erase_notes_of_departed_guests(session, NOW)
    assert (await session.execute(select(Note))).scalars().all() == []


async def test_the_tools_offered_are_resolved_for_the_actor_with_its_marks(add_person):
    marked = await add_person("Paul", "adult", is_child=True)
    seen: list[Actor] = []

    class Recording(RightsTable):
        def resolve(self, actor, action_type, policy):  # type: ignore[no-untyped-def]
            seen.append(actor)
            return super().resolve(actor, action_type, policy)

    tools_for(actor_of(marked), TOOLS, DEFAULT_POLICY, rights=Recording(()))
    assert seen and all(actor.child for actor in seen if actor.subject == "person")


async def test_whose_thing_it_is_follows_the_child_mark(session, add_person, add_account):
    alex = await add_person("Alex", "owner")
    await add_account(alex, "alex")
    paul = await add_person("Paul", "adult", is_child=True)
    await add_account(paul, "paul")
    thing = Subject("thing:1", paul.id)
    assert await concerned_of(session, thing, _leave(), NOW) == alex.id
    assert isinstance(alex.id, UUID)


def test_a_child_mark_changed_mid_turn_makes_the_prompt_stale():
    from domo_core.conversation.turn import Prompt, prompted_as

    prompt = Prompt(display_name="Paul", role="adult", can_act=True)
    assert prompted_as(prompt, Actor(uuid4(), "adult"))
    assert not prompted_as(prompt, Actor(uuid4(), "adult", child=True))
    assert not prompted_as(prompt, Actor(uuid4(), "owner"))
