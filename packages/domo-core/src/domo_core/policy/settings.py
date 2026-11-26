"""The owner's policy settings (plan decision D8): how long a request may wait, the rights
writer's time, the household's time zone (spec 5.5), and the rules of the house on the keys
(0.6.0, spec 4.3).

The defaults live in the code (``SETTING_DEFAULTS``). The ``policy_settings`` table holds only
what the owner has chosen: no row means "the default". The settings are read again at every
decision (``load_policy_settings``), so a change also applies to the requests already waiting.

Whether an action is confirmed, and how, is no setting: the yes-judge decides it
(``confirmations.judge``), and the owner's rules ("always ask me before...") are rights
(``policy.rights``). The keys:

* ``confirmation_max_lifetime_hours``: how long a request may wait before it expires by itself,
  from 1 to 720 hours;
* ``rights_writer.seconds``: how long each call to the rights writer (``policy.rights_writer``)
  may take, from 30 to 240 seconds, 60 by default. The owner can give it more when the writer
  runs out of time (``tools.rights_tools`` notes it, and Domo may suggest it);
* ``household.time_zone``: the household's time zone, an IANA name (``Europe/Paris``), in which a
  limit reads the hour and the day (``policy.facts``). It has no default: until the owner sets it,
  a limit cannot read the hour, and Domo asks the owner before writing one that would. A name is
  accepted when ``zoneinfo`` knows it (``zoneinfo.available_timezones``, then ``ZoneInfo``);
* ``keys.invitee_default``: what an invitee uses when their invitation does not say (``inviter``:
  the keys of whoever invited them; ``own``: a key of their own). Without it, their inviter's
  (``household.service.accept_invitation``);
* ``keys.invitee_general``: whether invitees who join from now on use the general key when their
  host does (``follow``), or never, whatever their invitation says (``never``). Without it,
  ``follow`` (``household.service.accept_invitation``);
* ``keys.upkeep``: whether the household's general key serves, for a few small calls, to ask
  someone who has no key Domo may use for them to give one (``conversation.prepare``, the upkeep
  turn), ``true`` or ``false``. Without it, it does;
* ``house.language``: the language of the house, a language tag in its usual case
  (``domo_common.text_rules.language_tag``: ``fr``, ``pt-BR``), never a name: the writer and
  the gatekeeper read it. Without it, ``en``. The owner's own language moves it
  (``tools.text_tools``);
* ``tools.base_mb``: how big the tools' own database may grow (``toolsdb``), a whole number of MB
  from 16 to 1048576. Without it, 1024 (1 GB): beyond, a program's write is refused;
* ``house.domo_name``: the name the whole house calls the assistant, one plain line of 1 to 40
  characters (``naming.is_domo_name``); without it, "Domo". The owner alone changes it, in a turn
  of their own, and never ``policy_update`` (``NAME_SETTINGS``);
* ``house.domo_name_before``: the house's name before the last change, kept for the notice of the
  change (same rules as ``house.domo_name``).

The rules of the keys are written by ``keys_house`` (``tools.keys_house``: a right of its own,
the owner's by default, which can be given), never by ``policy_update`` (``KEYS_SETTINGS``).

The functions here do not commit and write no audit entry: the caller (the ``policy_update``
tool, or ``keys_house``) does both.
"""

import functools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from domo_common.text_rules import language_tag
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domo_core.naming import is_domo_name
from domo_core.policy.models import PolicySetting

logger = logging.getLogger(__name__)

SettingValue = str | int

LIFETIME_KEY = "confirmation_max_lifetime_hours"
TIME_ZONE_KEY = "household.time_zone"
WRITER_SECONDS_KEY: Final = "rights_writer.seconds"
INVITEE_DEFAULT_KEY: Final = "keys.invitee_default"
INVITEE_GENERAL_KEY: Final = "keys.invitee_general"
UPKEEP_KEY: Final = "keys.upkeep"
HOUSE_LANGUAGE: Final = "house.language"
TOOLS_BASE_KEY: Final = "tools.base_mb"
# The rules of the house on the keys: ``keys_house``'s to write.
KEYS_SETTINGS: Final = frozenset({INVITEE_DEFAULT_KEY, INVITEE_GENERAL_KEY, UPKEEP_KEY})
# The house's name for the assistant, and the one it had before: the owner's alone to change.
HOUSE_DOMO_NAME: Final = "house.domo_name"
HOUSE_DOMO_NAME_BEFORE: Final = "house.domo_name_before"
NAME_SETTINGS: Final = frozenset({HOUSE_DOMO_NAME, HOUSE_DOMO_NAME_BEFORE})

