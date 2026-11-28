"""Which keys a person may use, in what order, and the route of a call: a key and a model.

A key is used only with the agreement of the one who gave it, the owner included (``allowed``):

* the one who gave a key may always use it;
* who else may is said by the lines of ``provider_users``: one for the person comes before one for
  « my invitees » (the people the giver invited), which comes before one for the whole household;
* a line lets someone use the key only when the giver wrote it, whoever else did is ignored; a
  line that takes it away counts whoever wrote it. The general key is the exception: its lines are
  the rules of the house (``tools.keys_house``, whose right is checked when a line is written), so
  each counts whoever wrote it (a delegate of the owner lets someone use it again);
* with no line that applies, the general key is the whole household's, guests included, and any
  other key is only its giver's;
* on the general key, an invitee has it only while their host does (a line for the invitee
  themselves comes first, and says yes or no as it is): the host's own lines and their host's
  count, up the chain, and a host who is not found or comes back round means no. A host who left
  is judged by these rules as if they were still there (only the person themself, once they left,
  has no key), so that whoever they invited keeps the general key; a line on the invitee is the
  way to take it away. An invitee never has more than their host.

The owner's rule "guests come with their own key" (``keys.guests_bring_own``) is read at each
turn, here (``usable_keys``): while it is set, someone visiting (``accounts.actors.is_visiting``)
uses no general key but one they gave themself, or one the owner himself let them use by a line
for them (his word for that person wins over his rule). Someone who lives here is never touched,
and nothing is written: once the rule is off, or they come to live here, the general key is
theirs again as the lines say. With no key left, they meet what anyone without a key meets.

The keys of a person come in this order (``usable_keys``): their own, then the ones others let
them use (of who invited them first), then the general key; the oldest first within each.

The key of a request (``choose_key``) is taken from them rank by rank (their own; the others';
the general key), never out of that order: in the first rank that has one, among its keys that
have a route for the conversation and a margin under their limits for its first call, the one
that lists the model Domo pinned for the conversation, else the one whose conversation is the
cheapest at the level wanted, the oldest at an equal price. The keys passed over at their limit
(no margin left for that call) are named (the context
of the turn says so); a key without a model for the conversation is passed over without a word.

The route of a call (``choose_route``) is the model ``choice.pick_model`` takes among the ones the
key lists, for a kind of task, at the level wanted (``wanted_level``): the level Domo chose for
that kind, else the kind's start, never under its minimum, raised a step at a time and at most
``strong``, and at least the level a request stepped up to (``at_least``). Once a call is raised
or stepped up, the model Domo pinned for its kind of task is left when it is under the level
wanted: a step up always means a stronger model. A route says the levels the key has for its kind
of task (``fit_levels``), so that a step up knows whether there is one above.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.accounts.actors import is_visiting
from domo_core.accounts.models import Person
from domo_core.llm.catalog import Catalog, Prices
from domo_core.llm.choice import candidates, fit_for, pick_model
from domo_core.llm.choices import Choices, load_choices
from domo_core.llm.levels import LEVELS, Level, level_rank
from domo_core.llm.models import ModelLevel, Provider, ProviderModel, ProviderUser
from domo_core.llm.tasks import TASKS, TaskKind
from domo_core.policy.settings import load_policy_settings

# The order in which the lines of access of a key apply: the most precise first.
_SCOPES = ("person", "invitees", "household")


def _applies(line: ProviderUser, provider: Provider, person: Person) -> bool:
    match line.scope:
        case "person":
            return line.person_id == person.id
        case "invitees":
            return person.invited_by is not None and person.invited_by == provider.given_by
        case "household":
            return True
        case _:
            return False


def allowed(
    provider: Provider,
    users: Sequence[ProviderUser],
    person: Person,
    *,
    people: Mapping[UUID, Person],
) -> bool:
    """Whether ``person`` may use the key ``provider``, whose lines of access are ``users`` (see
    the module docstring). ``people`` holds everybody in the household by id, the ones who left
    too: an invitee follows their host on the general key, a host who left being judged by the rules
    as if they were still there. Someone removed from the household may use none themself."""
    return _allowed(provider, users, person, people, frozenset())


def _allowed(
    provider: Provider,
    users: Sequence[ProviderUser],
    person: Person,
    people: Mapping[UUID, Person],
    seen: frozenset[UUID],
    *,
    is_host: bool = False,
) -> bool:
    # Someone who left uses no key, but a host walked through for an invitee is judged as present.
    if person.disabled_at is not None and not is_host:
        return False
    if provider.given_by == person.id:
        return True
    counted = [
        line
        for line in users
        if line.provider_id == provider.id
        and _applies(line, provider, person)
        and (not line.allowed or provider.is_general or line.set_by == provider.given_by)
    ]
    for scope in _SCOPES:
        decided = [line for line in counted if line.scope == scope]
        if decided and scope == "person":
            return decided[0].allowed
        if decided:
            result = decided[0].allowed
            break
    else:
        result = provider.is_general
    if not (result and provider.is_general and person.invited_by is not None):
        return result
    # On the general key, an invitee has it only while their host does (never more than them).
    host = people.get(person.invited_by)
    if host is None or host.id in seen:
        return False
    return _allowed(provider, users, host, people, seen | {person.id}, is_host=True)


async def household_people(session: AsyncSession) -> dict[UUID, Person]:
    """Everybody in the household by id, the ones who left too: the ``people`` of ``allowed``."""
    return {someone.id: someone for someone in (await session.execute(select(Person))).scalars()}


def _the_owner_lets(
    users: Sequence[ProviderUser], person: Person, people: Mapping[UUID, Person]
) -> bool:
    """Whether the owner himself wrote a line that lets ``person`` use the key whose lines are
    ``users``: his word for that person, which wins over his rule that guests bring their own
    key."""
    for line in users:
        writer = people.get(line.set_by)
        if (
            line.scope == "person"
            and line.person_id == person.id
            and line.allowed
            and writer is not None
            and writer.role == "owner"
        ):
            return True
    return False


async def usable_keys(session: AsyncSession, person: Person) -> list[Provider]:
    """The keys ``person`` may use, in order: their own, then those of others they may use (of
    who invited them first), then the general key; within each, the oldest first. The owner's
    rule that guests bring their own key is read here (see the module docstring)."""
    keys = (
        (await session.execute(select(Provider).order_by(Provider.created_at, Provider.id)))
        .scalars()
        .all()
    )
    lines: defaultdict[str, list[ProviderUser]] = defaultdict(list)
    for line in (await session.execute(select(ProviderUser))).scalars():
        lines[line.provider_id].append(line)
    people = await household_people(session)
    permitted = [key for key in keys if allowed(key, lines[key.id], person, people=people)]
    if is_visiting(person) and (await load_policy_settings(session)).guests_bring_own:
        permitted = [
            key
            for key in permitted
            if not key.is_general
            or key.given_by == person.id
            or _the_owner_lets(lines[key.id], person, people)
        ]
    own = [key for key in permitted if key.given_by == person.id]
    general = [key for key in permitted if key.is_general and key.given_by != person.id]
    others = [key for key in permitted if not key.is_general and key.given_by != person.id]
    # The one who invited them before the others (a stable sort keeps the oldest first).
    others.sort(key=lambda key: key.given_by != person.invited_by)
    return own + others + general


@dataclass(frozen=True)
class Route:
    """Where a call goes: the key ``provider`` and its model ``model_id``, of level ``level``
    (``None`` when it has none), at ``prices`` (``price_known`` is false when they are the
    highest of the provider, for want of a catalog entry), for the kind of task ``task``.
    ``fit_levels`` are the levels of the models of the key fit for that task (a model with no
    level counts as ``small``), so that a step up knows what is above."""

    provider: Provider
    model_id: str
    level: Level | None
    prices: Prices
    task: TaskKind
    price_known: bool = True
    fit_levels: frozenset[Level] = frozenset()


def wanted_level(task: TaskKind, choices: Choices, *, raised: int) -> Level:
    """The level a call of ``task`` asks for: the one Domo chose for it, else the task's start;
    never under the task's minimum; ``raised`` steps higher; at most ``strong``."""
    spec = TASKS[task]
    chosen = choices.tasks.get(task)
    start = spec.start if chosen is None or chosen.level is None else chosen.level
    rank = level_rank(start)
    if spec.minimum is not None:
        rank = max(rank, level_rank(spec.minimum))
    return LEVELS[min(rank + max(raised, 0), len(LEVELS) - 1)]


