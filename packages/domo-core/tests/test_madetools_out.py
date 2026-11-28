"""A program goes out (step 2, spec 13.3 to 13.6, 13.10): ``domo.out``, a request the gatekeeper
sends for the program to an address Domo opened, naming an account whose secret nobody sees.

The request is read (``asks.read_ask``: ``OutAsk``), judged as ``program.out`` for whom the launch
is, refused before the gatekeeper is called when its address is not open for the launch's tool or
its account is not one the launch may use, bounded by Domo's settings (``out_seconds``,
``out_bytes``, ``out_per_launch``, ``session_idle_minutes``) and audited twice: the barrier's line
before, ``madetool.out_done`` after. The gatekeeper is a fake that keeps what it was asked
(``FakeOut``), or the real client over ``respx`` where what its answers say matters. The programs
are coroutines given to a ``FakeSandbox``, as in ``test_madetools_means``."""

import base64
import dataclasses
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from domo_common.gatekeeper_api import OutRequest, OutResponse
from domo_common.sandbox_api import JsonObject, RunRequest
from domo_core.accounts.actors import actor_of
from domo_core.accounts.models import Person
from domo_core.gatekeeper_client import GatekeeperClient, OutRefused
from domo_core.madetools import means as means_module
from domo_core.madetools.asks import AskNotRight, OutAsk, read_ask
from domo_core.madetools.launch import HOUSE, ForWhom, LaunchDeps, Launched, launch
from domo_core.madetools.means import OUT_DONE, Chain, LaunchScope, Means
from domo_core.madetools.models import MadeTool
from domo_core.madetools.risks import (
    OperationsNotRight,
    kept_operations,
    minimum_risks,
    read_operations,
)
from domo_core.madetools.settings import set_for_tool
from domo_core.outward.models import ServiceAccount, account_ref
from domo_core.outward.service import open_address
from domo_core.policy.catalog import DEFAULT_POLICY, Risks
from fakes import FakeAsk, FakeRefused
from llm_support import CORE_TOKEN
from test_madetools_handover import add_tool, all_audit, audit_of, by_program, deps_of
from test_madetools_yes import stops, taken

LIGHTS = "192.168.1.20"
PORT = 8080
STATES = f"http://{LIGHTS}:{PORT}/api/states"
# The operations of the test tools: reading, switching (it acts, and cannot be undone), fetching
# from the internet, and ordering (it costs).
OPERATIONS: dict[str, Any] = {
    "read_state": {"says": "Reads a state.", "risks": []},
    "switch": {"says": "Switches a light.", "risks": ["physical", "irreversible"]},
    "fetch": {"says": "Reads a site.", "risks": ["outward"]},
    "order": {"says": "Orders the pizzas.", "risks": ["costs", "outward", "irreversible"]},
}
BEGIN = {"do": "begin", "operation": "read_state"}
GET: JsonObject = {"do": "out", "method": "GET", "url": STATES}


@pytest.fixture
async def alex(add_member) -> Person:
    return await add_member("Alex", "owner")


@pytest.fixture
async def dana(add_member, alex) -> Person:
    return await add_member("Dana", "adult")


@pytest.fixture
async def paul(add_member, alex) -> Person:
    return await add_member("Paul", "child")


@pytest.fixture
async def sam(add_member, alex) -> Person:
    return await add_member("Sam", "guest")


class FakeOut:
    """The gatekeeper's relay for programs: it keeps each request it is given (``asked``) and
    answers ``body`` (``status`` 200, from the house), or the zone of ``zones`` for a resolve
    only; ``refuse`` makes it refuse every request that is not a resolve with that code. A send
    whose zone (``zones``, else ``moved`` once a name was resolved) is not the one it expects is
    refused ``zone_changed``, as the gatekeeper does: nothing is sent."""

    def __init__(
        self,
        body: bytes = b'{"on": true}',
        *,
        zones: dict[str, str] | None = None,
        refuse: str | None = None,
        moved: dict[str, str] | None = None,
        refuse_lookup: str | None = None,
    ) -> None:
        self.asked: list[OutRequest] = []
        self.body = body
        self.zones = zones or {}
        self.refuse = refuse
        self.moved = moved or {}
        self.refuse_lookup = refuse_lookup

    @property
    def sent(self) -> list[OutRequest]:
        """What was really sent: every request but a resolve only."""
        return [request for request in self.asked if not request.resolve_only]

    async def out(self, request: OutRequest) -> OutResponse:
        self.asked.append(request)
        host = request.url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        zone = cast(Any, self.zones.get(host, "house"))
        if request.resolve_only and self.refuse_lookup is not None:
            raise OutRefused(self.refuse_lookup)
        if request.resolve_only:
            return OutResponse(status=0, headers={}, body_b64="", zone=zone, hops=0)
        zone = cast(Any, self.moved.get(host, zone))
        if request.expect_zone is not None and zone != request.expect_zone:
            raise OutRefused("zone_changed")
        if self.refuse is not None:
            raise OutRefused(self.refuse)
        if len(self.body) > request.most_bytes:
            raise OutRefused("too_big")
        return OutResponse(
            status=200,
            headers={"content-type": "application/json"},
            body_b64=base64.b64encode(self.body).decode("ascii"),
            zone=zone,
            hops=0,
        )