# What an invitee uses when their invitation does not say: their inviter's keys, or their own.
InviteeDefault = Literal["inviter", "own"]
INVITEE_DEFAULTS: Final[tuple[InviteeDefault, ...]] = ("inviter", "own")
# Whether an invitee who joins uses the general key when their host does, or never.
InviteeGeneral = Literal["follow", "never"]
INVITEE_GENERALS: Final[tuple[InviteeGeneral, ...]] = ("follow", "never")
_DEFAULT_LIFETIME_HOURS = 168
# The longest a request may wait is 30 days, the shortest an hour.
_LIFETIME_HOURS_RANGE = range(1, 721)
_DEFAULT_WRITER_SECONDS = 60
_DEFAULT_HOUSE_LANGUAGE: Final = "en"
# Each call to the rights writer has between half a minute and four minutes.
_WRITER_SECONDS_RANGE = range(30, 241)
# The tools' database holds 1 GB unless the owner says otherwise: from 16 MB to 1 TB, in MB.
DEFAULT_TOOLS_BASE_MB: Final = 1024
_TOOLS_BASE_MB_RANGE = range(16, 1_048_577)

SETTING_DEFAULTS: Mapping[str, SettingValue] = MappingProxyType(
    {LIFETIME_KEY: _DEFAULT_LIFETIME_HOURS, WRITER_SECONDS_KEY: _DEFAULT_WRITER_SECONDS}
)


class InvalidSetting(Exception):
    """A key or a value the policy settings do not accept."""


def _kind_of(
    key: str,
) -> Literal[
    "lifetime",
    "writer_seconds",
    "time_zone",
    "invitee_default",
    "invitee_general",
    "upkeep",
    "house_language",
    "tools_base",
    "domo_name",
]:
    """What the key holds; ``InvalidSetting`` if it is not a setting (the styles of before
    included: Domo judges the yes)."""
    if key == TOOLS_BASE_KEY:
        return "tools_base"
    if key == LIFETIME_KEY:
        return "lifetime"
    if key == TIME_ZONE_KEY:
        return "time_zone"
    if key == WRITER_SECONDS_KEY:
        return "writer_seconds"
    if key == INVITEE_DEFAULT_KEY:
        return "invitee_default"
    if key == INVITEE_GENERAL_KEY:
        return "invitee_general"
    if key == UPKEEP_KEY:
        return "upkeep"
    if key == HOUSE_LANGUAGE:
        return "house_language"
    if key in NAME_SETTINGS:
        return "domo_name"
    raise InvalidSetting("unknown setting")


def _is_whole_number_in(value: object, allowed: range) -> bool:
    """Whether ``value`` is a whole number inside ``allowed``. ``bool`` is an ``int`` to Python,
    and a JSON true is not a number of hours or of seconds."""
    return isinstance(value, int) and not isinstance(value, bool) and value in allowed


def _check(key: str, value: object) -> None:
    kind = _kind_of(key)
    if value is None:
        return
    if kind == "time_zone":
        if not isinstance(value, str) or not _is_time_zone(value):
            raise InvalidSetting("a time zone is the name of one, such as Europe/Paris")
        return
    if kind == "writer_seconds":
        if not _is_whole_number_in(value, _WRITER_SECONDS_RANGE):
            raise InvalidSetting("the writer's time is a whole number of seconds, from 30 to 240")
        return
    if kind == "lifetime":
        if not _is_whole_number_in(value, _LIFETIME_HOURS_RANGE):
            raise InvalidSetting("a lifetime is a whole number of hours, from 1 to 720")
        return
    if kind == "invitee_default":
        if not isinstance(value, str) or value not in INVITEE_DEFAULTS:
            raise InvalidSetting('what an invitee uses by default is "inviter" or "own"')
        return
    if kind == "invitee_general":
        if not isinstance(value, str) or value not in INVITEE_GENERALS:
            raise InvalidSetting('what an invitee does with the general key is "follow" or "never"')
        return
    if kind == "upkeep":
        if not isinstance(value, bool):
            raise InvalidSetting("whether the general key serves the upkeep is true or false")
        return
    if kind == "tools_base":
        if not _is_whole_number_in(value, _TOOLS_BASE_MB_RANGE):
            raise InvalidSetting(
                "the size of the tools' database is a whole number of MB, from 16 to 1048576"
            )
        return
    if kind == "house_language":
        # A tag in its usual case, never a name: it reaches the writer and the gatekeeper.
        if not isinstance(value, str) or language_tag(value) != value:
            raise InvalidSetting("the house's language is a language tag, such as fr or pt-BR")
        return
    if kind == "domo_name":
        if not is_domo_name(value):
            raise InvalidSetting("the assistant's name is one plain line of 1 to 40 characters")
        return