async def owner_levels(session: AsyncSession, provider_id: str) -> dict[str, Level]:
    """The levels the owner said for the models of the key ``provider_id``."""
    rows = await session.execute(
        select(ModelLevel.model_id, ModelLevel.level).where(ModelLevel.provider_id == provider_id)
    )
    levels: dict[str, Level] = {}
    for model_id, said in rows.all():
        for known in LEVELS:
            if known == said:
                levels[model_id] = known
    return levels


async def choose_route(
    session: AsyncSession,
    *,
    provider: Provider,
    task: TaskKind,
    catalog: Catalog,
    raised: int = 0,
    min_context: int = 0,
    at_least: Level | None = None,
) -> Route | None:
    """The route of a call of ``task`` on the key ``provider``: the model ``pick_model`` takes
    among the ones it lists (``provider_models``), described with ``catalog`` and the owner's
    levels, at the level ``wanted_level`` gives with the choices of that key (``load_choices``),
    and at least ``at_least`` (the level a request stepped up to), and the model Domo pinned
    for the task if it is fit, unless the call was raised or stepped up and it is under the level
    wanted. ``min_context`` is the size of the request, in estimated tokens. ``None`` when no
    model is fit (a protected task never takes one under its minimum)."""
    choices = await load_choices(session, provider.id)
    listed = await session.execute(
        select(ProviderModel.model_id)
        .where(ProviderModel.provider_id == provider.id)
        .order_by(ProviderModel.model_id)
    )
    described = candidates(
        catalog, provider, listed.scalars().all(), await owner_levels(session, provider.id)
    )
    chosen = choices.tasks.get(task)
    wanted = wanted_level(task, choices, raised=raised)
    if at_least is not None and level_rank(at_least) > level_rank(wanted):
        wanted = at_least
    fit = fit_for(described, task, min_context=min_context)
    pinned = None if chosen is None else chosen.model
    if pinned is not None and (raised > 0 or at_least is not None):
        # A step up means a stronger model: a pinned one under the level wanted is left.
        under = [
            c for c in fit if c.model_id == pinned and level_rank(c.level) < level_rank(wanted)
        ]
        if under:
            pinned = None
    picked = pick_model(described, task, wanted=wanted, pinned=pinned, min_context=min_context)
    if picked is None:
        return None
    return Route(
        provider=provider,
        model_id=picked.model_id,
        level=picked.level,
        prices=picked.prices,
        task=task,
        price_known=picked.price_known,
        fit_levels=frozenset(LEVELS[level_rank(c.level)] for c in fit),
    )


