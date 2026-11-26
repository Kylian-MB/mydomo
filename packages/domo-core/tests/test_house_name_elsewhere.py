"""The places that write the assistant's name outside the pages and the prompt: the API channel,
the notes he leaves, the requests that wait for a yes, the passkey prompt, the installed app's
manifest, the journal and the examples of a draft of rights. Each reads the person's own name for
him (``calls_domo``), else the house's (``house.domo_name``), else "Domo"; the passkey prompt and
the manifest, which have no reader, the house's else "Domo".
"""

import json
import uuid
from datetime import timedelta

from api_support import API, ApiPilot, pairing_code, plain_client
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.accounts.passkeys import RP_NAME, registration_options
from domo_core.audit.log import append_audit
from domo_core.chat.models import Conversation
from domo_core.confirmations.describe import example_situation
from domo_core.confirmations.routes import card_now
from domo_core.confirmations.store import awaiting_approval, create_confirmation
from domo_core.policy.actions import DescribedAction
from domo_core.policy.asks import Asked
from domo_core.policy.models import RightsDraft
from domo_core.policy.rights_drafts import Draft, Example, draft_as_json
from domo_core.policy.roles import Actor
from domo_core.policy.settings import load_policy_settings, update_policy_setting
from domo_core.texts.view import REFERENCE
from domo_core.tools.notes import notes_to_tell
from domo_core.tools.registry import TOOLS
from sqlalchemy import update
from test_audit_page import lines_of, plain
from test_notes import call_context
from test_notes_of_domo import alone, leave
from test_passkeys import NOW
from test_rights_writer import context as writer_context
from test_rights_writer import domo_change, domo_leaves_a_note
from web_support import ADULT_PASSWORD, ADULT_USERNAME, in_words, log_in

TRICKY = 'Max "&" <b>'


async def calls_him(session_factory, person: Person, name: str | None) -> None:
    async with session_factory() as session:
        await session.execute(update(Person).where(Person.id == person.id).values(calls_domo=name))
        await session.commit()


# --- the API channel ---------------------------------------------------------------------------


async def test_pairing_and_since_send_the_name_he_has_for_the_person(
    chat_server, adult_member, set_setting
):
    async def paired_name() -> str | None:
        code = await pairing_code(chat_server, adult_member)
        async with plain_client(chat_server) as client:
            paired = await client.post(f"{API}/pair", json={"code": code, "name": "Laptop"})
            assert paired.status_code == 200, paired.text
            return paired.json()["domo_name"]

    async with ApiPilot.paired(chat_server, adult_member) as device:

        async def since_name() -> str | None:
            response = await device.client.get(f"{API}/since")
            assert response.status_code == 200, response.text
            return response.json()["domo_name"]

        # Nobody gave him a name: none is sent, the program writes nothing before his words.
        assert (await paired_name(), await since_name()) == (None, None)
        await set_setting("house.domo_name", "Friday")
        assert (await paired_name(), await since_name()) == ("Friday", "Friday")
        await calls_him(chat_server.session_factory, adult_member.person, "Brenda")
        assert (await paired_name(), await since_name()) == ("Brenda", "Brenda")
        await calls_him(chat_server.session_factory, adult_member.person, None)
        assert (await paired_name(), await since_name()) == ("Friday", "Friday")


# --- the notes he leaves -----------------------------------------------------------------------