@functools.cache
def _time_zone_names() -> frozenset[str]:
    """The names of the time zones ``zoneinfo`` knows (read once: it lists files)."""
    return frozenset(available_timezones())


def _is_time_zone(name: str) -> bool:
    """Whether ``name`` is the name of a time zone that can be loaded. Only a listed name is tried:
    a path, a directory or a name with spaces around it is not one."""
    if name not in _time_zone_names():
        return False
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return False
    return True


def validate_setting(key: str, value: SettingValue | None) -> None:
    """Raise ``InvalidSetting`` unless ``key`` is a setting and ``value`` is what it accepts.

    ``None`` is always accepted for a setting: it clears the owner's choice.
    """
    _check(key, value)


def _accepted(key: str, value: object) -> bool:
    try:
        _check(key, value)
    except InvalidSetting:
        return False
    return True


@dataclass(frozen=True)
class PolicySettings:
    """The owner's choices (``owner_values``, by key) on top of the defaults.

    A value that the rules do not accept (``validate_setting``) is treated as no choice at all:
    the default applies. This can only happen if someone changed the table by hand.
    """

    owner_values: Mapping[str, SettingValue]

    def _chosen(self, key: str) -> SettingValue | None:
        value = self.owner_values.get(key)
        return value if value is not None and _accepted(key, value) else None

    @property
    def max_lifetime(self) -> timedelta:
        """How long a request may wait before it expires by itself."""
        hours = self._chosen(LIFETIME_KEY)
        return timedelta(hours=_DEFAULT_LIFETIME_HOURS if hours is None else cast(int, hours))

    @property
    def writer_seconds(self) -> int:
        """How long each call to the rights writer may take, in seconds."""
        seconds = self._chosen(WRITER_SECONDS_KEY)
        return _DEFAULT_WRITER_SECONDS if seconds is None else cast(int, seconds)

    @property
    def time_zone(self) -> ZoneInfo | None:
        """The household's time zone, ``None`` until the owner sets it (no default: it is never
        guessed)."""
        name = self._chosen(TIME_ZONE_KEY)
        return None if name is None else ZoneInfo(str(name))

    @property
    def invitee_default(self) -> InviteeDefault | None:
        """What an invitee uses when their invitation does not say, ``None`` when nobody said
        (their inviter's keys, then: ``household.service.accept_invitation``)."""
        chosen = self._chosen(INVITEE_DEFAULT_KEY)
        for known in INVITEE_DEFAULTS:
            if chosen == known:
                return known
        return None

    @property
    def invitee_general(self) -> InviteeGeneral | None:
        """Whether an invitee who joins uses the general key when their host does (``follow``) or
        never (``never``); ``None`` when nobody said (``follow``, then)."""
        chosen = self._chosen(INVITEE_GENERAL_KEY)
        for known in INVITEE_GENERALS:
            if chosen == known:
                return known
        return None

    @property
    def upkeep_on_general(self) -> bool:
        """Whether the general key serves to ask someone without a key for one (it does, unless
        it was said otherwise)."""
        chosen = self._chosen(UPKEEP_KEY)
        return True if chosen is None else bool(chosen)

    @property
    def house_language(self) -> str:
        """The language of the house, the one a visitor without a language of their own is
        written to in (``en`` when nobody said)."""
        chosen = self._chosen(HOUSE_LANGUAGE)
        return _DEFAULT_HOUSE_LANGUAGE if chosen is None else str(chosen)

    @property
    def house_domo_name(self) -> str | None:
        """The name the house calls the assistant, ``None`` when nobody said ("Domo", then)."""
        chosen = self._chosen(HOUSE_DOMO_NAME)
        return None if chosen is None else str(chosen)

    @property
    def house_domo_name_before(self) -> str | None:
        """The house's name before the last change, ``None`` when there was none."""
        chosen = self._chosen(HOUSE_DOMO_NAME_BEFORE)
        return None if chosen is None else str(chosen)

    @property
    def tools_base_bytes(self) -> int:
        """How big the tools' database may grow, in bytes (1 GB when nobody said): beyond, a
        program's write is refused (``toolsdb.store``)."""
        chosen = self._chosen(TOOLS_BASE_KEY)
        megabytes = DEFAULT_TOOLS_BASE_MB if chosen is None else cast(int, chosen)
        return megabytes * 1024 * 1024

    def rows(self) -> list[dict[str, SettingValue]]:
        """What applies now, one row ``{"key", "value", "source"}`` per setting (the lifetime of
        a request, then the writer's time), then the household's time zone and the rules of the
        keys, each once it was set. ``source`` is ``owner`` for a choice, ``default`` if none."""
        rows: list[dict[str, SettingValue]] = []
        for key, default in SETTING_DEFAULTS.items():
            chosen = self._chosen(key)
            rows.append(
                {
                    "key": key,
                    "value": default if chosen is None else chosen,
                    "source": "default" if chosen is None else "owner",
                }
            )
        for key in (
            TIME_ZONE_KEY,
            INVITEE_DEFAULT_KEY,
            INVITEE_GENERAL_KEY,
            UPKEEP_KEY,
            HOUSE_LANGUAGE,
            TOOLS_BASE_KEY,
            HOUSE_DOMO_NAME,
        ):
            chosen = self._chosen(key)
            if chosen is not None:
                rows.append({"key": key, "value": chosen, "source": "owner"})
        return rows


