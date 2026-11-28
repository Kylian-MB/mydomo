"""The link reaches the person without a model or a program seeing it (Task 18, design 6.5):
``invitation_send`` seals an invitation's link (``people.sealed``) and launches one of the
assistant's tools with a marker in its place; the marker opens only in the body of a request out,
as the gatekeeper is called (``madetools.means``), and what comes back is scrubbed of it.

The launches run on the fake sandbox and the fake gatekeeper of ``test_madetools_out``."""

import base64
import contextlib
import dataclasses
import json
import uuid
from typing import Any, cast
from urllib.parse import quote

import pytest
from domo_common.gatekeeper_api import OutRequest, OutResponse
from domo_common.sandbox_api import JsonObject, RunRequest
from domo_core.accounts.models import Person
from domo_core.audit.models import AuditEntry
from domo_core.chat.models import MessageAction
from domo_core.confirmations.models import Confirmation
from domo_core.conversation.execution import handle_tool_call
from domo_core.household.models import Invitation
from domo_core.household.service import create_invitation, hash_token
from domo_core.llm.types import ToolCall
from domo_core.madetools.launch import HOUSE
from domo_core.madetools.means import OUT_DONE, Chain, LaunchScope, Means
from domo_core.madetools.models import LaunchStop
from domo_core.madetools.risks import read_operations
from domo_core.people import directives
from domo_core.people.acts import AUDIT_ACTION
from domo_core.people.models import PersonAct
from domo_core.people.sealed import MARKER_PREFIX, SealedLinks
from domo_core.policy.roles import domo_actor
from fakes import FakeAsk, FakeRefused, FakeSandbox
from sqlalchemy import select
from test_concerned_person import _judge, _people, _turn
from test_madetools_handover import add_tool, all_audit, audit_of
from test_madetools_out import LIGHTS, PORT, FakeOut, going_out, opened
from test_own_idea import LIVE, mark

ORIGIN = "https://home.example"
MAILER = "mailer"
URL = f"http://{LIGHTS}:{PORT}/send"
OPERATIONS: dict[str, Any] = {
    "send": {"says": "Sends a message.", "risks": []},
    "look": {"says": "Looks at the messages sent.", "risks": []},
}
REASON = "Sam lives here and has no way in yet"
TEXT = "Hello Sam, here is your way in: {link}"


def _sent(record: Any) -> dict[str, Any]:
    """What the tool gave back, without the status the turn adds."""
    return {key: value for key, value in record.model_result.items() if key != "status"}


def send_call(**more: Any) -> ToolCall:
    arguments: dict[str, Any] = {
        "person": "Sam",
        "tool": MAILER,
        "operation": "send",
        "arguments": {"to": "sam@example.org", "text": TEXT},
    }
    return ToolCall(uuid.uuid4().hex[:8], "invitation_send", json.dumps({**arguments, **more}))


def posting(*, operation: str = "send", url: str = URL, headers: dict[str, str] | None = None):
    """A program that begins ``operation`` and posts the text it was given to ``url``; what it
    read back is kept in ``read``."""
    read: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        text = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": operation})
        try:
            read.append(
                await ask(
                    {
                        "do": "out",
                        "method": "POST",
                        "url": url,
                        "headers": cast(JsonObject, headers or {}),
                        "body": text,
                    }
                )
            )
        except FakeRefused as refused:
            read.append({"refused": refused.reason})
        return "sent"

    return program, read


class Echo(FakeOut):
    """An address that gives back what it received, as written, in a header, percent-encoded,
    with its slashes escaped, and its token alone."""

    async def out(self, request: OutRequest) -> OutResponse:
        self.asked.append(request)
        if request.resolve_only:
            return OutResponse(status=0, headers={}, body_b64="", zone="house", hops=0)
        got = base64.b64decode(request.body_b64 or "").decode("utf-8")
        link = got.rsplit(" ", 1)[-1]
        token = link.rsplit("/", 1)[-1]
        echoed = json.dumps(
            {
                "got": got,
                "quoted": quote(link, safe=""),
                "escaped": link.replace("/", "\\/"),
                "token": token,
            }
        )
        return OutResponse(
            status=200,
            headers={"x-echo": link, "content-type": "application/json"},
            body_b64=base64.b64encode(echoed.encode()).decode("ascii"),
            zone="house",
            hops=0,
        )


