"""The sentences of the text program (spec 6.12, finding WEB-8): the server gives them in the
person's language, as the assistant wrote them, to a device and to nothing else."""

from collections.abc import AsyncIterator

import httpx
import pytest
from api_support import API, ApiPilot
from chat_support import ChatServer, Member
from domo_common.i18n import t
from domo_common.meanings import (
    all_keys,
    default_text,
    group_of,
    has_meaning,
    holes,
    meaning_note,
)
from domo_core.accounts.models import Person
from domo_core.accounts.sessions import SESSION_COOKIE
from domo_core.channels.program_texts import TALK_GROUP, TALK_KEYS
from domo_core.texts.store import Reader, save_written
from sqlalchemy import update


@pytest.fixture
async def owner_device(chat_server: ChatServer, owner_member: Member) -> AsyncIterator[ApiPilot]:
    async with ApiPilot.paired(chat_server, owner_member) as device:
        yield device


async def fetched(device: ApiPilot) -> dict:
    answer = await device.client.get(f"{API}/texts")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def read_in(server: ChatServer, person: Person, language: str) -> None:
    async with server.session_factory() as session:
        await session.execute(
            update(Person).where(Person.id == person.id).values(language=language)
        )
        await session.commit()


async def test_the_texts_come_in_the_persons_language(
    chat_server: ChatServer, owner_device: ApiPilot, owner_member: Member
):
    await read_in(chat_server, owner_member.person, "fr")
    found = await fetched(owner_device)
    assert found["language"] == "fr"
    assert set(found["texts"]) == set(TALK_KEYS)
    assert found["texts"]["talk.too_long"] == default_text("talk.too_long", "fr")
    assert found["texts"]["talk.too_long"] != t("talk.too_long", "en")
    # The holes are left for the program to fill.
    assert "{error}" in found["texts"]["talk.refused"]
    assert "{url}" in found["texts"]["talk.unreachable"]
    await chat_server.texts_idle()


async def test_a_text_he_rewrote_is_the_one_given(
    chat_server: ChatServer, owner_device: ApiPilot, owner_member: Member
):
    await read_in(chat_server, owner_member.person, "fr")
    async with chat_server.session_factory() as session:
        await save_written(
            session,
            Reader(owner_member.person.id, "fr"),
            {"talk.too_long": "Là, c'est vraiment trop long."},
            model_id="m",
            now=chat_server.clock(),
        )
        await session.commit()
    found = await fetched(owner_device)
    assert found["texts"]["talk.too_long"] == "Là, c'est vraiment trop long."
    assert found["texts"]["talk.no_key"] == default_text("talk.no_key", "fr")
    await chat_server.texts_idle()


def test_every_talk_key_has_a_meaning_a_french_default_and_a_note():
    assert {key for key in all_keys() if group_of(key) == TALK_GROUP} == set(TALK_KEYS)
    for key in TALK_KEYS:
        assert has_meaning(key), key
        assert meaning_note(key), key
        french = default_text(key, "fr")
        assert french != default_text(key, "en"), key
        assert holes(key) == {"talk.refused": {"error"}, "talk.unreachable": {"url"}}.get(
            key, frozenset()
        )
        for hole in holes(key):
            assert "{" + hole + "}" in french


async def test_the_route_needs_a_device_and_refuses_a_browser_session(
    chat_server: ChatServer, owner_device: ApiPilot, live_owner_client: httpx.AsyncClient
):
    async with httpx.AsyncClient(base_url=str(chat_server.base_url)) as plain:
        for headers in ({}, {"authorization": "Bearer nope"}):
            refused = await plain.get(f"{API}/texts", headers=headers)
            assert (refused.status_code, refused.json()) == (401, {"error": "unknown_device"})
    token = live_owner_client.cookies[SESSION_COOKIE]
    refused = await owner_device.client.get(
        f"{API}/texts", headers={"cookie": f"{SESSION_COOKIE}={token}"}
    )
    assert (refused.status_code, refused.json()) == (403, {"error": "browser_session"})