async def test_a_note_of_his_names_him_as_the_reader_does(
    add_member, session_factory, live_clock, set_setting
):
    owner = await add_member("Alex", "owner")
    dana = await add_member("Dana", "adult")
    await leave(alone(call_context(owner, session_factory, live_clock)), "Dana", "Bins tonight.")

    async def author_in_the_list() -> str:
        async with session_factory() as session:
            [note] = await notes_to_tell(session, dana.id)
        return note.author

    async def author_when_shown() -> tuple[str, str]:
        async with session_factory() as session:
            [note] = await notes_to_tell(session, dana.id)
        ctx = call_context(dana, session_factory, live_clock)
        shown = await TOOLS["note_show"].execute({"note": note.id}, ctx)
        return note.author, shown.model_result["from"]

    assert await author_in_the_list() == "Domo"
    await set_setting("house.domo_name", "Friday")
    assert await author_when_shown() == ("Friday", "Friday")
    await calls_him(session_factory, dana, "Brenda")
    dana = await _fresh(session_factory, dana)
    assert await author_when_shown() == ("Brenda", "Brenda")
    # Another reader, without a name of his own, still reads the house's.
    await calls_him(session_factory, dana, None)
    dana = await _fresh(session_factory, dana)
    assert await author_when_shown() == ("Friday", "Friday")


async def _fresh(session_factory, person: Person) -> Person:
    async with session_factory() as session:
        return await session.get(Person, person.id)


# --- the requests that wait for a yes ----------------------------------------------------------


async def test_a_request_he_made_names_him_as_the_approver_reads_it(
    session, add_person, add_conversation, clock
):
    adult = await add_person("Alex", "adult")
    child = await add_person("Sam", "child")
    conversation = await add_conversation(child)
    await create_confirmation(
        session,
        requester=Actor(child.id, "child"),
        tool_name="profile_rename",
        arguments={"name": "Sam B"},
        action=DescribedAction(
            "core", "profile.rename", f"person:{child.id}", "household", "household"
        ),
        approver="adult",
        asked=Asked("yes", "judge"),
        conversation_id=conversation.id,
        message_id=None,
        now=clock(),
        by_domo=True,
    )
    later = clock() + timedelta(minutes=1)

    async def requester_shown() -> str:
        settings = await load_policy_settings(session)
        [(_, name)] = (await awaiting_approval(session, adult, settings, later, limit=20))[0]
        return name

    assert await requester_shown() == "Domo"
    await update_policy_setting(
        session, key="house.domo_name", value="Friday", by=adult.id, now=clock()
    )
    assert await requester_shown() == "Friday"
    adult.calls_domo = "Brenda"
    await session.flush()
    assert await requester_shown() == "Brenda"


# --- the passkey prompt ------------------------------------------------------------------------


async def test_the_passkey_prompt_names_the_house_never_the_person(session, adult_account):
    # Without a name for the house: the default.
    assert (await registration_options(session, adult_account, "localhost", NOW))["rp"][
        "name"
    ] == RP_NAME
    opts = await registration_options(session, adult_account, "localhost", NOW, rp_name="Friday")
    assert opts["rp"]["name"] == "Friday"


