"""The owner's household page (``GET /household``): every record, with an account or not, its
role and what a made role changes, where it stands, the promises that wait and the owner's
directives in force; read only, and never a link, a reason or a memory."""

import html
import json
import re
from datetime import UTC, datetime, timedelta
from importlib import resources

from domo_common.i18n import t
from domo_common.meanings import holes
from domo_core.accounts.models import Person
from domo_core.household import routes
from domo_core.household.models import Invitation
from domo_core.household.routes import label_key
from domo_core.memory.models import Memory
from domo_core.people.models import (
    AccessPromise,
    MadeRole,
    OwnerDirective,
    PersonAct,
    RoleAccess,
)
from domo_core.policy.catalog import KNOWN_ACTIONS, OpenKind
from domo_core.policy.open_catalog import add_kind
from domo_core.texts.view import CatalogTexts
from sqlalchemy import select
from web_support import in_words

NEW_KEYS = (
    "household.state.known",
    "household.state.asleep",
    "household.state.withdrawn",
    "household.state.refused",
    "household.standing.lives_here",
    "household.standing.visiting",
    "household.standing.elsewhere",
    "household.child",
    "household.no_account",
    "household.opened_by_domo",
    "household.role.made",
    "household.role.adds",
    "household.role.removes",
    "household.promises",
    "household.promises.none",
    "household.promises.asked_by",
    "household.promises.asked_by_domo",
    "household.promises.asked_by_someone_gone",
    "household.directives",
    "household.directives.none",
    "household.directives.kind.no_new_people",
    "household.directives.kind.no_keeping",
    "household.directives.kind.withdraw",
    "household.directives.kind.refuse",
    "household.directives.except",
    "household.directives.until",
    "household.by_domo",
)


def plain(page: str) -> str:
    """What a person reads in a page."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def content_of(page: str) -> str:
    """The page's own part, without the frame every page shares (its menu, its logout form)."""
    found = re.search(r'<section class="card">.*</section>', page, re.S)
    assert found is not None
    return found.group(0)


def access_items(row: str, which: str) -> list[str]:
    """The types a made role adds or removes, as the row of its person lists them."""
    found = re.search(rf'<ul class="household-access" data-access="{which}">(.*?)</ul>', row, re.S)
    assert found is not None, which
    return sorted(plain(item) for item in re.findall(r"<li>(.*?)</li>", found.group(1), re.S))


def row_of(page: str, person: Person) -> str:
    """The block of ``person`` on the page (it may hold lists of its own: up to the next person,
    or the end of the list of people)."""
    found = re.search(
        rf'<li class="household-person" data-person-id="{person.id}".*?'
        r'(?=<li class="household-person"|\n      </ul>)',
        page,
        re.S,
    )
    assert found is not None, person.display_name
    return found.group(0)


def now() -> datetime:
    """The time the page reads (its clock is the real one: a session is dated by it too)."""
    return datetime.now(UTC)


async def owner_of(session) -> Person:
    return (await session.execute(select(Person).where(Person.role == "owner"))).scalar_one()


