"""The described action carries its facts (spec 5.4): typed values that a limit will read, written
by the code of each tool (never by the model), stored with the request and audited with the
decision. A note is described by its length, never by its words; a person is ``person:<uuid>``.

An action stored before the facts existed (five fields) still reads back, with no facts.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.audit.log import FORBIDDEN_DETAIL_KEYS
from domo_core.devices.service import begin_pairing, finish_pairing
from domo_core.household.models import Invitation
from domo_core.household.service import GUEST_DAYS_DEFAULT, create_invitation, disable
from domo_core.household.succession import SuccessorEntry, note_death, replace_successors
from domo_core.notes.models import Note
from domo_core.outward.models import ServiceAccount, account_ref
from domo_core.people.models import AccessPromise
from domo_core.policy.actions import EMPTY_FACTS, DescribedAction
from domo_core.policy.catalog import (
    ACTION_FACTS,
    DEFAULT_POLICY,
    GIVES_ACCESS,
    KNOWN_ACTIONS,
    MAKES_SOMEONE_NEW,
)
from domo_core.policy.roles import ROLES
from domo_core.tools.context import ToolContext
from domo_core.tools.registry import TOOLS
from sqlalchemy import update
from test_spending import add_key

# --- the field ---------------------------------------------------------------------------------


def test_an_action_carries_its_facts_and_reads_back():
    action = DescribedAction(
        "core",
        "household.invite",
        None,
        "permissions",
        "household",
        {"role": "guest", "days": 2, "display_name": "Alex"},
    )
    assert DescribedAction.from_json(action.as_json()) == action
    assert dict(action.facts) == {"role": "guest", "days": 2, "display_name": "Alex"}


def test_an_action_has_no_facts_unless_it_is_given_some():
    action = DescribedAction("core", "household.list", None, "read", "household")
    assert action.facts == {}
    assert action.facts is EMPTY_FACTS
    assert action.as_json()["facts"] == {}


def test_the_facts_are_written_as_plain_json():
    action = DescribedAction(
        "core",
        "note.withdraw",
        "note:x",
        "message",
        "household",
        {"recipient": "person:x", "seen": False, "days": None, "length": 3},
    )
    written = action.as_json()
    assert written["facts"] == {"recipient": "person:x", "seen": False, "days": None, "length": 3}
    assert type(written["facts"]) is dict  # what a database holds, not a read-only view
    assert json.loads(json.dumps(written)) == written
    # The booleans stay booleans, the numbers numbers: what a limit reads is typed.
    assert DescribedAction.from_json(json.loads(json.dumps(written))) == action


def test_the_facts_of_an_action_cannot_be_changed_afterwards():
    given = {"role": "guest"}
    action = DescribedAction("core", "household.invite", None, "permissions", "household", given)
    given["role"] = "owner"  # the caller's own dict is not the action's
    assert action.facts == {"role": "guest"}
    with pytest.raises(TypeError):
        action.facts["role"] = "owner"  # type: ignore[index]
    with pytest.raises(TypeError):
        EMPTY_FACTS["x"] = 1  # type: ignore[index]


def test_an_action_with_facts_is_equal_to_the_same_action_and_still_hashable():
    one = DescribedAction("core", "note.leave", "person:x", "message", "household", {"length": 3})
    other = DescribedAction("core", "note.leave", "person:x", "message", "household", {"length": 3})
    different = DescribedAction(
        "core", "note.leave", "person:x", "message", "household", {"length": 4}
    )
    assert one == other and one != different
    assert hash(one) == hash(other)
    assert len({one, other, different}) == 2


def test_an_action_stored_before_facts_reads_back_with_none():
    old = {
        "connector": "core",
        "action_type": "note.leave",
        "target": "person:x",
        "effect": "message",
        "destination": "household",
    }
    action = DescribedAction.from_json(old)
    assert action.facts == {}
    assert action == DescribedAction("core", "note.leave", "person:x", "message", "household")


@pytest.mark.parametrize(
    "facts",
    [
        {"k": [1]},
        {"k": {"a": 1}},
        {"x" * 65: 1},
        {"k": "y" * 201},
        {"k": 1.5},
        {"k": ("a",)},
        {"ok": 1, "bad": [None]},
    ],
)
def test_from_json_refuses_facts_that_are_not_simple_values(facts):
    stored = {**_FIVE_FIELDS, "facts": facts}
    with pytest.raises(ValueError):
        DescribedAction.from_json(stored)


@pytest.mark.parametrize("facts", [None, [], ["k", 1], "facts", 3])
def test_from_json_refuses_facts_that_are_not_an_object(facts):
    with pytest.raises(ValueError):
        DescribedAction.from_json({**_FIVE_FIELDS, "facts": facts})


def test_from_json_still_refuses_a_field_too_many():
    with pytest.raises(ValueError):
        DescribedAction.from_json({**_FIVE_FIELDS, "facts": {}, "arguments": "x"})


def test_the_limits_of_a_fact_are_inclusive():
    facts = {"k" * 64: "v" * 200, "n": 0, "b": False, "none": None}
    action = DescribedAction("core", "x.y", None, "read", "household", facts)
    assert DescribedAction.from_json(action.as_json()) == action


@pytest.mark.parametrize("facts", [{"k": [1]}, {"x" * 65: 1}, {"k": "y" * 201}, {"k": 2.5}])
def test_an_action_cannot_be_made_with_facts_that_would_not_read_back(facts):
    # A tool that wrote such a fact would store a request that could never be replayed.
    with pytest.raises(ValueError):
        DescribedAction("core", "x.y", None, "read", "household", facts)


_FIVE_FIELDS: dict[str, Any] = {
    "connector": "core",
    "action_type": "note.leave",
    "target": "person:x",
    "effect": "message",
    "destination": "household",
}

# --- the catalog declares the facts --------------------------------------------------------------


@pytest.mark.parametrize(
    ("action_type", "declared"),
    [
        (
            "household.invite",
            {
                "role": "role",
                "days": "number",
                "no_end": "boolean",
                "display_name": "text",
                "owner_first": "boolean",
                "exception_of": "text",
                "serves_the_speaker": "boolean",
            },
        ),
        (
            "household.change_role",
            {
                "person": "person",
                "current_role": "role",
                "role": "role",
                "days": "number",
                "no_end": "boolean",
                "returns": "boolean",
                "comes_to_live": "boolean",
                "off_the_guests_key_rule": "boolean",
                "may_then_agree_for_a_child": "boolean",
                "has_account": "boolean",
                "state": "text",
                "made_role": "text",
                "exception_of": "text",
                "serves_the_speaker": "boolean",
            },
        ),
        (
            "household.remove",
            {
                "person": "person",
                "current_role": "role",
                "is_self": "boolean",
                "given_by_domo": "boolean",
                "has_account": "boolean",
                "state": "text",
                "made_role": "text",
            },
        ),
        (
            "household.bring_back",
            {
                "person": "person",
                "role": "role",
                "days": "number",
                "no_end": "boolean",
                "returns": "boolean",
                "has_account": "boolean",
                "state": "text",
                "made_role": "text",
                "exception_of": "text",
                "serves_the_speaker": "boolean",
            },
        ),
        # Every type that gives an access says whether it goes to the person who speaks.
        ("household.send_link", {"exception_of": "text", "serves_the_speaker": "boolean"}),
        (
            "access.promise_keep",
            {
                "role": "role",
                "days": "number",
                "exception_of": "text",
                "serves_the_speaker": "boolean",
            },
        ),
        ("people.open", {"exception_of": "text"}),
        ("people.link", {"from": "person", "to": "person"}),
        ("people.unlink", {"from": "person", "to": "person"}),
        # The two records a merge makes one, and splits again.
        ("people.merge", {"kept": "person", "absorbed": "person"}),
        ("people.split", {"kept": "person", "absorbed": "person"}),
        (
            "household.transfer_ownership",
            {"from": "person", "to": "person", "requester_role": "role", "to_role": "role"},
        ),
        ("household.cancel_invitation", {"role": "role", "given_by_domo": "boolean"}),
        # Whether forgetting takes away the access of someone who lives here: the judge weighs
        # it, and an owner's limit may read it.
        ("people.forget", {"loses_access": "boolean"}),
        ("people.forget_me", {"loses_access": "boolean"}),
        ("note.leave", {"recipient": "person", "recipient_role": "role", "length": "number"}),
        ("note.withdraw", {"recipient": "person", "seen": "boolean"}),
        ("note.keep", {"recipient": "person", "days": "number"}),
        ("device.remove", {"person": "person", "is_self": "boolean"}),
        ("successors.set", {"count": "number"}),
        ("successors.note_death", {"name": "text"}),
        ("successors.undo_death", {"name": "text"}),
        # Whose texts, writing note or language someone else changes, and their role.
        ("texts.for_someone", {"person": "person", "person_role": "role"}),
        # Whether the house's language follows (the owner's own, to another one).
        ("texts.language", {"house_follows": "boolean"}),
    ],
)
def test_the_catalog_declares_the_facts_of_each_action_and_their_types(action_type, declared):
    assert dict(ACTION_FACTS[action_type]) == declared


def test_every_action_of_the_catalog_declares_its_facts_even_when_it_has_none():
    assert set(ACTION_FACTS) >= KNOWN_ACTIONS
    assert {tool.action_type for tool in TOOLS.values()} <= set(ACTION_FACTS)
    for action_type in (
        "household.list",
        "note.list_sent",
        "profile.rename",
        "policy.update",
        "successors.show",
    ):
        assert dict(ACTION_FACTS[action_type]) == {}


def test_the_declared_facts_cannot_be_changed_by_accident():
    with pytest.raises(TypeError):
        ACTION_FACTS["note.leave"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        ACTION_FACTS["note.leave"]["text"] = "text"  # type: ignore[index]


def test_a_fact_never_holds_a_free_text_of_any_length():
    # Only the names of people are text (the name of someone invited, the name of a successor
    # who died, the name of who gave a key), and the kind of a key (one of the provider kinds); a
    # note is its length.
    texts = {
        (action_type, name)
        for action_type, facts in ACTION_FACTS.items()
        for name, kind in facts.items()
        if kind == "text"
    }
    assert texts == {
        ("household.invite", "display_name"),
        # The id of the owner's directive whose words let a call of the assistant's through
        # (``people.directives``): on every type that gives an access or makes someone new.
        *(
            (action_type, "exception_of")
            for action_type in sorted(GIVES_ACCESS | MAKES_SOMEONE_NEW)
        ),
        # A new link for an invitation that waits reads the facts of inviting (F1, 2026-11-16).
        ("household.invite_again", "display_name"),
        ("successors.note_death", "name"),
        ("successors.undo_death", "name"),
        ("key.share", "key_of"),
        ("key.share", "key_kind"),
        ("spending.limit", "key_of"),
        ("spending.limit", "key_kind"),
        ("key.admin", "key_of"),
        ("key.admin", "key_kind"),
        # The names of a tool of Domo's and of one of its declared operations (never what a
        # program wrote: an operation it does not declare is refused before it is described), and
        # the period of an agreement (day, week or month).
        ("program.begin", "tool"),
        ("program.begin", "operation"),
        ("agreement.keep", "tool"),
        ("agreement.keep", "operation"),
        ("agreement.keep", "per"),
        # A program run by hand that carries a yes: the operation it is for, and whose yes
        # (self, adult or owner).
        ("program.run", "operation"),
        ("program.run", "approver"),
        # Going out (step 2): an address by its host, the tool it is for and its zone as written
        # (house or internet); an account by its name and the kind of its place. Never a path, a
        # header's template or a value.
        *(
            (opening, fact)
            for opening in ("net.open", "net.open_host")
            for fact in ("host", "tool", "zone")
        ),
        ("net.close", "host"),
        ("net.close", "tool"),
        ("net.look", "tool"),
        ("account.ask", "name"),
        ("account.ask", "place"),
        # The instant the owner removed it, as an ISO date and time: what a yes is held against.
        ("account.ask", "removed_at"),
        ("account.remove", "name"),
        ("account.share", "name"),
        # What a program sends out: the zone of its address as written (house or internet).
        ("program.out", "zone"),
        # A role, a removal and a return act on a record: where it stands (one closed word) and
        # the name of the role made for the household the action is about.
        *(
            (action_type, fact)
            for action_type in ("household.change_role", "household.remove", "household.bring_back")
            for fact in ("state", "made_role")
        ),
    }


# --- each tool describes the facts it declares ---------------------------------------------------


@dataclass
class Scene:
    owner: Person
    alex: Person  # an adult
    sam: Person  # a child
    tom: Person  # a guest
    dana: Person  # an adult who left
    waiting: Invitation  # for "Jo", a child
    seen_note: Note  # from the owner to Alex, shown on Alex's page
    guest_note: Note  # from the owner to Tom
    session_factory: Any
    clock: Any
    alexs_key: str = ""  # the id of a key Alex gave
    alexs_anthropic_key: str = ""  # the id of an Anthropic key Alex gave (it has an admin key)
    promise_id: str = ""  # the id of a promise of access that waits (for an adult)

    @property
    def in_ten_days(self) -> str:
        """The day ten days from today (UTC), written ``YYYY-MM-DD``."""
        return (self.clock().astimezone(UTC).date() + timedelta(days=10)).isoformat()


async def begin_and_finish_pairing(writer, person_id, now, name):
    code = await begin_pairing(writer, person_id, now)
    assert await finish_pairing(writer, code, name, now) is not None


@pytest.fixture
async def scene(add_member, session_factory, live_clock) -> Scene:
    owner = await add_member("Owner", "owner")
    alex = await add_member("Alex", "adult", username="alex")
    sam = await add_member("Sam", "child")
    tom = await add_member("Tom", "guest")
    dana = await add_member("Dana", "adult")
    async with session_factory() as writer:
        await disable(writer, await writer.get_one(Person, dana.id), live_clock(), by=dana.id)
        waiting, _ = await create_invitation(
            writer,
            display_name="Jo",
            role="child",
            guest_days=None,
            created_by=owner.id,
            now=live_clock(),
        )
        note = Note(
            author_id=owner.id,
            recipient_id=alex.id,
            text="Bonjour tout le monde",
            created_at=live_clock(),
        )
        writer.add(note)
        for_the_guest = Note(
            author_id=owner.id,
            recipient_id=tom.id,
            text="La clé est sous le tapis",
            created_at=live_clock(),
        )
        writer.add(for_the_guest)
        # A device of the owner's, for ``device_remove``.
        await begin_and_finish_pairing(writer, owner.id, live_clock(), "Desk")
        await writer.flush()
        await writer.execute(update(Note).where(Note.id == note.id).values(seen_at=live_clock()))
        # Paul, on the owner's list of successors, whose death is noted: only a death noted can be
        # undone (it is read before anything is asked).
        paul = SuccessorEntry(person_id=None, name="Paul", relation=None, access_way=None)
        await replace_successors(writer, [paul], live_clock())
        await note_death(writer, "Paul", live_clock())
        # A promise of access that waits, for someone the assistant does not know yet.
        promise = AccessPromise(
            description="Alex's cousin",
            role="adult",
            standing="lives_here",
            asked_by=f"person:{owner.id}",
            is_order=True,
            reason="asked by a person",
        )
        writer.add(promise)
        # An account of Alex's own, and one of the house's (going out, step 2).
        for name, whose in (("music", alex.id), ("lights", None)):
            writer.add(
                ServiceAccount(
                    name=name,
                    person_id=whose,
                    ref=account_ref(uuid.uuid4()),
                    hosts=[{"host": "192.168.1.20", "port": 8123}],
                    place={"kind": "basic"},
                    given_by=alex.id if whose else owner.id,
                )
            )
        await writer.commit()
    alexs_key = await add_key(session_factory, alex.id)
    alexs_anthropic_key = await add_key(
        session_factory, alex.id, kind="anthropic", base_url="https://api.anthropic.com"
    )
    return Scene(
        owner,
        alex,
        sam,
        tom,
        dana,
        waiting,
        note,
        for_the_guest,
        session_factory,
        live_clock,
        alexs_key=alexs_key.id,
        alexs_anthropic_key=alexs_anthropic_key.id,
        promise_id=str(promise.id),
    )


def _context(person: Person, scene: Scene) -> ToolContext:
    return ToolContext(
        session_factory=scene.session_factory,
        actor=actor_of(person),  # type: ignore[arg-type]
        person=person,
        conversation_id=uuid.uuid4(),
        origin="https://domo.example.com/",
        gatekeeper=None,  # type: ignore[arg-type]
        clock=scene.clock,
        registry=TOOLS,
        policy=DEFAULT_POLICY,
        reply_message_id=None,
    )


async def _described(tool: str, arguments: dict[str, Any], who: Person, scene: Scene):
    return await TOOLS[tool].describe(
        arguments,
        actor_of(who),
        _context(who, scene),  # type: ignore[arg-type]
    )


# One valid call for each tool that has facts, and the facts it must describe (the owner calls it,
# unless the action is never the owner's: ``_CALLERS``).
_CALLS: dict[str, Any] = {
    "household.invite": (
        "household_invite",
        {"display_name": "Léa", "role": "guest"},
        lambda s: {
            "role": "guest",
            "days": GUEST_DAYS_DEFAULT,
            "no_end": False,
            "display_name": "Léa",
            "owner_first": False,
            "exception_of": None,
            "serves_the_speaker": False,
        },
    ),
    "household.change_role": (
        "household_change_role",
        {"person": "Alex", "role": "guest", "guest_days": 5},
        lambda s: {
            "person": f"person:{s.alex.id}",
            "current_role": "adult",
            "role": "guest",
            "days": 5,
            "no_end": False,
            "returns": False,
            "made_role": None,
            "has_account": True,
            "state": "has_access",
            "exception_of": None,
            "serves_the_speaker": False,
            "comes_to_live": False,
            "off_the_guests_key_rule": False,
            "may_then_agree_for_a_child": False,
        },
    ),
    "access.promise_keep": (
        "promise_keep",
        lambda s: {"promise": s.promise_id, "person": "Dana"},
        lambda s: {
            "role": "adult",
            "days": None,
            "exception_of": None,
            "serves_the_speaker": False,
        },
    ),
    "household.remove": (
        "household_remove",
        {"person": "Sam"},
        lambda s: {
            "person": f"person:{s.sam.id}",
            "current_role": "child",
            "is_self": False,
            # Sam was not given his access by the assistant.
            "given_by_domo": False,
            "made_role": None,
            "has_account": False,
            "state": "has_access",
        },
    ),
    "household.leave": ("household_leave", {}, lambda s: {"current_role": "child"}),
    "household.bring_back": (
        "household_bring_back",
        {"person": "Dana", "role": "child"},
        lambda s: {
            "person": f"person:{s.dana.id}",
            "role": "child",
            "days": None,
            "no_end": False,
            "returns": False,
            "made_role": None,
            "has_account": False,
            "state": "gone",
            "exception_of": None,
            "serves_the_speaker": False,
        },
    ),
    "household.transfer_ownership": (
        "household_transfer_ownership",
        {"person": "Alex"},
        lambda s: {
            "from": f"person:{s.owner.id}",
            "to": f"person:{s.alex.id}",
            "requester_role": "owner",
            "to_role": "adult",
        },
    ),
    "household.cancel_invitation": (
        "household_cancel_invitation",
        {"display_name": "Jo"},
        lambda s: {"role": "child", "given_by_domo": False},
    ),
    "household.invite_again": (
        "invitation_link_again",
        {"display_name": "Jo"},
        lambda s: {
            "role": "child",
            "days": None,
            "no_end": False,
            "display_name": "Jo",
            "owner_first": False,
            "exception_of": None,
            "serves_the_speaker": False,
            "given_by_domo": False,
        },
    ),
    "people.forget": ("record_forget", {"person": "Alex"}, lambda s: {"loses_access": True}),
    "people.forget_me": ("forget_me", {}, lambda s: {"loses_access": False}),
    "people.open": (
        "record_open",
        {"name": "Tom", "standing": "visiting", "ground": "comes"},
        lambda s: {"exception_of": None},
    ),
    "note.leave": (
        "note_leave",
        {"recipient": "Alex", "text": "  Bonjour\r\nà tous "},
        lambda s: {
            "recipient": f"person:{s.alex.id}",
            "recipient_role": "adult",
            "length": len("Bonjour\nà tous"),
        },
    ),
    "note.withdraw": (
        "note_withdraw",
        lambda s: {"note": str(s.seen_note.id)},
        lambda s: {"recipient": f"person:{s.alex.id}", "seen": True},
    ),
    "note.keep": (
        "note_keep",
        lambda s: {"note": str(s.guest_note.id), "until": s.in_ten_days},
        lambda s: {"recipient": f"person:{s.tom.id}", "days": 10},
    ),
    "people.link": (
        "link_note",
        {"person": "Alex", "relation": "friend of", "to": "Sam"},
        lambda s: {"from": f"person:{s.alex.id}", "to": f"person:{s.sam.id}"},
    ),
    "people.unlink": (
        "link_drop",
        {"person": "Alex", "relation": "friend of", "to": "Sam"},
        lambda s: {"from": f"person:{s.alex.id}", "to": f"person:{s.sam.id}"},
    ),
    "people.merge": (
        "records_merge",
        {"keep": "Sam", "absorb": "Dana"},
        lambda s: {"kept": f"person:{s.sam.id}", "absorbed": f"person:{s.dana.id}"},
    ),
    "device.remove": (
        "device_remove",
        lambda s: {"device": "Desk"},
        lambda s: {"person": f"person:{s.owner.id}", "is_self": True},
    ),
    "successors.set": (
        "successors_set",
        {"successors": [{"person": "Alex"}, {"name": "Paul"}]},
        lambda s: {"count": 2},
    ),
    "successors.note_death": (
        "successors_note_death",
        {"name": "  Paul "},
        lambda s: {"name": "Paul"},
    ),
    "successors.undo_death": (
        "successors_undo_death",
        {"name": "  Paul "},
        lambda s: {"name": "Paul"},
    ),
    "chain.run": (
        "do_in_order",
        {
            "steps": [
                {"tool": "household_change_role", "arguments": {"person": "Sam", "role": "adult"}},
                {"tool": "household_remove", "arguments": {"person": "Sam"}},
            ]
        },
        lambda s: {"steps": 2, "checked_in_turn": 0, "waits": 0},
    ),
    "models.choose": (
        "models_choose",
        {"why": "spend less", "task": "conversation", "level": "small"},
        lambda s: {"own_key": False},
    ),
    # A key is told by its kind and the name of who gave it, for whoever says yes.
    "key.share": (
        "key_share",
        lambda s: {"key": s.alexs_key, "with": "person", "person": "Sam"},
        lambda s: {"key_kind": "openai_compatible", "key_of": "Alex"},
    ),
    # Removing a key says whether it is the last that works, or the general key with no other
    # key of its giver's to take over (task 25: facts the judge weighs, never a refusal).
    "key.remove": (
        "key_remove",
        lambda s: {"key": s.alexs_key},
        lambda s: {"last_key": False, "general_key_without_heir": False},
    ),
    "spending.limit": (
        "key_limit",
        lambda s: {"key": s.alexs_key, "amount": 5, "currency": "EUR"},
        lambda s: {"key_kind": "openai_compatible", "key_of": "Alex"},
    ),
    "key.admin": (
        "key_admin",
        lambda s: {"key": s.alexs_anthropic_key},
        lambda s: {"key_kind": "anthropic", "key_of": "Alex"},
    ),
    # Whose writing note is changed: never the note itself.
    "texts.for_someone": (
        "texts_for_someone",
        {"person": "Sam", "what": "note", "note": "Short sentences, no jokes."},
        lambda s: {"person": f"person:{s.sam.id}", "person_role": "child"},
    ),
    # The owner's own language, another than the house's: the house's follows.
    "texts.language": ("texts_language", {"language": "es"}, lambda s: {"house_follows": True}),
    # Going out (step 2): an address of the house for a tool, and on the internet.
    "net.open": (
        "address_open",
        {"host": "api.example.com", "port": 443, "tool": "lights"},
        lambda s: {
            "host": "api.example.com",
            "port": 443,
            "tool": "lights",
            "on_host": False,
            "zone": "internet",
            "outward": True,
            # Not closed by the owner: no day.
            "closed_by_owner": False,
            "closed_on": None,
        },
    ),
    # A port of the machine that hosts Domo, in the owner's turn: an opening that waits for his
    # yes is described as an opening, ``net.open``; with Domo's right, ``net.open_host``.
    "net.close": (
        "address_close",
        {"host": "192.168.1.20", "port": 8123, "tool": "lights"},
        lambda s: {"host": "192.168.1.20", "port": 8123, "tool": "lights"},
    ),
    "net.look": ("addresses_look", {"tool": "lights"}, lambda s: {"tool": "lights"}),
    "account.ask": (
        "account_ask",
        {
            "name": "heating",
            "for": "house",
            "hosts": [
                {"host": "192.168.1.20", "port": 8123},
                {"host": "api.example.com", "port": 443},
            ],
            "place": {"kind": "basic"},
        },
        lambda s: {
            "name": "heating",
            "place": "basic",
            "for_house": True,
            "giver": f"person:{s.owner.id}",
            "hosts": 2,
            "outward": True,
            # Never removed by the owner: no day (Task 27).
            "removed_by_owner": False,
            "removed_on": None,
            "removed_at": "",
        },
    ),
    "account.remove": (
        "account_remove",
        {"name": "music", "for": "me"},
        lambda s: {"name": "music", "for_house": False, "of": f"person:{s.alex.id}"},
    ),
    "account.share": (
        "account_share",
        {"name": "lights", "guests": True, "domo": False},
        lambda s: {"name": "lights", "guests": True, "domo": False},
    ),
}


# Who calls the tools of ``_CALLS`` that the owner never does (the owner never leaves), or on a
# key that is someone else's (only its giver shares or limits it).
_CALLERS: dict[str, Any] = {
    "household.leave": lambda s: s.sam,
    "key.share": lambda s: s.alex,
    "key.remove": lambda s: s.alex,
    "spending.limit": lambda s: s.alex,
    "key.admin": lambda s: s.alex,
    "account.remove": lambda s: s.alex,
    "people.forget_me": lambda s: s.tom,
}


def _the_kind_is_respected(kind: str, value: object, name: str = "") -> bool:
    match kind:
        case "text":
            # The exception of a directive is none for a call that is no exception, and so is a
            # made role for a built-in role alone.
            return isinstance(value, str) or (
                name in ("exception_of", "made_role") and value is None
            )
        case "number":
            return value is None or (isinstance(value, int) and not isinstance(value, bool))
        case "boolean":
            return isinstance(value, bool)
        case "person":
            return isinstance(value, str) and value.startswith("person:")
        case "role":
            return value in ROLES
        case "date":
            return value is None or (isinstance(value, str) and len(value) == len("YYYY-MM-DD"))
    return False


# What has facts but no call below: a program's beginning has no tool of the core (its facts are
# tried in ``test_madetools_yes.py``), nor do the agreements without a tool of Domo that costs
# (``test_agreements.py``), nor a program run by hand, whose facts need one of its stops
# (``test_program_run.py``), nor a port of the machine that hosts Domo opened under a right of
# his (``test_outward_tools.py``), nor what a program sends out (``test_madetools_out.py``).
_DESCRIBED_ELSEWHERE: Final = frozenset(
    {
        "program.begin",
        "agreement.keep",
        "agreement.drop",
        "program.run",
        "net.open_host",
        "program.out",
        # The gestures of the people of the house that have no tool yet (later tasks): their
        # facts are the exception of a directive and whether it serves the person who speaks.
        "household.send_link",
        # A split needs a merge to split first: its facts name the two records of the merge
        # (``test_people_merge.py``, where what it undoes is read from them).
        "people.split",
    }
)


def test_every_action_with_facts_has_a_call_below():
    with_facts = {action_type for action_type, facts in ACTION_FACTS.items() if facts}
    assert with_facts - _DESCRIBED_ELSEWHERE == set(_CALLS)


def test_the_facts_are_declared_for_actions_of_the_catalog_and_are_not_secret_looking():
    assert {action_type for action_type, facts in ACTION_FACTS.items() if facts} <= KNOWN_ACTIONS
    # The audit log refuses details with these keys: a fact of that name would lose the decision.
    names = {name.casefold() for facts in ACTION_FACTS.values() for name in facts}
    assert not names & FORBIDDEN_DETAIL_KEYS


@pytest.mark.parametrize("action_type", sorted(_CALLS))
async def test_each_tool_describes_exactly_the_facts_the_catalog_declares(action_type, scene):
    tool, arguments, expected = _CALLS[action_type]
    if callable(arguments):
        arguments = arguments(scene)
    caller = _CALLERS.get(action_type, lambda s: s.owner)(scene)
    action = await _described(tool, arguments, caller, scene)
    assert action.action_type == action_type
    declared = ACTION_FACTS[action_type]
    assert set(action.facts) == set(declared)
    assert dict(action.facts) == expected(scene)
    for name, kind in declared.items():
        assert _the_kind_is_respected(kind, action.facts[name], name), (
            name,
            kind,
            action.facts[name],
        )
    # What is stored is what the people and the audit log get, and it reads back.
    assert DescribedAction.from_json(json.loads(json.dumps(action.as_json()))) == action


async def test_the_tools_that_have_no_facts_describe_none(scene):
    for tool, arguments in (
        ("household_list", {}),
        ("note_list_sent", {}),
        ("profile_rename", {"name": "Owner B"}),
        ("successors_show", {}),
    ):
        action = await _described(tool, arguments, scene.owner, scene)
        assert action.facts == {}, tool


async def test_a_guest_stays_the_days_the_call_says_or_the_default(scene):
    kept = await _described(
        "household_invite",
        {"display_name": "Lou", "role": "guest", "guest_days": 9},
        scene.owner,
        scene,
    )
    assert kept.facts["days"] == 9
    # Not a guest: no stay, so no days.
    adult = await _described(
        "household_invite", {"display_name": "Lou", "role": "adult"}, scene.owner, scene
    )
    assert adult.facts["days"] is None
    changed = await _described(
        "household_change_role", {"person": "Alex", "role": "guest"}, scene.owner, scene
    )
    assert changed.facts["days"] == GUEST_DAYS_DEFAULT
    back = await _described(
        "household_bring_back",
        {"person": "Dana", "role": "guest", "guest_days": 4},
        scene.owner,
        scene,
    )
    assert back.facts["days"] == 4


async def test_a_name_in_a_call_is_the_invited_name_without_the_spaces_around_it(scene):
    action = await _described(
        "household_invite", {"display_name": "  Mia  ", "role": "child"}, scene.owner, scene
    )
    assert action.facts["display_name"] == "Mia"


async def test_a_note_is_described_by_its_length_never_its_text(scene):
    words = "Un mot secret que personne d'autre ne lit"
    action = await _described(
        "note_leave", {"recipient": "Alex", "text": words}, scene.owner, scene
    )
    assert action.facts["length"] == len(words)
    written = json.dumps(action.as_json(), ensure_ascii=False)
    assert "secret" not in written and words not in written
    # Nothing else of the call is in the action either: its target is the person, not the words.
    assert action.target == f"person:{scene.alex.id}"


async def test_a_withdrawal_says_whether_the_note_was_seen(scene):
    async with scene.session_factory() as writer:
        unseen = Note(
            author_id=scene.owner.id,
            recipient_id=scene.sam.id,
            text="Pas encore vu",
            created_at=datetime.now(UTC),
        )
        writer.add(unseen)
        await writer.commit()
    action = await _described("note_withdraw", {"note": str(unseen.id)}, scene.owner, scene)
    assert dict(action.facts) == {"recipient": f"person:{scene.sam.id}", "seen": False}


async def test_removing_oneself_is_described_as_such(scene):
    action = await _described("household_remove", {"person": "Alex"}, scene.alex, scene)
    assert action.facts["is_self"] is True
    assert action.facts["current_role"] == "adult"


async def test_leaving_is_described_with_the_role_of_now_and_oneself_as_target(scene):
    async with scene.session_factory() as writer:
        await writer.execute(
            update(Person).where(Person.id == scene.alex.id).values(role="child", is_child=True)
        )
        await writer.commit()
    # Alex asks with the role Domo knew: the role of now is read again from the database.
    action = await _described("household_leave", {}, scene.alex, scene)
    assert action.target == f"person:{scene.alex.id}"
    assert action.effect == "permissions"
    assert dict(action.facts) == {"current_role": "child"}