class Outbox(FakeOut):
    """An address that keeps every body it received and gives them all back."""

    async def out(self, request: OutRequest) -> OutResponse:
        self.asked.append(request)
        if request.resolve_only:
            return OutResponse(status=0, headers={}, body_b64="", zone="house", hops=0)
        bodies = [_body(sent) for sent in self.sent]
        return OutResponse(
            status=200,
            headers={},
            body_b64=base64.b64encode(json.dumps(bodies).encode()).decode("ascii"),
            zone="house",
            hops=0,
        )


async def _household(session_factory: Any, monkeypatch: Any, program: Any, gatekeeper: Any):
    """Alex, the owner, in a turn; Sam, a record who lives here, with an invitation that waits
    for its link; the assistant's tool ``mailer`` with its address open."""
    people = await _people(session_factory, ("Alex", "owner"))
    alex = people["Alex"]
    _judge(monkeypatch)
    async with session_factory() as writer:
        sam = Person(
            display_name="Sam",
            role="adult",
            state="known",
            standing="lives_here",
            origin="domo",
            origin_reason="Alex said Sam moves in",
        )
        writer.add(sam)
        await writer.flush()
        invitation, _ = await create_invitation(
            writer,
            display_name="Sam",
            role="adult",
            guest_days=None,
            created_by=alex.id,
            now=LIVE,
            with_token=False,
            record=sam,
        )
        await writer.commit()
    await add_tool(session_factory, MAILER, OPERATIONS)
    await opened(session_factory, MAILER)
    deps = going_out(session_factory, lambda: LIVE, {MAILER: program}, gatekeeper)
    ctx, reply_id, _ = await _turn(session_factory, alex, origin=ORIGIN, launch_deps=deps)
    return ctx, reply_id, alex, sam, invitation.id, deps


async def _invitation(session_factory: Any, invitation_id: uuid.UUID) -> Invitation:
    async with session_factory() as reader:
        found = await reader.get(Invitation, invitation_id)
        assert found is not None
        return found


def _body(request: OutRequest) -> str:
    return base64.b64decode(request.body_b64 or "").decode("utf-8")


def _sandbox(deps: Any) -> FakeSandbox:
    return deps.sandbox


async def test_the_program_sees_a_marker_and_the_address_receives_the_link(
    session_factory, monkeypatch
):
    program, read = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, _, _, invitation_id, deps = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert record.status == "done", record.model_result
    assert _sent(record) == {"sent": True}
    [run] = _sandbox(deps).runs
    given = str(run.entry["arguments"]["text"])  # type: ignore[index]
    assert MARKER_PREFIX in given and "/invite/" not in given
    # The run is for the house: no name of anyone.
    assert run.entry["for"] is None
    [sent] = gatekeeper.sent
    body = _body(sent)
    assert body.startswith("Hello Sam, here is your way in: https://home.example/invite/")
    token = body.rsplit("/", 1)[-1]
    invitation = await _invitation(session_factory, invitation_id)
    assert invitation.token_hash == hash_token(token)
    # Of the request that carried the link, the program reads its status alone.
    assert read == [{"status": 200}]
    # The act is traced, with ids alone in its audit line.
    async with session_factory() as reader:
        [act] = await reader.scalars(select(PersonAct).where(PersonAct.act == "link_sent"))
    [line] = await audit_of(session_factory, AUDIT_ACTION["link_sent"])
    assert line.details["act_id"] == str(act.id)


async def test_an_echo_of_the_link_never_comes_back(session_factory, monkeypatch):
    program, read = posting()
    gatekeeper = Echo()
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(record) == {"sent": True}
    link = _body(gatekeeper.sent[0]).rsplit(" ", 1)[-1]
    token = link.rsplit("/", 1)[-1]
    # The address echoed the link, in several spellings and a header: the program read only the
    # status of that request.
    assert read == [{"status": 200}]
    audited = json.dumps([entry.details for entry in await all_audit(session_factory)])
    for secret in (link, token, quote(link, safe=""), link.replace("/", "\\/")):
        assert secret not in audited
    [done] = await audit_of(session_factory, OUT_DONE)
    assert done.details["status"] == 200


class Encoded(FakeOut):
    """An address that answers what it received in base64, or in hexadecimal."""

    def __init__(self, how: str) -> None:
        super().__init__()
        self.how = how

    async def out(self, request: OutRequest) -> OutResponse:
        self.asked.append(request)
        if request.resolve_only:
            return OutResponse(status=0, headers={}, body_b64="", zone="house", hops=0)
        got = base64.b64decode(request.body_b64 or "")
        said = base64.b64encode(got) if self.how == "base64" else got.hex().encode()
        return OutResponse(
            status=200,
            headers={"x-said": said.decode("ascii")[:200]},
            body_b64=base64.b64encode(said).decode("ascii"),
            zone="house",
            hops=0,
        )