def going_out(session_factory, clock, programs: dict[str, Any], gatekeeper: Any) -> LaunchDeps:
    """What a launch needs, with ``gatekeeper`` as the way out."""
    deps = deps_of(session_factory, clock, by_program(**programs), [])
    return dataclasses.replace(deps, gatekeeper=gatekeeper)


def sending(*asks: JsonObject, operation: str = "read_state") -> Any:
    """A program that begins ``operation``, then sends each of ``asks`` and gives back, for each,
    what it read, or its refusal (its reason, and the setting it names)."""

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask({"do": "begin", "operation": operation})
        met: list[Any] = []
        for question in asks:
            try:
                met.append(await ask(question))
            except FakeRefused as refused:
                met.append(
                    {"refused": refused.reason}
                    if refused.setting is None
                    else {
                        "refused": refused.reason,
                        "setting": refused.setting,
                        "value": refused.value,
                    }
                )
        return met

    return program


async def opened(
    session_factory,
    tool_name: str | None,
    host: str = LIGHTS,
    port: int = PORT,
    *,
    on_host: bool = False,
) -> None:
    async with session_factory() as writer:
        await open_address(
            writer,
            tool_name=tool_name,
            host=host,
            port=port,
            on_host=on_host,
            by="domo",
            now=datetime.now(UTC),
        )
        await writer.commit()


async def account(
    session_factory,
    name: str,
    whose: Person | None,
    giver: Person,
    *,
    for_guests: bool = False,
    for_domo: bool = True,
    removed: bool = False,
) -> ServiceAccount:
    """An account ``name`` of ``whose`` (``None``: the house's), bound to the lights' address."""
    async with session_factory() as writer:
        row = ServiceAccount(
            name=name,
            person_id=None if whose is None else whose.id,
            ref=account_ref(uuid.uuid4()),
            hosts=[{"host": LIGHTS, "port": PORT}],
            place={"kind": "header", "name": "authorization", "template": "Bearer {value}"},
            given_by=giver.id,
            for_guests=for_guests,
            for_domo=for_domo,
        )
        writer.add(row)
        await writer.flush()
        if removed:
            row.removed_at = datetime.now(UTC)
            row.removed_by = str(giver.id)
        await writer.commit()
        return row


async def launched(
    deps: LaunchDeps, tool: MadeTool, person: Person | None, cause: str = "trigger"
) -> Launched:
    for_whom = HOUSE if person is None else ForWhom(person)
    return await launch(deps, tool.id, arguments={}, for_whom=for_whom, cause=cause)  # type: ignore[arg-type]


async def own_settings(session_factory, tool: MadeTool, **values: int) -> None:
    async with session_factory() as writer:
        row = await writer.get(MadeTool, tool.id)
        assert row is not None
        await set_for_tool(writer, row, values)
        await writer.commit()


# --- reading the request -------------------------------------------------------------------------


def test_an_out_is_read_and_what_is_not_one_is_refused():
    assert read_ask(GET) == OutAsk("GET", STATES, {}, None, None)
    assert read_ask(
        {
            "do": "out",
            "method": "post",
            "url": STATES,
            "headers": {"Content-Type": "application/json"},
            "body": '{"é": 1}',
            "account": "lights",
        }
    ) == OutAsk("POST", STATES, {"Content-Type": "application/json"}, '{"é": 1}'.encode(), "lights")
    assert read_ask({**GET, "body_b64": base64.b64encode(b"\xff\x00").decode()}) == OutAsk(
        "GET", STATES, {}, b"\xff\x00", None
    )
    assert read_ask({**GET, "headers": None, "account": None, "body": None}) == OutAsk(
        "GET", STATES, {}, None, None
    )
    for wrong in (
        {**GET, "method": "CONNECT"},
        {**GET, "method": "TRACE"},
        {**GET, "url": "ftp://192.168.1.20/x"},
        {**GET, "url": "file:///etc/passwd"},
        {**GET, "url": "http:///no-host"},
        {**GET, "url": "192.168.1.20:8080/x"},
        {**GET, "url": "http://user:xxxx@192.168.1.20/"},
        {**GET, "url": "http://192.168.1.20/a b"},
        {**GET, "url": "http://192.168.1.20/\nx"},
        {**GET, "url": "http://192.168.1.20:99999/"},
        {**GET, "url": "x" * 9000},
        {**GET, "headers": {"bad name": "x"}},
        {**GET, "headers": {"x-a": "line\r\nInjected: xxxx"}},
        {**GET, "headers": {"x-a": "nul\x00"}},
        {**GET, "headers": {"x-a": 3}},
        {**GET, "headers": ["x"]},
        {**GET, "body": 3},
        {**GET, "body_b64": "not base64!"},
        {**GET, "body": "a", "body_b64": "YQ=="},
        {**GET, "account": "Not A Name"},
        {**GET, "account": "x"},
        {**GET, "extra": 1},
        {"do": "out", "url": STATES},
    ):
        with pytest.raises(AskNotRight) as refused:
            read_ask(wrong)  # type: ignore[arg-type]
        assert "xxxx" not in str(refused.value)