async def test_the_page_shows_records_with_and_without_an_account(
    owner_client, session, add_person, add_account
):
    dana = await add_person("Dana", "adult")
    await add_account(dana, "dana")
    paul = await add_person(
        "Paul", "child", state="known", origin="domo", origin_reason="heard at dinner"
    )
    cook = MadeRole(name="Cook", name_key="cook", base="guest", created_by="domo", reason="cooks")
    session.add(cook)
    await session.flush()
    session.add_all(
        [
            RoleAccess(role_id=cook.id, action_type="plan.set", access="allow"),
            RoleAccess(role_id=cook.id, action_type="made.recipes", access="allow"),
            RoleAccess(role_id=cook.id, action_type="plan.cancel", access="deny"),
        ]
    )
    await add_kind(
        session,
        OpenKind(
            action_type="made.recipes",
            costs=False,
            irreversible=False,
            physical=False,
            outward=False,
            facts={},
            default_roles=frozenset(),
            description="Find a recipe in the house's notebook.",
        ),
        now=now(),
    )
    sam = await add_person(
        "Sam",
        "guest",
        standing="visiting",
        made_role_id=cook.id,
        expires_at=now() - timedelta(days=2),
        first_access_at=now() - timedelta(days=9),
    )
    tom = await add_person("Tom", "adult", state="withdrawn", disabled_at=now())
    await session.flush()

    page = (await owner_client.get("/household")).text
    words = plain(page)
    for name in ("Owner", "Dana", "Paul", "Sam", "Tom"):
        assert name in words
    states = dict(re.findall(r'data-person-id="([^"]+)"[^>]*data-person-state="([a-z_]+)"', page))
    assert states[str(dana.id)] == "has_access"
    assert states[str(paul.id)] == "known"
    assert states[str(sam.id)] == "asleep"
    assert states[str(tom.id)] == "withdrawn"

    paul_row = plain(row_of(page, paul))
    assert t("household.no_account", "en") in paul_row
    assert t("household.state.known", "en") in paul_row
    assert t("household.opened_by_domo", "en") in paul_row
    assert t("household.role.child", "en") in paul_row
    assert t("household.no_account", "en") not in plain(row_of(page, dana))
    assert t("household.opened_by_domo", "en") not in plain(row_of(page, dana))

    sam_row = plain(row_of(page, sam))
    assert t("household.standing.visiting", "en") in sam_row
    assert t("household.role.made", "en", name="Cook", base=t("household.role.guest", "en")) in (
        sam_row
    )
    # Each type by its own label, or by the description of an open kind.
    adds = access_items(row_of(page, sam), "adds")
    assert adds == sorted(
        [t("household.right.plan.set", "en"), "Find a recipe in the house's notebook."]
    )
    assert access_items(row_of(page, sam), "removes") == [t("household.right.plan.cancel", "en")]
    assert t("household.role.adds", "en") in sam_row
    assert t("household.role.removes", "en") in sam_row
    assert "plan.set" not in page and "made.recipes" not in page
    assert t("household.state.asleep", "en", date=in_words(sam.expires_at)) in sam_row
    assert t("household.state.withdrawn", "en", date=in_words(now())) in plain(row_of(page, tom))


async def test_a_record_that_left_is_gone_and_says_when(owner_client, add_person):
    left = now() - timedelta(days=3)
    sam = await add_person("Sam", "guest", state="gone", disabled_at=left)
    page = (await owner_client.get("/household")).text
    assert re.search(rf'data-person-id="{sam.id}"[^>]*data-person-state="gone"', page)
    assert t("household.state.removed", "en", date=in_words(left)) in plain(row_of(page, sam))


async def test_a_child_mark_on_another_role_is_shown(owner_client, add_person):
    sam = await add_person("Sam", "guest", is_child=True)
    page = (await owner_client.get("/household")).text
    assert t("household.child", "en") in plain(row_of(page, sam))


async def test_the_page_shows_promises_and_directives(owner_client, session, add_person):
    owner = await owner_of(session)
    dana = await add_person("Dana", "adult")
    tom = await add_person(
        "Tom", "adult", state="refused", origin="domo", origin_reason="a stranger"
    )
    until = now() + timedelta(days=7)
    session.add_all(
        [
            AccessPromise(
                description="Dana's friend from work",
                role="guest",
                standing="visiting",
                asked_by=f"person:{dana.id}",
                is_order=False,
                reason="promised in the kitchen",
            ),
            AccessPromise(
                description="the neighbour who waters the plants",
                role="adult",
                standing="elsewhere",
                asked_by="domo",
                is_order=False,
                reason="waters the plants",
            ),
            OwnerDirective(
                kind="no_new_people",
                description="nobody new this week",
                except_words="unless Dana says so",
                until=until,
                set_by=owner.id,
            ),
            OwnerDirective(kind="refuse", person_id=tom.id, set_by=owner.id),
        ]
    )
    await session.flush()

    page = (await owner_client.get("/household")).text
    words = plain(page)
    assert t("household.state.refused", "en") in plain(row_of(page, tom))
    assert "Dana's friend from work" in words
    assert t("household.promises.asked_by", "en", name="Dana") in words
    assert "the neighbour who waters the plants" in words
    assert t("household.promises.asked_by_domo", "en") in words
    assert t("household.directives.kind.no_new_people", "en") in words
    assert "nobody new this week" in words
    assert t("household.directives.except", "en", words="unless Dana says so") in words
    assert t("household.directives.until", "en", date=in_words(until)) in words
    refuse = re.search(
        r'<li class="household-directive" data-directive-kind="refuse">.*?</li>', page, re.S
    )
    assert refuse is not None
    assert "Tom" in plain(refuse.group(0))


