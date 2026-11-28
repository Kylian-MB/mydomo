"""A note may be left for a guest, and is erased once the guest has left (spec 12.1 of the rights
design).

The catalog no longer refuses a note for a guest: a guest who is there can receive one. When the
guest leaves, because their stay ended or because they were removed, the notes they received are
erased for good, read or not, by the upkeep (``notes.upkeep``), a small task of the core that runs
when it starts and every minute after, unless their author keeps them until a day (``note_keep``,
spec 5.10): then they are erased at the end of that day if the guest is still away. The notes the
guest left for others stay, and so do the notes of an adult or a child who was removed (they have
them back if they come back). Each erasure is in the audit log (``notes.erased``, by the system):
who lost notes and how many, never what they said.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from functools import partial

import anyio
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.household.service import disable
from domo_core.household.upkeep import departures_pass, pause_rights_of_those_who_left
from domo_core.notes.models import Note
from domo_core.notes.upkeep import erase_notes_of_departed_guests, notes_pass
from domo_core.policy.catalog import ACTION_FACTS, DEFAULT_POLICY
from domo_core.policy.models import PolicyRight
from domo_core.upkeep import UPKEEP_SECONDS, run_upkeep
from domo_core.web.app import create_app
from domo_core.web.deps import get_clock
from sqlalchemy import select

HELLO = "Welcome, the key is under the mat."
GOODBYE = "Thanks for having us."


async def add_note(session, author: Person, recipient: Person, text: str, **more) -> Note:
    note = Note(author_id=author.id, recipient_id=recipient.id, text=text, **more)
    session.add(note)
    await session.flush()
    return note


async def texts_of_notes(session) -> set[str]:
    return set((await session.execute(select(Note.text))).scalars())


async def erasures(session) -> list[AuditEntry]:
    found = await session.execute(
        select(AuditEntry).where(AuditEntry.action == "notes.erased").order_by(AuditEntry.id)
    )
    return list(found.scalars())


# --- a guest may receive a note ------------------------------------------------------------------


async def test_a_guest_may_receive_a_note(adult_chat, guest_member, stored_notes):
    # The household's old rule is gone: the catalog has no limit left for a note.
    assert "note.leave" not in DEFAULT_POLICY.limits
    # A limit of the owner's can still read who the note is for.
    assert ACTION_FACTS["note.leave"]["recipient_role"] == "role"
    reply = await adult_chat.say_with_tool(
        "note_leave", {"recipient": "Robin", "text": "Bienvenue"}, judge="do"
    )
    assert reply.actions == [{"tool": "note_leave", "status": "done"}]
    [note] = await stored_notes()
    assert note.recipient_id == guest_member.person.id and note.text == "Bienvenue"


# --- the erasure ---------------------------------------------------------------------------------


async def test_notes_of_a_guest_are_erased_once_the_stay_ends(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(hours=1)
    await add_note(session, adult, guest, HELLO, seen_at=clock() - timedelta(days=1))
    await add_note(session, adult, guest, "Dinner is at eight.")
    await add_note(session, guest, adult, GOODBYE)

    assert await erase_notes_of_departed_guests(session, clock()) == 2

    # What the guest received is gone, read or not; what the guest left for others is not.
    assert await texts_of_notes(session) == {GOODBYE}
    [entry] = await erasures(session)
    assert entry.actor == "system"
    assert entry.details == {"recipient": f"person:{guest.id}", "count": 2}
    # Ids and numbers only: no word of a note is in the audit log.
    assert HELLO not in json.dumps(entry.details) and "Dinner" not in json.dumps(entry.details)


async def test_each_guest_who_left_has_an_entry_of_their_own(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    first, second = await add_person("Robin", "guest"), await add_person("Kim", "guest")
    for guest in (first, second):
        guest.expires_at = clock() - timedelta(minutes=1)
    await add_note(session, adult, first, "one")
    await add_note(session, adult, second, "two")
    await add_note(session, adult, second, "three")

    assert await erase_notes_of_departed_guests(session, clock()) == 3

    assert {
        entry.details["recipient"]: entry.details["count"] for entry in await erasures(session)
    } == {
        f"person:{first.id}": 1,
        f"person:{second.id}": 2,
    }


async def test_only_departed_guests_lose_their_notes(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    here = await add_person("Robin", "guest")
    here.expires_at = clock() + timedelta(days=1)
    removed_adult = await add_person("Sam", "adult")
    removed_adult.disabled_at = clock() - timedelta(days=1)
    removed_adult.state = "gone"
    removed_guest = await add_person("Kim", "guest")
    removed_guest.expires_at = clock() + timedelta(days=1)  # the stay was not over
    removed_guest.disabled_at = clock() - timedelta(hours=1)
    removed_guest.state = "gone"
    leaving_now = await add_person("Dana", "guest")
    leaving_now.expires_at = clock()  # a stay ends the moment it is no longer in the future
    for recipient, text in (
        (here, "for the guest who is here"),
        (removed_adult, "for the adult who was removed"),
        (removed_guest, "for the guest who was removed"),
        (leaving_now, "for the guest whose stay ends now"),
    ):
        await add_note(session, adult, recipient, text)

    assert await erase_notes_of_departed_guests(session, clock()) == 2

    assert await texts_of_notes(session) == {
        "for the guest who is here",
        "for the adult who was removed",
    }
    assert {entry.details["recipient"] for entry in await erasures(session)} == {
        f"person:{removed_guest.id}",
        f"person:{leaving_now.id}",
    }


async def test_nothing_to_erase_writes_nothing(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    here = await add_person("Robin", "guest")
    here.expires_at = clock() + timedelta(days=1)
    await add_note(session, adult, here, HELLO)

    assert await erase_notes_of_departed_guests(session, clock()) == 0

    assert await texts_of_notes(session) == {HELLO}
    assert await erasures(session) == []


# --- the notes an author keeps until a date (spec 5.10) ------------------------------------------


def the_end_of_the_day_in(clock, days: int) -> datetime:
    """The last second of the day ``days`` days after today, in UTC: what a note is kept until."""
    day = clock().astimezone(UTC).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


async def test_a_kept_note_outlives_the_guests_departure_and_is_erased_at_its_date(
    session, add_person, clock
):  # spec 8.9
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(hours=1)
    await add_note(session, adult, guest, HELLO, keep_until=the_end_of_the_day_in(clock, 30))

    # The guest has left, and the note stays: nothing is erased, nothing is written.
    assert await erase_notes_of_departed_guests(session, clock()) == 0
    assert await texts_of_notes(session) == {HELLO}
    assert await erasures(session) == []

    clock.advance(timedelta(days=31))  # past the end of that day, the guest still away
    assert await erase_notes_of_departed_guests(session, clock()) == 1
    assert await texts_of_notes(session) == set()
    [entry] = await erasures(session)
    assert entry.details == {"recipient": f"person:{guest.id}", "count": 1}
    assert HELLO not in json.dumps(entry.details)


async def test_a_kept_note_is_erased_from_the_last_second_of_its_day_and_not_before(
    session, add_person, clock
):
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.disabled_at = clock() - timedelta(hours=1)
    guest.state = "gone"
    until = the_end_of_the_day_in(clock, 1)
    await add_note(session, adult, guest, HELLO, keep_until=until)

    assert await erase_notes_of_departed_guests(session, until - timedelta(seconds=1)) == 0
    assert await erase_notes_of_departed_guests(session, until) == 1


async def test_only_the_notes_that_are_kept_outlive_the_departure(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(hours=1)
    await add_note(session, adult, guest, HELLO, keep_until=the_end_of_the_day_in(clock, 30))
    await add_note(session, adult, guest, "Dinner is at eight.")
    await add_note(session, adult, guest, "Kept, but the date is past.", keep_until=clock())

    assert await erase_notes_of_departed_guests(session, clock()) == 2

    assert await texts_of_notes(session) == {HELLO}
    [entry] = await erasures(session)
    assert entry.details == {"recipient": f"person:{guest.id}", "count": 2}


async def test_a_guest_back_before_the_date_finds_the_kept_notes(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(hours=1)
    await add_note(session, adult, guest, HELLO, keep_until=the_end_of_the_day_in(clock, 30))
    await add_note(session, adult, guest, GOODBYE, keep_until=clock() - timedelta(days=1))
    assert await erase_notes_of_departed_guests(session, clock()) == 1  # the one whose date is past
    # The guest is back (a new stay): nothing is erased, whatever the date of a note.
    clock.advance(timedelta(days=45))
    guest.expires_at = clock() + timedelta(days=5)
    guest.disabled_at = None
    guest.state = "has_access"
    assert await erase_notes_of_departed_guests(session, clock()) == 0
    assert await texts_of_notes(session) == {HELLO}


async def test_a_guest_who_leaves_early_by_themselves_leaves_the_kept_notes_too(
    session, add_person, clock
):  # household_leave closes the account as a removal does (spec 5.9): the same rule
    adult = await add_person("Alex", "adult")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() + timedelta(days=10)
    await add_note(session, adult, guest, HELLO, keep_until=the_end_of_the_day_in(clock, 20))
    await add_note(session, adult, guest, "Dinner is at eight.")
    await disable(session, guest, clock(), by=guest.id)

    assert await erase_notes_of_departed_guests(session, clock()) == 1

    assert await texts_of_notes(session) == {HELLO}
    clock.advance(timedelta(days=21))
    assert await erase_notes_of_departed_guests(session, clock()) == 1
    assert await texts_of_notes(session) == set()


async def test_a_kept_note_of_someone_who_is_not_a_guest_is_left_alone(session, add_person, clock):
    adult = await add_person("Alex", "adult")
    sam = await add_person("Sam", "adult")
    sam.disabled_at = clock() - timedelta(days=1)
    sam.state = "gone"
    await add_note(session, adult, sam, HELLO, keep_until=clock() - timedelta(days=1))

    assert await erase_notes_of_departed_guests(session, clock()) == 0
    assert await texts_of_notes(session) == {HELLO}


# --- the rights of the people who left -----------------------------------------------------------


def a_right(owner: Person, person: Person | None, action_type: str, **more) -> PolicyRight:
    """A right of the owner for ``person`` (or, without one, for the role ``more`` names)."""
    return PolicyRight(
        person_id=None if person is None else person.id,
        action_type=action_type,
        access="allow",
        summary="a right",
        owner_words="",
        created_by=owner.id,
        **more,
    )


async def paused_rights_of(session) -> dict[str, set[str]]:
    """The action types of the rights that are paused (and not revoked), by person id."""
    found = await session.execute(
        select(PolicyRight.person_id, PolicyRight.action_type).where(
            PolicyRight.paused_at.is_not(None), PolicyRight.revoked_at.is_(None)
        )
    )
    paused: dict[str, set[str]] = {}
    for person_id, action_type in found.all():
        paused.setdefault(str(person_id), set()).add(action_type)
    return paused


async def pauses(session) -> list[AuditEntry]:
    found = await session.execute(
        select(AuditEntry).where(AuditEntry.action == "rights.paused").order_by(AuditEntry.id)
    )
    return list(found.scalars())


async def test_the_rights_of_a_guest_whose_stay_ended_are_paused_by_the_upkeep(
    session, add_person, clock
):  # F31, spec 5.7
    owner = await add_person("Owner", "owner")
    guest = await add_person("Robin", "guest")
    guest.expires_at = clock() - timedelta(hours=1)
    session.add_all(
        [
            a_right(owner, guest, "household.invite"),
            a_right(owner, guest, "note.leave"),
            a_right(owner, None, "household.invite", role="guest"),  # the role's, not theirs
        ]
    )
    await session.flush()

    assert await pause_rights_of_those_who_left(session, clock()) == 2

    assert await paused_rights_of(session) == {str(guest.id): {"household.invite", "note.leave"}}
    [entry] = await pauses(session)
    # By the system, with an identifier and a number, like the pause of a removal or a return.
    assert entry.actor == "system"
    assert entry.details == {"person_id": str(guest.id), "count": 2}
    # Once: a second pass has nothing left to pause and writes nothing more.
    assert await pause_rights_of_those_who_left(session, clock()) == 0
    assert len(await pauses(session)) == 1


async def test_only_the_rights_of_the_people_who_left_are_paused(session, add_person, clock):
    owner = await add_person("Owner", "owner")
    here = await add_person("Robin", "guest")
    here.expires_at = clock() + timedelta(days=1)
    leaving_now = await add_person("Dana", "guest")
    leaving_now.expires_at = clock()  # a stay ends the moment it is no longer in the future
    adult = await add_person("Alex", "adult")
    for person in (owner, here, leaving_now, adult):
        session.add(a_right(owner, person, "household.invite"))
    session.add(
        a_right(
            owner,
            leaving_now,
            "note.leave",
            revoked_at=clock() - timedelta(days=2),  # taken away already: it stays so
        )
    )
    await session.flush()

    assert await pause_rights_of_those_who_left(session, clock()) == 1

    assert await paused_rights_of(session) == {str(leaving_now.id): {"household.invite"}}
    [entry] = await pauses(session)
    assert entry.details == {"person_id": str(leaving_now.id), "count": 1}


async def test_each_person_who_left_has_a_pause_of_their_own_in_the_audit(
    session, add_person, clock
):
    owner = await add_person("Owner", "owner")
    first, second = await add_person("Robin", "guest"), await add_person("Kim", "guest")
    for guest in (first, second):
        guest.expires_at = clock() - timedelta(minutes=1)
        session.add(a_right(owner, guest, "household.invite"))
    session.add(a_right(owner, second, "note.leave"))
    await session.flush()

    assert await pause_rights_of_those_who_left(session, clock()) == 3

    assert {
        entry.details["person_id"]: entry.details["count"] for entry in await pauses(session)
    } == {
        str(first.id): 1,
        str(second.id): 2,
    }


async def test_the_rights_of_someone_removed_before_removals_paused_them_are_paused_too(
    session, add_person, clock
):  # spec 5.7: when someone leaves the household, their personal rights are paused
    owner = await add_person("Owner", "owner")
    removed_adult = await add_person("Sam", "adult")
    removed_adult.disabled_at = clock() - timedelta(days=30)
    removed_adult.state = "gone"
    removed_guest = await add_person("Kim", "guest")
    removed_guest.expires_at = clock() + timedelta(days=1)  # the stay was not over
    removed_guest.disabled_at = clock() - timedelta(hours=3)
    removed_guest.state = "gone"
    for person in (removed_adult, removed_guest):
        session.add(a_right(owner, person, "household.invite"))
    await session.flush()

    assert await pause_rights_of_those_who_left(session, clock()) == 2

    assert await paused_rights_of(session) == {
        str(removed_adult.id): {"household.invite"},
        str(removed_guest.id): {"household.invite"},
    }
    assert {entry.actor for entry in await pauses(session)} == {"system"}
    # Once: nothing is left in force for the next pass.
    assert await pause_rights_of_those_who_left(session, clock()) == 0
    assert len(await pauses(session)) == 2


async def test_a_removal_that_paused_the_rights_leaves_nothing_for_the_pass(
    session, add_person, clock
):
    owner = await add_person("Owner", "owner")
    sam = await add_person("Sam", "adult")
    session.add(a_right(owner, sam, "household.invite"))
    await session.flush()
    removal = await disable(session, sam, clock(), by=owner.id)
    assert removal.rights_paused == 1
    assert await pause_rights_of_those_who_left(session, clock()) == 0
    [entry] = await pauses(session)
    # The one who removed them paused them; the pass wrote nothing more.
    assert entry.actor == f"person:{owner.id}"


async def test_the_departures_pass_pauses_and_commits_in_a_session_of_its_own(
    session_factory, clock
):
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        guest = Person(display_name="Robin", role="guest", expires_at=clock() - timedelta(hours=1))
        writer.add_all([owner, guest])
        await writer.flush()
        writer.add(a_right(owner, guest, "household.invite"))
        await writer.commit()
        guest_id = guest.id

    await departures_pass(session_factory)(clock())

    async with session_factory() as reader:
        assert await paused_rights_of(reader) == {str(guest_id): {"household.invite"}}
        assert len(await pauses(reader)) == 1
    # The next round finds it done.
    await departures_pass(session_factory)(clock())
    async with session_factory() as reader:
        assert len(await pauses(reader)) == 1


# --- the loop ------------------------------------------------------------------------------------


class FailsOnce:
    """A session factory whose first opening fails (the database is away for a moment); the
    others open sessions of the real one."""

    def __init__(self, real) -> None:
        self.real = real
        self.openings = 0

    def __call__(self):
        self.openings += 1
        if self.openings == 1:
            raise ConnectionError("the database is away")
        return self.real()


async def a_guest_who_left_with_a_note(session_factory, clock) -> None:
    """Committed: an adult, a guest whose stay ended an hour ago, and a note for them."""
    async with session_factory() as writer:
        adult, guest = (
            Person(display_name="Alex", role="adult"),
            Person(display_name="Robin", role="guest", expires_at=clock() - timedelta(hours=1)),
        )
        writer.add_all([adult, guest])
        await writer.flush()
        writer.add(Note(author_id=adult.id, recipient_id=guest.id, text=HELLO))
        await writer.commit()


async def test_the_upkeep_goes_on_after_a_failed_pass(session_factory, clock, count_notes, caplog):
    await a_guest_who_left_with_a_note(session_factory, clock)
    flaky = FailsOnce(session_factory)
    with anyio.move_on_after(30) as bound:
        async with anyio.create_task_group() as group:
            group.start_soon(partial(run_upkeep, [notes_pass(flaky)], clock, every=0.01))
            while await count_notes() > 0:
                await anyio.sleep(0.01)
            group.cancel_scope.cancel()  # the loop never ends by itself: it is cancelled
    assert not bound.cancelled_caught, "the second pass never came"
    assert flaky.openings >= 2
    # The failed pass is in the log by the kind of error alone, as everywhere in the core: no
    # traceback, and none of the words of the error.
    [failure] = [
        record
        for record in caplog.records
        if record.name == "domo_core.upkeep" and record.levelno == logging.ERROR
    ]
    assert "ConnectionError" in failure.getMessage()
    assert "the database is away" not in failure.getMessage()
    assert failure.exc_info is None


async def test_the_first_pass_is_made_at_once(session_factory, clock, count_notes):
    await a_guest_who_left_with_a_note(session_factory, clock)
    with anyio.move_on_after(30) as bound:
        async with anyio.create_task_group() as group:
            # A pause of an hour: whatever is erased, the first pass erased it.
            group.start_soon(partial(run_upkeep, [notes_pass(session_factory)], clock, every=3600))
            while await count_notes() > 0:
                await anyio.sleep(0.01)
            group.cancel_scope.cancel()
    assert not bound.cancelled_caught


def test_the_upkeep_runs_every_minute():
    assert UPKEEP_SECONDS == 60.0


async def test_a_pass_that_fails_does_not_stop_the_others(
    session_factory, clock, count_notes, caplog
):
    await a_guest_who_left_with_a_note(session_factory, clock)

    async def broken(now) -> None:
        raise ConnectionError("the vault is away")

    with anyio.move_on_after(30) as bound:
        async with anyio.create_task_group() as group:
            # The broken pass comes first, and fails every time: the notes are erased all the same.
            group.start_soon(
                partial(run_upkeep, [broken, notes_pass(session_factory)], clock, every=3600)
            )
            while await count_notes() > 0:
                await anyio.sleep(0.01)
            group.cancel_scope.cancel()
    assert not bound.cancelled_caught
    [failure] = [
        record
        for record in caplog.records
        if record.name == "domo_core.upkeep" and record.levelno == logging.ERROR
    ]
    assert "ConnectionError" in failure.getMessage()
    assert "the vault is away" not in failure.getMessage()


# --- with the application ------------------------------------------------------------------------


async def test_the_upkeep_of_the_app_also_pauses_the_rights_of_guests_who_left(
    settings, session_factory, clock
):
    async with session_factory() as writer:
        owner = Person(display_name="Owner", role="owner")
        guest = Person(display_name="Robin", role="guest", expires_at=clock() - timedelta(hours=1))
        writer.add_all([owner, guest])
        await writer.flush()
        writer.add(a_right(owner, guest, "household.invite"))
        await writer.commit()
        guest_id = guest.id
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: clock
    async with app.router.lifespan_context(app):
        with anyio.fail_after(30):
            while True:
                async with session_factory() as reader:
                    if await paused_rights_of(reader):
                        break
                await anyio.sleep(0.01)
    async with session_factory() as reader:
        assert await paused_rights_of(reader) == {str(guest_id): {"household.invite"}}


async def test_the_upkeep_runs_with_the_app(settings, session_factory, clock, count_notes):
    # The stay ended on the application's clock (2030), not on the real one (the stay is still
    # to come for it): the upkeep reads the time the way the pages do.
    await a_guest_who_left_with_a_note(session_factory, clock)
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: clock
    before = asyncio.all_tasks()
    async with app.router.lifespan_context(app):
        with anyio.fail_after(30):
            while await count_notes() > 0:
                await anyio.sleep(0.01)
        # The task of the upkeep itself, not just any task the application started.
        running = {
            task
            for task in asyncio.all_tasks() - before
            if "run_upkeep" in getattr(task.get_coro(), "__qualname__", "")
        }
        assert running, "the upkeep is a task of the application"
    # Stopped with the application: nothing is left alive.
    assert not {task for task in asyncio.all_tasks() - before if not task.done()}
    assert all(task.done() for task in running)