def test_the_code_s_minimum_risks_of_an_out():
    house = OutAsk("GET", STATES, {}, None, None)
    assert minimum_risks(house, DEFAULT_POLICY) == Risks()
    # Towards the internet by its literal, or by what the gatekeeper found.
    site = OutAsk("GET", "https://api.example.com/x", {}, None, None)
    assert minimum_risks(site, DEFAULT_POLICY) == Risks(outward=True)
    assert minimum_risks(house, DEFAULT_POLICY, zone="internet") == Risks(outward=True)
    assert minimum_risks(house, DEFAULT_POLICY, zone="host") == Risks()
    # An account used for anything but a read cannot be undone; a read with one can.
    assert minimum_risks(OutAsk("POST", STATES, {}, None, "lights"), DEFAULT_POLICY) == Risks(
        irreversible=True
    )
    assert minimum_risks(OutAsk("HEAD", STATES, {}, None, "lights"), DEFAULT_POLICY) == Risks()
    assert minimum_risks(OutAsk("DELETE", STATES, {}, None, None), DEFAULT_POLICY) == Risks()


def test_an_operation_may_name_the_accounts_and_the_addresses_it_goes_out_with():
    # Task 7: as declared, what Domo and the reviewer read of where a tool goes out.
    given = {
        "switch": {
            "says": "Switches a light.",
            "risks": ["physical", "irreversible"],
            "accounts": ["lights"],
            "addresses": [{"host": "LAMPS.home.arpa", "port": PORT}, {"host": LIGHTS, "port": 80}],
        },
        "read_state": {"says": "Reads a state.", "risks": [], "accounts": [], "addresses": []},
    }
    read = read_operations(given)
    assert read["switch"].accounts == ("lights",)
    assert read["switch"].addresses == (("lamps.home.arpa", PORT), (LIGHTS, 80))
    assert read["read_state"].accounts == () and read["read_state"].addresses == ()
    # Kept as declared, the empty ones left out: an operation that names none keeps its form.
    kept = kept_operations(read)
    assert kept == {
        "switch": {
            "says": "Switches a light.",
            "risks": ["irreversible", "physical"],
            "accounts": ["lights"],
            "addresses": [{"host": "lamps.home.arpa", "port": PORT}, {"host": LIGHTS, "port": 80}],
        },
        "read_state": {"says": "Reads a state.", "risks": []},
    }
    assert read_operations(kept) == read


@pytest.mark.parametrize(
    "more",
    [
        {"accounts": "lights"},
        {"accounts": ["Lights"]},
        {"accounts": ["lights", "lights"]},
        {"accounts": [f"a{n}x" for n in range(11)]},
        {"addresses": [{"host": LIGHTS}]},
        {"addresses": [{"host": LIGHTS, "port": 0}]},
        {"addresses": [{"host": LIGHTS, "port": True}]},
        {"addresses": [{"host": "http://lamps", "port": 80}]},
        {"addresses": [{"host": LIGHTS, "port": 80, "path": "/x"}]},
        {"addresses": [{"host": "lamps", "port": 80}, {"host": "LAMPS", "port": 80}]},
        {"addresses": "lamps:80"},
        {"elsewhere": []},
    ],
)
def test_what_an_operation_names_of_going_out_is_checked(more: dict[str, Any]):
    with pytest.raises(OperationsNotRight):
        read_operations({"switch": {"says": "Switches.", "risks": [], **more}})


# --- going out -----------------------------------------------------------------------------------