async def test_the_passkey_route_gives_the_house_name(
    client_by_name, adult_account, session, clock
):
    await log_in(client_by_name, ADULT_USERNAME, ADULT_PASSWORD)

    async def prompt() -> str:
        r = await client_by_name.post("/account/passkeys/options", data={"current": ADULT_PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()["rp"]["name"]

    assert await prompt() == "Domo"
    await update_policy_setting(
        session, key="house.domo_name", value="Friday", by=adult_account.person_id, now=clock()
    )
    # Never the person's own name for him.
    await session.execute(
        update(Person).where(Person.id == adult_account.person_id).values(calls_domo="Brenda")
    )
    assert await prompt() == "Friday"


# --- the installed app -------------------------------------------------------------------------


async def test_the_manifest_carries_the_house_name(client, owner_account, session, clock):
    manifest = (await client.get("/manifest.webmanifest")).json()
    assert (manifest["name"], manifest["short_name"]) == ("Domo", "Domo")
    await update_policy_setting(
        session, key="house.domo_name", value="Friday", by=owner_account.person_id, now=clock()
    )
    manifest = (await client.get("/manifest.webmanifest")).json()
    assert (manifest["name"], manifest["short_name"]) == ("Friday", "Friday")


async def test_manifest_carries_the_house_name_whatever_it_holds(
    client, owner_account, session, clock
):
    await update_policy_setting(
        session, key="house.domo_name", value=TRICKY, by=owner_account.person_id, now=clock()
    )
    response = await client.get("/manifest.webmanifest")
    manifest = json.loads(response.text)
    assert (manifest["name"], manifest["short_name"]) == (TRICKY, TRICKY)


# --- the journal -------------------------------------------------------------------------------


async def test_the_journal_names_him_as_the_owner_reads_it(client, owner_account, session, clock):
    await log_in(client)
    entry = await append_audit(
        session, actor="system", action="catalog.refreshed", details={"models": 42}
    )

    async def line_with(name: str) -> bool:
        said = t(
            "audit.line.catalog.refreshed",
            "en",
            who=name,
            when=in_words(entry.ts),
            count="42",
        )
        return lines_of((await client.get("/audit")).text)[entry.id] == plain(said)

    assert await line_with("Domo")
    await update_policy_setting(
        session, key="house.domo_name", value="Friday", by=owner_account.person_id, now=clock()
    )
    assert await line_with("Friday")
    await session.execute(
        update(Person).where(Person.id == owner_account.person_id).values(calls_domo="Brenda")
    )
    assert await line_with("Brenda")


async def test_the_journal_escapes_the_house_name(client, owner_account, session, clock):
    await log_in(client)
    await append_audit(session, actor="system", action="catalog.refreshed", details={"models": 4})
    # Written straight into the settings row: the tool would accept this name too.
    await update_policy_setting(
        session, key="house.domo_name", value=TRICKY, by=owner_account.person_id, now=clock()
    )
    page = (await client.get("/audit")).text
    assert "Max &#34;&amp;&#34; &lt;b&gt;" in page
    assert "<b>" not in page


# --- the examples of a draft of rights ---------------------------------------------------------


def test_an_example_about_him_says_the_name_the_reader_sees():
    example = Example(
        person_id=None, action_type="note.leave", facts={}, expected="refused", for_domo=True
    )
    names: dict[str, str] = {str(uuid.uuid4()): "Alex"}
    default = example_situation(example, names, REFERENCE)
    assert default.startswith("Domo")
    assert example_situation(example, names, REFERENCE, domo="Friday").startswith("Friday")
    assert example_situation(example, names, REFERENCE, domo="Brenda").startswith("Brenda")


async def test_the_card_of_a_draft_names_him_as_its_reader_does(session_factory, clock):
    async with session_factory() as writer:
        alex = Person(display_name="Alex", role="owner", calls_domo="Brenda")
        writer.add(alex)
        await writer.flush()
        conversation = Conversation(person_id=alex.id)
        draft = RightsDraft(
            created_by=alex.id,
            owner_words="No more notes of your own",
            content=draft_as_json(
                Draft((domo_change(),), (domo_leaves_a_note("refused"),)), writer_context()
            ),
        )
        writer.add_all([conversation, draft])
        await writer.flush()
        request = await create_confirmation(
            writer,
            requester=Actor(alex.id, "owner"),
            tool_name="rights_apply",
            arguments={"draft": str(draft.id)},
            action=DescribedAction(
                "core", "rights.apply", f"draft:{draft.id}", "permissions", "household"
            ),
            approver="self",
            asked=Asked("yes", "judge"),
            conversation_id=conversation.id,
            message_id=None,
            now=clock(),
        )
        await update_policy_setting(
            writer, key="house.domo_name", value="Friday", by=alex.id, now=clock()
        )
        await writer.commit()

    async def situation() -> str:
        reader = await _fresh(session_factory, alex)
        card = await card_now(
            session_factory, request.id, viewer=reader, texts=REFERENCE, now=clock()
        )
        assert card is not None and card.draft is not None
        return card.draft.examples[0].situation

    # Their own name for him, else the house's, else the default.
    assert (await situation()).startswith("Brenda")
    await calls_him(session_factory, alex, None)
    assert (await situation()).startswith("Friday")
    async with session_factory() as writer:
        await update_policy_setting(
            writer, key="house.domo_name", value=None, by=alex.id, now=clock()
        )
        await writer.commit()
    assert (await situation()).startswith("Domo")