@pytest.mark.parametrize("shift", ["", "x", "xy"])
@pytest.mark.parametrize("how", ["base64", "hex"])
async def test_an_encoded_echo_misaligned_on_purpose_gives_nothing_back(
    session_factory, monkeypatch, shift, how
):
    """The program shifts the link so that the base64 of the body is not aligned on it, or the
    address answers in hexadecimal, which no scrub finds: it still reads only the status."""
    program, read = posting()
    gatekeeper = Encoded(how)
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    call = send_call(arguments={"text": f"{shift}{{link}}"})
    assert _sent(await handle_tool_call(call, ctx=ctx, reply_id=reply_id)) == {"sent": True}
    assert "/invite/" in _body(gatekeeper.sent[0])
    assert read == [{"status": 200}]


async def test_the_seal_opens_in_one_request_only(session_factory, monkeypatch):
    seen: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        text = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": "send"})
        for _ in range(2):
            seen.append(await ask({"do": "out", "method": "POST", "url": URL, "body": text}))
        return None

    gatekeeper = Echo()
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    assert _sent(await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)) == {"sent": True}
    first, second = gatekeeper.sent
    assert "/invite/" in _body(first)
    # The second request carries the marker, closed; its echo holds no link.
    assert "/invite/" not in _body(second) and MARKER_PREFIX in _body(second)
    assert seen[0] == {"status": 200}
    assert "/invite/" not in json.dumps(seen[1]) and MARKER_PREFIX in json.dumps(seen[1])
    # The request that carried the link may go to the address it names alone: no redirect
    # takes it elsewhere.
    assert [(o.host, o.port) for o in first.open] == [(LIGHTS, PORT)]


async def test_a_tool_that_launches_itself_never_opens_the_seal_again(session_factory, monkeypatch):
    asked: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        text = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": "send"})
        await ask({"do": "out", "method": "POST", "url": URL, "body": text})
        try:
            asked.append(await ask({"do": "tool", "name": MAILER, "arguments": {"text": text}}))
        except FakeRefused as refused:
            asked.append(refused.reason)
        return None

    gatekeeper = FakeOut()
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    assert _sent(await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)) == {"sent": True}
    # A tool is never launched in its own path: one request out, one launch.
    assert asked == ["in_its_own_path"] and len(gatekeeper.sent) == 1


async def test_a_link_is_never_placed_in_a_url_or_a_header(session_factory, monkeypatch):
    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        marker = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": "send"})
        await ask(
            {
                "do": "out",
                "method": "POST",
                "url": f"{URL}?to={marker}",
                "headers": {"x-link": str(marker)},
                "body": str(marker),
            }
        )
        return None

    gatekeeper = FakeOut()
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    call = send_call(arguments={"text": "{link}"})
    assert _sent(await handle_tool_call(call, ctx=ctx, reply_id=reply_id)) == {"sent": True}
    [sent] = gatekeeper.sent
    assert "/invite/" in _body(sent)
    assert MARKER_PREFIX in sent.url and "/invite/" not in sent.url
    assert MARKER_PREFIX in sent.headers["x-link"]
    assert "/invite/" not in json.dumps(sent.headers)