async def test_a_lifted_or_ended_directive_and_a_kept_promise_are_not_shown(
    owner_client, session, add_person
):
    owner = await owner_of(session)
    session.add_all(
        [
            OwnerDirective(
                kind="no_new_people",
                description="the old rule",
                set_by=owner.id,
                lifted_at=now(),
                lifted_by=owner.id,
            ),
            OwnerDirective(
                kind="no_keeping",
                description="the ended rule",
                until=now() - timedelta(minutes=1),
                set_by=owner.id,
            ),
            AccessPromise(
                description="the promise kept already",
                role="guest",
                standing="visiting",
                asked_by="domo",
                is_order=False,
                reason="kept",
                kept_at=now(),
            ),
        ]
    )
    await session.flush()
    page = content_of((await owner_client.get("/household")).text)
    words = plain(page)
    for gone in ("the old rule", "the ended rule", "the promise kept already"):
        assert gone not in words
    assert t("household.directives.none", "en") in words
    assert t("household.promises.none", "en") in words


async def test_the_page_never_shows_a_link_a_reason_or_a_memory(owner_client, session, add_person):
    owner = await owner_of(session)
    sam = await add_person("Sam", "guest", origin="domo", origin_reason="secret-origin-reason")
    session.add_all(
        [
            PersonAct(act="opened", person_id=sam.id, reason="secret-act-reason", by="domo"),
            Memory(
                subject="Sam",
                content="secret-memory-content",
                kind="fact",
                origin="others_words",
                about_id=sam.id,
                said_by_id=owner.id,
            ),
            Invitation(
                display_name="Tom",
                role="guest",
                guest_days=3,
                created_by=None,
                by_domo=True,
                reason="secret-invitation-reason",
                token_hash="0" * 64,
                expires_at=now() + timedelta(days=2),
            ),
        ]
    )
    await session.flush()
    page = (await owner_client.get("/household")).text
    for secret in (
        "secret-origin-reason",
        "secret-act-reason",
        "secret-memory-content",
        "secret-invitation-reason",
        "0" * 64,
        "/invite/",
    ):
        assert secret not in page
    invitation = re.search(r'<li class="household-invitation".*?</li>', page, re.S)
    assert invitation is not None
    assert "Tom" in plain(invitation.group(0))
    assert t("household.by_domo", "en") in plain(invitation.group(0))


async def test_a_promise_asked_by_someone_since_forgotten_is_not_the_assistant_s(
    owner_client, session, add_person
):
    sam = await add_person("Sam", "guest", state="gone", disabled_at=now())
    session.add(
        AccessPromise(
            description="the friend of a friend",
            role="guest",
            standing="visiting",
            asked_by=f"person:{sam.id}",
            is_order=False,
            reason="asked long ago",
        )
    )
    await session.flush()
    sam.forgotten_at = now()
    sam.display_name = ""
    await session.flush()
    page = content_of((await owner_client.get("/household")).text)
    promise = re.search(r'<li class="household-promise">.*?</li>', page, re.S)
    assert promise is not None
    words = plain(promise.group(0))
    assert t("household.promises.asked_by_someone_gone", "en") in words
    assert t("household.promises.asked_by_domo", "en") not in words


