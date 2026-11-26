"""The pages show the name the reader sees for the assistant: their own (``calls_domo``), else the
house's (``house.domo_name``), else "Domo". A visitor reads the house's name; the pages of the
installation say "Domo". An open chat page follows a change of the house's name as it follows a
person's own (the ``frame`` event).
"""

import json
import re
from datetime import UTC, datetime

from chat_support import live_http_client
from domo_common.i18n import t
from domo_core.accounts.models import Person
from domo_core.policy.settings import update_policy_setting
from sqlalchemy import update


def brand_and_title(page: str) -> tuple[str, str]:
    brand = re.search(r'<a class="brand" href="/">([^<]*)</a>', page)
    title = re.search(r"<title>[^<]* - ([^<]*)</title>", page)
    assert brand is not None and title is not None, page
    return brand.group(1), title.group(1)


def placeholder_of(page: str) -> str:
    found = re.search(r'<textarea id="chat-text"[^>]*placeholder="([^"]*)"', page)
    assert found is not None, page
    return found.group(1)


def frames_of(reply) -> list[dict[str, str]]:
    return [json.loads(event.data) for event in reply.events if event.event == "frame"]


async def calls_him(chat, name: str | None) -> None:
    async with chat.server.session_factory() as session:
        await session.execute(
            update(Person).where(Person.id == chat.person.id).values(calls_domo=name)
        )
        await session.commit()


async def test_title_and_brand_three_cases(adult_chat, child_chat, set_setting):
    # No name at all: Domo.
    assert brand_and_title((await adult_chat.get("/chat")).text) == ("Domo", "Domo")
    # The house's name only: Friday, for everyone.
    await set_setting("house.domo_name", "Friday")
    assert brand_and_title((await adult_chat.get("/chat")).text) == ("Friday", "Friday")
    assert brand_and_title((await child_chat.get("/chat")).text) == ("Friday", "Friday")
    assert placeholder_of((await child_chat.get("/chat")).text) == "Write to Friday..."
    # Their own wins over the house's; Sam still reads the house's.
    await calls_him(adult_chat, "Brenda")
    page = (await adult_chat.get("/chat")).text
    assert brand_and_title(page) == ("Brenda", "Brenda")
    assert placeholder_of(page) == "Write to Brenda..."
    assert brand_and_title((await child_chat.get("/chat")).text) == ("Friday", "Friday")
    # Without their own, back to the house's.
    await calls_him(adult_chat, None)
    assert brand_and_title((await adult_chat.get("/chat")).text) == ("Friday", "Friday")


async def test_a_visitor_reads_the_house_name(chat_server, set_setting):
    async with live_http_client(chat_server.base_url) as visitor:
        assert brand_and_title((await visitor.get("/login")).text) == ("Domo", "Domo")
        await set_setting("house.domo_name", "Friday")
        assert brand_and_title((await visitor.get("/login")).text) == ("Friday", "Friday")


async def test_setup_pages_say_domo(settings, session, clock, client_factory):
    from web_support import build_test_app

    async with session.begin_nested():
        owner = Person(display_name="Alex", role="adult")
        session.add(owner)
        await session.flush()
        await update_policy_setting(
            session, key="house.domo_name", value="Friday", by=owner.id, now=datetime.now(UTC)
        )
    # The installation is not over (``build_test_app`` alone leaves the gate on).
    installing = build_test_app(settings, session, clock)
    async with client_factory(installing) as visitor:
        for path in ("/login", "/setup/owner"):
            page = await visitor.get(path)
            assert page.status_code == 200, path
            assert brand_and_title(page.text) == ("Domo", "Domo"), path


async def test_house_name_is_escaped(owner_chat):
    reply = await owner_chat.say_with_tool("house_name", {"name": "<b>Max</b>"})
    # The frame is plain text in JSON: the page puts it in as text.
    [frame] = frames_of(reply)
    assert frame["brand"] == "<b>Max</b>"
    page = (await owner_chat.get("/chat")).text
    assert "<b>Max</b>" not in page
    assert brand_and_title(page) == ("&lt;b&gt;Max&lt;/b&gt;",) * 2
    assert placeholder_of(page) == "Write to &lt;b&gt;Max&lt;/b&gt;..."


async def test_open_page_follows_the_house_name(owner_chat):
    reply = await owner_chat.say_with_tool(
        "house_name", {"name": "Friday"}, text="Call yourself Friday for the whole house"
    )
    [frame] = frames_of(reply)
    assert frame == {
        "brand": "Friday",
        "title": f"{t('chat.title', 'en')} - Friday",
        "placeholder": t("chat.input.placeholder", "en", domo="Friday"),
    }
    names = [event.event for event in reply.events]
    assert names.index("frame") < names.index("done")
    assert brand_and_title((await owner_chat.get("/chat")).text) == ("Friday", "Friday")
    # A turn that changes nothing of the name tells the page nothing.
    assert frames_of(await owner_chat.say_with_tool("household_list", {})) == []


async def test_open_page_of_someone_with_their_own_name_does_not_move(owner_chat, adult_chat):
    await calls_him(owner_chat, "Brenda")
    reply = await owner_chat.say_with_tool("house_name", {"name": "Friday"})
    assert frames_of(reply) == []
    assert brand_and_title((await owner_chat.get("/chat")).text) == ("Brenda", "Brenda")
    # Alex, who has none, reads the house's.
    assert brand_and_title((await adult_chat.get("/chat")).text) == ("Friday", "Friday")


async def test_message_fragments_use_the_resolved_name(adult_chat, set_setting):
    await adult_chat.say("Hello")
    await set_setting("house.domo_name", "Friday")
    since = (await adult_chat.get("/chat/since")).text
    assert f'aria-label="{t("chat.author.assistant", "en", domo="Friday")}"' in since
    await calls_him(adult_chat, "Brenda")
    since = (await adult_chat.get("/chat/since")).text
    assert f'aria-label="{t("chat.author.assistant", "en", domo="Brenda")}"' in since
    assert "Friday" not in since