class Answering(FakeOut):
    """An address that answers ``status``."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status

    async def out(self, request: OutRequest) -> OutResponse:
        answer = await super().out(request)
        return answer if request.resolve_only else answer.model_copy(update={"status": self.status})


@pytest.mark.parametrize(
    "case", ["no_out", "other_operation", "error", "refused", "status_500", "status_404"]
)
async def test_a_tool_that_sent_nothing_leaves_no_working_link(session_factory, monkeypatch, case):
    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        text = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": "look" if case == "other_operation" else "send"})
        if case == "no_out":
            return None
        with contextlib.suppress(FakeRefused):
            await ask({"do": "out", "method": "POST", "url": URL, "body": text})
        if case == "error":
            raise FakeRefused("the mail server said no")
        return None

    gatekeeper: FakeOut = FakeOut(refuse="unreachable") if case == "refused" else FakeOut()
    if case.startswith("status_"):
        gatekeeper = Answering(int(case.removeprefix("status_")))
    ctx, reply_id, _, _, invitation_id, _ = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert record.model_result["sent"] is False, record.model_result
    assert record.model_result["why"] == ("failed" if case == "error" else "not_placed")
    if case == "other_operation":
        # Under another operation the marker stays closed: the address got the marker alone.
        assert "/invite/" not in _body(gatekeeper.sent[0])
    invitation = await _invitation(session_factory, invitation_id)
    assert invitation.token_hash is None
    async with session_factory() as reader:
        acts = list(await reader.scalars(select(PersonAct).where(PersonAct.act == "link_sent")))
    assert acts == []


async def test_the_tools_result_and_the_stored_action_hold_no_link(session_factory, monkeypatch):
    program, _ = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, *_ = await _household(session_factory, monkeypatch, program, gatekeeper)
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert record.status == "done"
    link = _body(gatekeeper.sent[0]).rsplit(" ", 1)[-1]
    token = link.rsplit("/", 1)[-1]
    async with session_factory() as reader:
        stored = list(await reader.scalars(select(MessageAction.summary)))
        audited = list(await reader.scalars(select(AuditEntry.details)))
    told = json.dumps([record.model_result, record.card, record.card_once, stored, audited])
    assert "/invite/" not in told and token not in told and MARKER_PREFIX not in told


async def test_without_a_request_the_link_starts_with_the_house_s_own_address(
    session_factory, monkeypatch
):
    # A review, or a turn the assistant started: no request gave an address.
    program, read = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, *_, deps = await _household(session_factory, monkeypatch, program, gatekeeper)
    house = dataclasses.replace(deps, house_origin="https://house.example:8443")
    ctx = dataclasses.replace(ctx, origin="", launch_deps=house)
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(record) == {"sent": True}
    assert _body(gatekeeper.sent[0]).startswith(
        "Hello Sam, here is your way in: https://house.example:8443/invite/"
    )
    assert read == [{"status": 200}]


async def test_a_link_without_an_address_is_never_sent(session_factory, monkeypatch):
    program, _ = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, _, _, invitation_id, deps = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    ctx = dataclasses.replace(ctx, origin="", launch_deps=deps)
    assert deps.house_origin is None
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert (record.status, _sent(record)) == ("failed", {"sent": False, "why": "no_address"})
    # Nothing was made, nothing launched.
    assert (await _invitation(session_factory, invitation_id)).token_hash is None
    assert _sandbox(deps).runs == [] and gatekeeper.sent == []


async def test_a_dry_run_opens_no_seal(session_factory, live_clock, add_member):
    tool = await add_tool(session_factory, MAILER, OPERATIONS)
    gatekeeper = FakeOut()
    sealed = SealedLinks(MAILER, "send")
    marker = sealed.seal(f"{ORIGIN}/invite/abc", scrub_too=("abc",))
    scope = LaunchScope(
        tool_name=MAILER,
        tool_id=tool.id,
        for_whom=HOUSE,
        actor=domo_actor(),
        depth=0,
        chain=Chain(),
        waited=False,
        path=(MAILER,),
        dry_run=True,
        operations=read_operations(OPERATIONS),
        sealed=sealed,
    )
    means = Means(going_out(session_factory, live_clock, {}, gatekeeper), scope)
    assert await means.answer({"do": "begin", "operation": "send"}) == {"ok": None}
    out: JsonObject = {"do": "out", "method": "POST", "url": URL, "body": marker}
    assert await means.answer(out) == {"ok": None}
    assert gatekeeper.asked == [] and sealed.used == 0


async def test_sending_to_someone_withdrawn_is_refused_before_any_launch(
    session_factory, monkeypatch
):
    program, _ = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, alex, sam, invitation_id, deps = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    async with session_factory() as writer:
        await directives.set_directive(
            writer,
            kind="withdraw",
            person=await writer.get(Person, sam.id),
            description=None,
            except_words=None,
            until=None,
            owner=await writer.get(Person, alex.id),  # type: ignore[arg-type]
            now=LIVE,
        )
        await writer.commit()
    # His own idea: the barrier refuses it before any judgement.
    await handle_tool_call(mark(REASON), ctx=ctx, reply_id=reply_id)
    his = await handle_tool_call(send_call(reason=REASON), ctx=ctx, reply_id=reply_id)
    assert his.model_result.get("reason") == "owner_directive", his.model_result
    # The owner's own word passes the barrier, yet nothing is made nor sent.
    owners = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(owners) == {"sent": False, "why": "owner_directive"}
    assert _sandbox(deps).runs == [] and gatekeeper.asked == []
    assert (await _invitation(session_factory, invitation_id)).token_hash is None


def test_a_seal_opens_its_markers_and_scrubs_the_usual_spellings():
    sealed = SealedLinks(MAILER, "send")
    link = f"{ORIGIN}/invite/Tok_en-123"
    marker = sealed.seal(link, scrub_too=("Tok_en-123",))
    assert marker.startswith(MARKER_PREFIX) and len(marker) == len(MARKER_PREFIX) + 32
    body, opened_count = sealed.open_in(f"a {marker} b {marker}".encode())
    assert (body, opened_count) == (f"a {link} b {link}".encode(), 2)
    form, count = sealed.open_in(f"to=sam&link={quote(marker, safe='')}".encode())
    assert (form.decode(), count) == (f"to=sam&link={quote(link, safe='')}", 1)
    assert sealed.used == 0
    sealed.sent(3)
    assert sealed.used == 3
    assert sealed.values() == (link, "Tok_en-123")
    raw = link.encode()
    for spelling in (
        link,
        quote(link, safe=""),
        quote(link),
        link.replace("/", "\\/"),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "Tok_en-123",
    ):
        scrubbed = sealed.scrub(f"<{spelling}>")
        assert "Tok_en-123" not in scrubbed and marker in scrubbed
        assert b"Tok_en-123" not in sealed.scrub_bytes(f"<{spelling}>".encode())
    assert not sealed.opens_in(MAILER, "look") and not sealed.opens_in("other", "send")


async def test_a_launch_asked_by_the_sending_tool_is_scrubbed_too(session_factory, monkeypatch):
    """What the chain sealed is scrubbed from what any of its launches reads back: a tool that
    looks at the messages sent never reads the link."""
    reader_read: list[Any] = []

    async def sender(request: RunRequest, ask: FakeAsk) -> Any:
        text = request.entry["arguments"]["text"]  # type: ignore[index]
        await ask({"do": "begin", "operation": "send"})
        await ask({"do": "out", "method": "POST", "url": URL, "body": text})
        return await ask({"do": "tool", "name": "outbox", "arguments": {}})

    async def outbox(request: RunRequest, ask: FakeAsk) -> Any:
        await ask({"do": "begin", "operation": "look"})
        reader_read.append(await ask({"do": "out", "method": "GET", "url": URL}))
        return None

    gatekeeper = Outbox()
    ctx, reply_id, _, _, _, deps = await _household(
        session_factory, monkeypatch, sender, gatekeeper
    )
    await add_tool(session_factory, "outbox", OPERATIONS)
    await opened(session_factory, "outbox")
    ctx = dataclasses.replace(
        ctx,
        launch_deps=going_out(
            session_factory, lambda: LIVE, {MAILER: sender, "outbox": outbox}, gatekeeper
        ),
    )
    del deps
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(record) == {"sent": True}
    # The address holds the link, and the tool that read it back got the marker.
    assert "/invite/" in _body(gatekeeper.sent[0])
    assert reader_read and "/invite/" not in json.dumps(reader_read)
    assert MARKER_PREFIX in json.dumps(reader_read)


async def test_a_send_that_failed_gives_back_the_link_that_was_there(session_factory, monkeypatch):
    """An invitation whose link was shown already: a failed send leaves that link working."""
    program, _ = posting()
    gatekeeper = FakeOut(refuse="unreachable")
    ctx, reply_id, _, _, invitation_id, _ = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    async with session_factory() as writer:
        row = await writer.get(Invitation, invitation_id)
        assert row is not None
        row.token_hash = hash_token("the-link-shown-before")
        await writer.commit()
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(record) == {"sent": False, "why": "not_placed"}
    invitation = await _invitation(session_factory, invitation_id)
    assert invitation.token_hash == hash_token("the-link-shown-before")


async def test_a_send_that_needs_a_yes_keeps_nothing_to_run_again(
    session_factory, monkeypatch, grant
):
    """The owner asked to be asked before the assistant's tools begin anything: the sealed launch
    stops, but no stop is kept (its relaunch could only send the marker), nobody is asked, and the
    token is unmade."""
    program, _ = posting()
    gatekeeper = FakeOut()
    ctx, reply_id, _, _, invitation_id, deps = await _household(
        session_factory, monkeypatch, program, gatekeeper
    )
    await grant("program.begin", for_domo=True, ask="yes")
    record = await handle_tool_call(send_call(), ctx=ctx, reply_id=reply_id)
    assert _sent(record) == {"sent": False, "why": "needs_yes"}
    assert gatekeeper.asked == []
    async with session_factory() as reader:
        assert list(await reader.scalars(select(LaunchStop))) == []
        pending = list(await reader.scalars(select(Confirmation)))
    assert pending == []
    assert (await _invitation(session_factory, invitation_id)).token_hash is None