async def load_policy_settings(session: AsyncSession) -> PolicySettings:
    """The owner's choices, as they are in the database now.

    A row that the rules do not accept is left out (and logged): it must not break every
    decision, and it must not loosen anything.
    """
    result = await session.execute(select(PolicySetting.key, PolicySetting.value))
    values: dict[str, SettingValue] = {}
    for key, value in result:
        if value is None:
            continue
        if not _accepted(key, value):
            logger.warning("Ignored the policy setting %s: its value is not accepted", key)
            continue
        values[key] = cast(SettingValue, value)
    return PolicySettings(MappingProxyType(values))


# How many times a change tries again when the row it met is removed before it could lock it (a
# clear of the same key, committed in between): once is already rare, three is plenty.
_WRITE_ATTEMPTS = 3


async def update_policy_setting(
    session: AsyncSession, *, key: str, value: SettingValue | None, by: UUID, now: datetime
) -> tuple[SettingValue | None, SettingValue | None]:
    """Set the owner's choice for ``key`` to ``value``, or clear it when ``value`` is ``None``.

    Returns the owner's choice before and after (``None``: no choice, the default applies), for
    the caller's audit entry. ``InvalidSetting`` if the key or the value is not accepted, and
    nothing is written then. Nothing is committed and no audit entry is written.

    Two changes of one key at the same time never both report the same old value, and never
    fail: a choice is first inserted if there is none (``INSERT ... ON CONFLICT DO NOTHING``,
    which waits for a change of that key that is not committed yet); when there is one, its row
    is locked while it is read, then changed. Clearing locks the row, if there is one, and removes
    it.
    """
    validate_setting(key, value)
    if value is None:
        row = await _locked_row(session, key)
        if row is None:
            return None, None
        old = _old_value(key, row)
        await session.delete(row)
        await session.flush()
        return old, None
    for _ in range(_WRITE_ATTEMPTS):
        inserted = await session.execute(
            pg_insert(PolicySetting)
            .values(key=key, value=value, updated_by=by, updated_at=now)
            .on_conflict_do_nothing(index_elements=[PolicySetting.key])
            .returning(PolicySetting.key)
        )
        if inserted.scalar_one_or_none() is not None:
            return None, value
        row = await _locked_row(session, key)
        if row is None:
            # Cleared since the insert met it: there is room for this choice now.
            continue
        old = _old_value(key, row)
        row.value = value
        row.updated_by = by
        row.updated_at = now
        await session.flush()
        return old, value
    raise RuntimeError(f"the policy setting {key} kept changing while it was being written")


async def _locked_row(session: AsyncSession, key: str) -> PolicySetting | None:
    """The row of ``key``, locked until the end of the transaction, as the database has it now."""
    result = await session.execute(
        select(PolicySetting)
        .where(PolicySetting.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _old_value(key: str, row: PolicySetting) -> SettingValue | None:
    """The choice a row holds, or ``None`` when the rules do not accept it (no choice)."""
    return cast(SettingValue, row.value) if _accepted(key, row.value) else None