async def test_a_program_reads_an_open_address_with_two_audit_lines(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    gatekeeper = FakeOut()
    deps = going_out(session_factory, live_clock, {"lights": sending(GET)}, gatekeeper)
    came = await launched(deps, tool, dana)
    assert came == Launched(
        "done",
        [{"status": 200, "headers": {"content-type": "application/json"}, "body": '{"on": true}'}],
    )
    [sent] = gatekeeper.asked
    assert (sent.method, sent.url, sent.account, sent.resolve_only) == ("GET", STATES, None, False)
    # An address its literal settles: the zone judged is the literal's (ruling R12).
    assert sent.expect_zone == "house"
    assert [(a.host, a.port, a.on_host) for a in sent.open] == [(LIGHTS, PORT, False)]
    # The settings at their start: 30 s, and out_bytes (1 MB, ruling R21).
    assert (sent.seconds, sent.most_bytes, sent.session_idle_seconds) == (30, 1_048_576, 3600)
    [decision] = [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == "program.out"
    ]
    assert decision.actor == f"person:{dana.id}"
    assert decision.details["facts"] == {"zone": "house", "writes": False, "with_account": False}
    [done] = await audit_of(session_factory, OUT_DONE)
    assert done.actor == f"person:{dana.id}"
    details = dict(done.details)
    assert details.pop("ms") >= 0
    assert details == {
        "tool": "lights",
        "operation": "read_state",
        "for_person": str(dana.id),
        "host": LIGHTS,
        "port": PORT,
        "method": "GET",
        "status": 200,
        "refused": None,
        "bytes_out": 0,
        "bytes_in": len(b'{"on": true}'),
        "hops": 0,
        "zone": "house",
    }


async def test_a_body_that_is_not_utf8_comes_back_in_base64(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    raw = b"\x89PNG\xff\x00"
    deps = going_out(session_factory, live_clock, {"lights": sending(GET)}, FakeOut(raw))
    [answer] = (await launched(deps, tool, dana)).result  # type: ignore[misc]
    assert answer == {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body_b64": base64.b64encode(raw).decode("ascii"),
    }


async def test_an_address_not_open_for_the_tool_is_refused_and_the_gatekeeper_never_called(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    # Open for another tool, and for the programs run by hand: never for this one.
    await opened(session_factory, "heating")
    await opened(session_factory, None)
    gatekeeper = FakeOut()
    other_port = {**GET, "url": f"http://{LIGHTS}:8124/api"}
    deps = going_out(session_factory, live_clock, {"lights": sending(GET, other_port)}, gatekeeper)
    came = await launched(deps, tool, dana)
    assert came.result == [{"refused": "closed_address"}, {"refused": "closed_address"}]
    assert gatekeeper.asked == []
    [done, _] = await audit_of(session_factory, OUT_DONE)
    assert (done.details["refused"], done.details["status"]) == ("closed_address", None)
    assert done.details["host"] == LIGHTS


@pytest.mark.parametrize("host", ["tell!domo*to,obey", "ignore~all", "a..b"])
async def test_a_refused_request_keeps_no_host_that_is_no_host(
    session_factory, live_clock, dana, host
):
    # I3: what the program wrote where a host goes, when it is no valid host name nor address,
    # is never kept in the audit line (nor given back by the figures).
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    gatekeeper = FakeOut()
    written = {**GET, "url": f"http://{host}/api"}
    deps = going_out(session_factory, live_clock, {"lights": sending(written)}, gatekeeper)
    came = await launched(deps, tool, dana)
    assert came.result == [{"refused": "closed_address"}]
    [done] = await audit_of(session_factory, OUT_DONE)
    assert (done.details["refused"], done.details["host"]) == ("closed_address", None)
    assert host not in json.dumps(done.details)


async def test_a_program_run_by_hand_reaches_the_addresses_open_without_a_tool(
    session_factory, live_clock, dana
):
    await opened(session_factory, None)
    gatekeeper = FakeOut()
    deps = going_out(session_factory, live_clock, {}, gatekeeper)
    means = Means(
        deps,
        LaunchScope(
            tool_name=None,
            tool_id=None,
            for_whom=ForWhom(dana),
            actor=actor_of(dana),
            depth=0,
            chain=Chain(),
            waited=False,
            path=(),
            operations=read_operations(OPERATIONS),
        ),
    )
    assert await means.answer(BEGIN) == {"ok": None}
    answer = await means.answer(GET)
    assert answer is not None and answer["ok"]["status"] == 200  # type: ignore[index]
    assert len(gatekeeper.asked) == 1


# --- the accounts (Review Focus 3) ---------------------------------------------------------------


def with_account(name: str, method: str = "GET") -> JsonObject:
    return {**GET, "method": method, "account": name}


async def test_a_persons_account_serves_their_own_launches_only(
    session_factory, live_clock, alex, dana, paul
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    music = await account(session_factory, "music", dana, dana)
    gatekeeper = FakeOut()
    deps = going_out(
        session_factory, live_clock, {"lights": sending(with_account("music"))}, gatekeeper
    )
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    assert [request.account for request in gatekeeper.asked] == [music.ref]
    # Never for a launch for someone else, nor for Domo on his own (the house).
    assert (await launched(deps, tool, paul)).result == [{"refused": "account_not_yours"}]
    assert (await launched(deps, tool, None)).result == [{"refused": "account_not_yours"}]
    assert len(gatekeeper.asked) == 1


async def test_a_house_account_is_never_a_guests_unless_the_owner_opened_it(
    session_factory, live_clock, alex, sam
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    await account(session_factory, "lights", None, alex)
    shared = await account(session_factory, "heating", None, alex, for_guests=True)
    gatekeeper = FakeOut()
    program = sending(with_account("lights"), with_account("heating"))
    deps = going_out(session_factory, live_clock, {"lights": program}, gatekeeper)
    came = await launched(deps, tool, sam, cause="turn")
    assert came.result[0] == {"refused": "account_not_yours"}  # type: ignore[index]
    assert came.result[1]["status"] == 200  # type: ignore[index]
    assert [request.account for request in gatekeeper.asked] == [shared.ref]


async def test_a_house_account_closed_to_domo_alone_serves_a_person_and_never_him(
    session_factory, live_clock, alex, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    house = await account(session_factory, "lights", None, alex, for_domo=False)
    gatekeeper = FakeOut()
    deps = going_out(
        session_factory, live_clock, {"lights": sending(with_account("lights"))}, gatekeeper
    )
    assert (await launched(deps, tool, None)).result == [{"refused": "account_not_yours"}]
    assert gatekeeper.asked == []
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    assert [request.account for request in gatekeeper.asked] == [house.ref]


async def test_a_live_account_closed_to_the_launch_is_not_yours_though_one_was_removed(
    session_factory, live_clock, alex, sam
):
    # A house account removed once, and given again closed to guests: a guest reads "not
    # yours", never "removed".
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    await account(session_factory, "lights", None, alex, removed=True)
    await account(session_factory, "lights", None, alex)
    gatekeeper = FakeOut()
    deps = going_out(
        session_factory, live_clock, {"lights": sending(with_account("lights"))}, gatekeeper
    )
    came = await launched(deps, tool, sam, cause="turn")
    assert came.result == [{"refused": "account_not_yours"}]
    assert gatekeeper.asked == []


async def test_a_removed_account_is_refused_and_one_the_vault_lacks_says_so(
    session_factory, live_clock, alex, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    await account(session_factory, "music", dana, dana, removed=True)
    await account(session_factory, "lights", None, alex)
    gatekeeper = FakeOut(refuse="account_not_found")
    program = sending(with_account("music"), with_account("lights"), with_account("nothing"))
    deps = going_out(session_factory, live_clock, {"lights": program}, gatekeeper)
    came = await launched(deps, tool, dana)
    # The account asked but whose form was never filled reaches the gatekeeper: its vault has no
    # secret for it.
    assert came.result == [
        {"refused": "account_removed"},
        {"refused": "account_not_found"},
        {"refused": "account_not_yours"},
    ]
    assert len(gatekeeper.asked) == 1


# --- the bounds (Review Focus 5) -----------------------------------------------------------------


async def test_out_bytes_and_out_seconds_go_down_and_their_refusals_name_them(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    await own_settings(session_factory, tool, out_bytes=5, out_seconds=7)
    gatekeeper = FakeOut(b"more than five")
    deps = going_out(session_factory, live_clock, {"lights": sending(GET)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == [
        {"refused": "too_big", "setting": "out_bytes", "value": 5}
    ]
    assert (gatekeeper.asked[0].most_bytes, gatekeeper.asked[0].seconds) == (5, 7)
    gatekeeper.refuse = "too_slow"
    assert (await launched(deps, tool, dana)).result == [
        {"refused": "too_slow", "setting": "out_seconds", "value": 7}
    ]


async def test_out_per_launch_counts_and_zero_refuses_the_first(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    await own_settings(session_factory, tool, out_per_launch=0)
    gatekeeper = FakeOut()
    deps = going_out(session_factory, live_clock, {"lights": sending(GET, GET, GET)}, gatekeeper)
    refused = {"refused": "too_many_requests", "setting": "out_per_launch", "value": 0}
    assert (await launched(deps, tool, dana)).result == [refused] * 3
    assert gatekeeper.asked == []
    await own_settings(session_factory, tool, out_per_launch=2)
    came = (await launched(deps, tool, dana)).result
    assert [answer.get("refused") for answer in came] == [None, None, "too_many_requests"]  # type: ignore[union-attr]
    assert len(gatekeeper.asked) == 2


async def test_an_answer_up_to_out_bytes_reaches_the_program_at_the_start(
    session_factory, live_clock, dana
):
    # I4 (ruling R21): out_bytes (1 MB at the start) means what it says, though result_bytes is
    # 64 KB: the answer of a request out is framed by the larger of the two.
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")

    async def reads_its_size(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            read = await ask(GET)
        except FakeRefused as refused:
            return {"refused": refused.reason, "setting": refused.setting, "value": refused.value}
        return len(read["body"])

    gatekeeper = FakeOut(b"x" * 200_000)
    deps = going_out(session_factory, live_clock, {"lights": reads_its_size}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == 200_000
    assert gatekeeper.asked[0].most_bytes == 1_048_576
    # The run is told it may read that much of one answer (the sandbox's guard of the channel).
    assert deps.sandbox.runs[-1].answer_bytes == 1_048_576  # type: ignore[attr-defined]
    # Over out_bytes, the gatekeeper stops reading: too_big, naming out_bytes.
    gatekeeper.body = b"x" * (1_048_576 + 1)
    assert (await launched(deps, tool, dana)).result == {
        "refused": "too_big",
        "setting": "out_bytes",
        "value": 1_048_576,
    }


async def test_an_answer_the_frame_cannot_hold_is_too_big_and_the_run_goes_on(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    # The frame of an answer out is the larger of out_bytes and result_bytes: here out_bytes.
    await own_settings(session_factory, tool, out_bytes=2000, result_bytes=1000)
    # 1900 bytes fit the frame as they are read, not once in base64.
    gatekeeper = FakeOut(b"\xff" * 1900)
    deps = going_out(session_factory, live_clock, {"lights": sending(GET, GET)}, gatekeeper)
    came = await launched(deps, tool, dana)
    assert came.status == "done"
    assert came.result == [{"refused": "too_big", "setting": "out_bytes", "value": 2000}] * 2
    assert gatekeeper.asked[0].most_bytes == 2000
    # Past it as it is read, the gatekeeper stops reading: out_bytes is named.
    gatekeeper.body = b"x" * 2001
    assert (await launched(deps, tool, dana)).result == [
        {"refused": "too_big", "setting": "out_bytes", "value": 2000}
    ] * 2
    # A larger result_bytes is the frame: it is the one named (1000 bytes read, which JSON
    # writes as 6000).
    await own_settings(session_factory, tool, out_bytes=1000, result_bytes=2000)
    gatekeeper.body = b"\x01" * 1000
    assert (await launched(deps, tool, dana)).result == [
        {"refused": "too_big", "setting": "result_bytes", "value": 2000}
    ] * 2
    assert gatekeeper.asked[-1].most_bytes == 1000


# --- the zone of a name (Review Focus 1, at the risk) ---------------------------------------------


async def test_a_house_name_the_gatekeeper_finds_on_the_internet_is_refused_and_nothing_sent(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "printer.local", 80)
    gatekeeper = FakeOut(zones={"printer.local": "internet"})
    ask = {**GET, "url": "http://printer.local/status"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == [{"refused": "risk_not_declared"}]
    # The zone was asked first, and nothing was sent.
    assert [request.resolve_only for request in gatekeeper.asked] == [True]
    assert gatekeeper.sent == []
    [done] = await audit_of(session_factory, OUT_DONE)
    assert (done.details["refused"], done.details["zone"]) == ("risk_not_declared", "internet")
    # An operation that says it leaves the house sends it, held to the zone found first.
    gatekeeper.asked.clear()
    deps = going_out(
        session_factory, live_clock, {"lights": sending(ask, operation="fetch")}, gatekeeper
    )
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    assert [(r.resolve_only, r.expect_zone) for r in gatekeeper.asked] == [
        (True, None),
        (False, "internet"),
    ]


async def test_a_name_that_leads_elsewhere_once_judged_is_refused_zone_changed(
    session_factory, live_clock, dana
):
    # Ruling R12: the house at the look, the internet at the send. The gatekeeper refuses it on
    # the lookup the connection goes to: nothing is sent, and no answer is weighed.
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "printer.local", 80)
    gatekeeper = FakeOut(moved={"printer.local": "internet"})
    ask = {**GET, "url": "http://printer.local/status"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == [{"refused": "zone_changed"}]
    assert [(r.resolve_only, r.expect_zone) for r in gatekeeper.asked] == [
        (True, None),
        (False, "house"),
    ]
    [done] = await audit_of(session_factory, OUT_DONE)
    assert (done.details["refused"], done.details["status"]) == ("zone_changed", None)
    assert (done.details["bytes_in"], done.details["zone"]) == (0, "house")


async def test_a_port_of_the_host_machine_is_sent_expecting_the_host(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "192.168.1.2", PORT, on_host=True)
    gatekeeper = FakeOut(zones={"192.168.1.2": "host"})
    ask = {**GET, "url": f"http://192.168.1.2:{PORT}/api"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    assert [(r.resolve_only, r.expect_zone) for r in gatekeeper.asked] == [(False, "host")]


async def test_a_house_name_found_on_the_internet_is_judged_as_the_internet(
    session_factory, live_clock, dana, grant
):
    # Ruling R14: a name is looked up before the barrier, and judged by where it leads: an
    # owner's rule on the zone sees it, though the operation declares outward.
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "printer.local", 80)
    await grant(
        "program.out",
        person=dana,
        limit='action.facts.zone == "internet" ? "refuse" : "allow"',
    )
    gatekeeper = FakeOut(zones={"printer.local": "internet"})
    ask = {**GET, "url": "http://printer.local/status"}
    deps = going_out(
        session_factory, live_clock, {"lights": sending(ask, operation="fetch")}, gatekeeper
    )
    assert (await launched(deps, tool, dana)).result == [{"refused": "limit"}]
    assert gatekeeper.sent == []
    [decision] = [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == "program.out"
    ]
    assert decision.details["facts"]["zone"] == "internet"
    assert decision.details["decision"] == "refuse"


async def test_a_lookup_refused_is_the_request_s_refusal_and_audited(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "printer.local", 80)
    gatekeeper = FakeOut(refuse_lookup="never_reachable")
    ask = {**GET, "url": "http://printer.local/status"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == [{"refused": "never_reachable"}]
    assert gatekeeper.sent == []
    [done] = await audit_of(session_factory, OUT_DONE)
    assert done.details["refused"] == "never_reachable"


async def test_a_host_port_open_on_the_host_says_nothing_of_another_port(
    session_factory, live_clock, dana
):
    # The row of the host machine matches on its host and its port, like any open address.
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "192.168.1.2", PORT, on_host=True)
    await opened(session_factory, "lights", "192.168.1.2", PORT + 1)
    gatekeeper = FakeOut()
    ask = {**GET, "url": f"http://192.168.1.2:{PORT + 1}/api"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    assert [request.expect_zone for request in gatekeeper.asked] == ["house"]


async def test_an_internet_address_under_an_operation_that_stays_home_is_refused_at_once(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "api.example.com", 443)
    gatekeeper = FakeOut(zones={"api.example.com": "internet"})
    ask = {**GET, "url": "https://api.example.com/x"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask)}, gatekeeper)
    assert (await launched(deps, tool, dana)).result == [{"refused": "risk_not_declared"}]
    # Looked up (ruling R16), never sent.
    assert gatekeeper.sent == []


@pytest.mark.parametrize("operation", ["read_state", "fetch"])
async def test_a_host_name_opened_as_an_ordinary_address_is_host_not_opened(
    session_factory, live_clock, dana, operation
):
    # Ruling R16: the lookup's own refusal tells Domo what is true, whatever the operation
    # declares; never risk_not_declared on how the name reads.
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "host.docker.internal", 18996)
    gatekeeper = FakeOut(refuse_lookup="host_not_opened")
    ask = {**GET, "url": "http://host.docker.internal:18996/api"}
    deps = going_out(
        session_factory, live_clock, {"lights": sending(ask, operation=operation)}, gatekeeper
    )
    assert (await launched(deps, tool, dana)).result == [{"refused": "host_not_opened"}]
    assert [request.resolve_only for request in gatekeeper.asked] == [True]
    assert gatekeeper.sent == []
    [done] = await audit_of(session_factory, OUT_DONE)
    assert done.details["refused"] == "host_not_opened"


# --- a dry run -----------------------------------------------------------------------------------


async def test_a_host_machine_port_under_a_name_of_the_internet_is_the_house(
    session_factory, live_clock, dana
):
    # Fix round 2: a name its literal calls the internet, opened as a port of the machine that
    # hosts Domo, is the house: an ordinary operation reaches it, held to the host's zone.
    docker = "host.docker.internal"
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", docker, PORT, on_host=True)
    gatekeeper = FakeOut(zones={docker: "host"})
    ask = {**GET, "url": f"http://{docker}:{PORT}/api"}
    other = {**GET, "url": f"http://{docker}:{PORT + 1}/api"}
    deps = going_out(session_factory, live_clock, {"lights": sending(ask, other)}, gatekeeper)
    came = (await launched(deps, tool, dana)).result
    assert came[0]["status"] == 200  # type: ignore[index]
    # The same name at a port not opened is closed, whatever its literal says.
    assert came[1] == {"refused": "closed_address"}  # type: ignore[index]
    assert [(r.resolve_only, r.expect_zone) for r in gatekeeper.asked] == [(False, "host")]
    [decision] = [
        entry
        for entry in await audit_of(session_factory, "policy.decision")
        if entry.details["action_type"] == "program.out"
    ]
    assert decision.details["facts"]["zone"] == "house"


async def test_a_dry_run_lets_a_host_machine_port_through_but_not_the_internet(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights", "host.docker.internal", PORT, on_host=True)
    scope = LaunchScope(
        tool_name="lights",
        tool_id=tool.id,
        for_whom=ForWhom(dana),
        actor=actor_of(dana),
        depth=0,
        chain=Chain(),
        waited=False,
        path=("lights",),
        dry_run=True,
        operations=read_operations(OPERATIONS),
    )
    gatekeeper = FakeOut()
    means = Means(going_out(session_factory, live_clock, {}, gatekeeper), scope)
    assert await means.answer(BEGIN) == {"ok": None}
    host_port = {**GET, "url": f"http://host.docker.internal:{PORT}/api"}
    assert await means.answer(host_port) == {"ok": None}
    other_port = {**GET, "url": f"http://host.docker.internal:{PORT + 1}/api"}
    assert (await means.answer(other_port) or {}).get("refused") == "risk_not_declared"
    assert gatekeeper.asked == []


async def test_a_dry_run_sends_nothing_checks_no_address_nor_account_and_answers_none(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    gatekeeper = FakeOut()
    scope = LaunchScope(
        tool_name="lights",
        tool_id=tool.id,
        for_whom=ForWhom(dana),
        actor=actor_of(dana),
        depth=0,
        chain=Chain(),
        waited=False,
        path=("lights",),
        dry_run=True,
        operations=read_operations(OPERATIONS),
    )
    means = Means(going_out(session_factory, live_clock, {}, gatekeeper), scope)
    # Nothing but a read before an operation begins, as for real.
    assert (await means.answer(GET) or {}).get("refused") == "no_operation"
    assert await means.answer(BEGIN) == {"ok": None}
    # No address is open and no account exists: neither is checked.
    assert await means.answer(GET) == {"ok": None}
    assert await means.answer(with_account("nothing", "POST")) == {"ok": None}
    # Its form is checked as for real, and what would always be refused is refused alike.
    assert (await means.answer({**GET, "method": "TRACE"}) or {}).get("refused") == "not_valid"
    internet = {**GET, "url": "https://api.example.com/"}
    assert (await means.answer(internet) or {}).get("refused") == "risk_not_declared"
    assert gatekeeper.asked == []
    assert await audit_of(session_factory, "policy.decision", OUT_DONE) == []


# --- what the audit and the program never get -----------------------------------------------------


async def test_the_audit_lines_hold_no_path_query_header_nor_body(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    ask = {
        "do": "out",
        "method": "PUT",
        "url": f"http://{LIGHTS}:{PORT}/api/PATHPART?q=QUERYPART",
        "headers": {"x-trace": "HEADERVALUE"},
        "body": "BODYTEXT",
    }
    gatekeeper = FakeOut(b"ANSWERTEXT")
    deps = going_out(
        session_factory, live_clock, {"lights": sending(ask, operation="switch")}, gatekeeper
    )
    assert (await launched(deps, tool, dana)).result[0]["status"] == 200  # type: ignore[index]
    [done] = await audit_of(session_factory, OUT_DONE)
    assert (done.details["method"], done.details["bytes_out"]) == ("PUT", len("BODYTEXT"))
    audited = json.dumps([entry.details for entry in await all_audit(session_factory)])
    for word in ("PATHPART", "QUERYPART", "HEADERVALUE", "x-trace", "BODYTEXT", "ANSWERTEXT"):
        assert word not in audited


async def test_an_error_text_of_the_gatekeeper_never_reaches_the_program(
    session_factory, live_clock, dana, respx_mock, gk_client: GatekeeperClient
):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    respx_mock.post("http://gatekeeper.test/internal/out").respond(
        500, json={"code": "the site said: SECRETWORD", "error": "SECRETWORD"}
    )
    met: list[Any] = []

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask(BEGIN)
        try:
            await ask(GET)
        except FakeRefused as refused:
            met.append((refused.reason, refused.detail))
        return None

    deps = going_out(session_factory, live_clock, {"lights": program}, gk_client)
    assert (await launched(deps, tool, dana)).status == "done"
    assert met == [("failed", None)]
    audited = json.dumps([entry.details for entry in await all_audit(session_factory)])
    assert "SECRETWORD" not in audited and CORE_TOKEN not in audited


# --- a yes (spec 5.8) ----------------------------------------------------------------------------


async def test_an_out_in_a_costing_operation_waits_for_the_yes_and_is_sent_once(
    session_factory, live_clock, dana
):
    tool = await add_tool(session_factory, "pizza", OPERATIONS)
    await opened(session_factory, "pizza", "api.example.com", 443)
    gatekeeper = FakeOut()
    order = {"do": "out", "method": "POST", "url": "https://api.example.com/order", "body": "{}"}

    async def program(request: RunRequest, ask: FakeAsk) -> Any:
        await ask({"do": "begin", "operation": "order", "amount_cents": 3250})
        return await ask(order)

    deps = going_out(session_factory, live_clock, {"pizza": program}, gatekeeper)
    for_dana = ForWhom(dana)
    arguments: JsonObject = {"n": 2}
    came = await launch(deps, tool.id, arguments=arguments, for_whom=for_dana, cause="trigger")
    # The run stopped at its begin: nothing was sent.
    assert came.status == "stopped" and gatekeeper.asked == []
    [stop] = await stops(session_factory)
    yes = await taken(session_factory, stop.id, tool.id, dana, arguments)
    assert yes is not None
    came = await launch(
        deps, tool.id, arguments=arguments, for_whom=for_dana, cause="trigger", yes=yes
    )
    assert came.status == "done" and came.result["status"] == 200  # type: ignore[index]
    assert [request.method for request in gatekeeper.sent] == ["POST"]
    # Used once: a launch without it stops again, and sends nothing.
    came = await launch(deps, tool.id, arguments=arguments, for_whom=for_dana, cause="trigger")
    assert came.status == "stopped" and len(gatekeeper.sent) == 1


async def test_without_a_gatekeeper_nothing_goes_out(session_factory, live_clock, dana):
    tool = await add_tool(session_factory, "lights", OPERATIONS)
    await opened(session_factory, "lights")
    deps = going_out(session_factory, live_clock, {"lights": sending(GET)}, None)
    assert (await launched(deps, tool, dana)).result == [{"refused": "unreachable"}]
    assert means_module.OUT_DONE == "madetool.out_done"