async def test_words_people_wrote_are_escaped(owner_client, session):
    owner = await owner_of(session)
    trap = "<img src=x onerror=alert(1)>"
    session.add_all(
        [
            AccessPromise(
                description=trap,
                role="guest",
                standing="visiting",
                asked_by="domo",
                is_order=False,
                reason="a test",
            ),
            OwnerDirective(
                kind="no_new_people", description=trap, except_words=trap, set_by=owner.id
            ),
        ]
    )
    await session.flush()
    page = (await owner_client.get("/household")).text
    assert "<img src=x" not in page
    assert page.count("&lt;img src=x onerror=alert(1)&gt;") == 3


async def test_the_page_changes_nothing(owner_client, add_person):
    await add_person("Sam", "guest", state="known", origin="domo", origin_reason="seen")
    page = content_of((await owner_client.get("/household")).text)
    for control in ("<form", "<button", "hx-post", "hx-delete", "<input"):
        assert control not in page


async def test_a_merged_or_forgotten_record_is_not_listed(owner_client, add_person):
    dana = await add_person("Dana", "adult")
    merged = await add_person("Paul", "child", state="known", merged_into_id=dana.id)
    forgotten = await add_person("Sam", "guest", state="gone", disabled_at=now())
    forgotten.forgotten_at = now()
    page = (await owner_client.get("/household")).text
    assert f'data-person-id="{dana.id}"' in page
    assert f'data-person-id="{merged.id}"' not in page
    assert f'data-person-id="{forgotten.id}"' not in page


async def test_only_the_owner_opens_it(adult_client):
    assert (await adult_client.get("/household")).status_code == 403


def test_the_new_keys_exist_in_both_languages():
    locales = resources.files("domo_common").joinpath("locales")
    for language in ("en", "fr"):
        catalog = json.loads(locales.joinpath(f"{language}.json").read_text(encoding="utf-8"))
        missing = [key for key in NEW_KEYS if key not in catalog]
        assert not missing, (language, missing)


def test_every_action_type_of_the_catalog_has_a_plain_label_in_both_languages():
    """A type added later without a label fails here: the owner's page never shows a type's
    name."""
    locales = resources.files("domo_common").joinpath("locales")
    catalogs = {
        language: json.loads(locales.joinpath(f"{language}.json").read_text(encoding="utf-8"))
        for language in ("en", "fr")
    }
    # Said of someone else's role: never a word for the reader.
    for_the_reader = {
        "en": re.compile(r"\b(you|your)\b", re.I),
        "fr": re.compile(r"(?<![\w'’])(tu|ta|ton|tes|te)(?![\w'’])", re.I),
    }
    unlabelled = []
    for action_type in sorted(KNOWN_ACTIONS):
        key = f"household.right.{action_type}"
        if label_key(action_type) != key or holes(key):
            unlabelled.append(action_type)
            continue
        for language, catalog in catalogs.items():
            label = catalog.get(key, "")
            if (
                not label.endswith(".")
                or "{" in label
                or action_type in label
                or for_the_reader[language].search(label)
            ):
                unlabelled.append(f"{action_type} ({language}): {label!r}")
    assert not unlabelled


async def test_a_made_role_s_rights_are_said_in_words(owner_client, session, add_person):
    helper = MadeRole(
        name="Helper", name_key="helper", base="adult", created_by="domo", reason="helps"
    )
    session.add(helper)
    await session.flush()
    session.add_all(
        [
            RoleAccess(role_id=helper.id, action_type="household.invite", access="allow"),
            RoleAccess(role_id=helper.id, action_type="people.look", access="deny"),
        ]
    )
    dana = await add_person("Dana", "adult", made_role_id=helper.id)
    page = (await owner_client.get("/household")).text
    row = row_of(page, dana)
    assert access_items(row, "adds") == [t("household.right.household.invite", "en")]
    assert access_items(row, "removes") == [t("household.right.people.look", "en")]
    assert "household.invite" not in page and "people.look" not in page


async def test_an_asker_label_that_names_nobody_does_not_break_the_page(session):
    for label in ("person:not-an-id", "someone", ""):
        said = await routes._asker(session, label, CatalogTexts("en"))
        assert said == t("household.promises.asked_by_someone_gone", "en")