def key_label(provider: Provider, giver_name: str) -> str:
    """How a key is named to Domo: its kind and who gave it (``"openai key of Alex"``), and
    ``", general"`` for the household's general key. The name is a person's words: whoever puts
    it in a prompt gives it as data."""
    label = f"{provider.kind} key of {giver_name}"
    return f"{label}, general" if provider.is_general else label


@dataclass(frozen=True)
class KeyChoice:
    """The key of a request (``None`` when no key the person may use can take it), the keys
    passed over because they were at the end of a limit, in the order they were looked at, and
    the route the conversation found on the key chosen (``None`` with no key)."""

    key: Provider | None
    at_limit: tuple[Provider, ...] = ()
    route: Route | None = None


@dataclass(frozen=True)
class _Fit:
    key: Provider
    route: Route
    choices: Choices

    @property
    def lists_the_pinned_model(self) -> bool:
        chosen = self.choices.tasks.get("conversation")
        return chosen is not None and chosen.model == self.route.model_id

    def order(self) -> tuple[bool, Decimal]:
        """At the level wanted before any other, then the cheapest (input plus output)."""
        wanted = wanted_level("conversation", self.choices, raised=0)
        return (self.route.level != wanted, self.route.prices.input + self.route.prices.output)


def _ranks(person: Person, keys: Sequence[Provider]) -> list[list[Provider]]:
    """``keys`` (as ``usable_keys`` gives them) in their ranks: the person's own, the others'
    (of who invited them first), the general key; in their order within each."""
    own = [key for key in keys if key.given_by == person.id]
    general = [key for key in keys if key.is_general and key.given_by != person.id]
    others = [key for key in keys if not key.is_general and key.given_by != person.id]
    return [own, others, general]


async def choose_key(
    session: AsyncSession,
    person: Person,
    keys: Sequence[Provider],
    *,
    catalog: Catalog,
    at_limit: Callable[[Provider, Route], Awaitable[bool]],
    min_context: int = 0,
) -> KeyChoice:
    """The key of a request of ``person`` among ``keys``, the keys they may use in their order
    (``usable_keys``), rank by rank (see the module docstring). ``at_limit`` says whether a key
    has no margin left, under its limits for the person, for the first call of the request on
    the conversation's route found for it (``spending.at_limit``, with what that call may cost);
    ``min_context`` is the size of the request, in estimated tokens, for that route."""
    passed_over: list[Provider] = []
    for rank in _ranks(person, keys):
        fit: list[_Fit] = []
        for key in rank:
            route = await choose_route(
                session, provider=key, task="conversation", catalog=catalog, min_context=min_context
            )
            if route is None:
                continue
            if await at_limit(key, route):
                passed_over.append(key)
                continue
            fit.append(_Fit(key, route, await load_choices(session, key.id)))
        if fit:
            pinned = [one for one in fit if one.lists_the_pinned_model]
            # ``min`` keeps the first of equals: the oldest (of who invited them first).
            chosen = pinned[0] if pinned else min(fit, key=_Fit.order)
            return KeyChoice(chosen.key, tuple(passed_over), chosen.route)
    return KeyChoice(None, tuple(passed_over))
