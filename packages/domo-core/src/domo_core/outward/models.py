"""The tables of going out (step 2, spec 13.3, 13.4, 13.10).

``open_addresses`` keeps the addresses a program may reach: a host and a port, for one tool
(``tool_name``: it outlives the tool's versions) or for a program run by hand (``None``), and
whether it is a port of the machine that hosts Domo (``on_host``, a right apart). Who opened it
and who closed it is ``"domo"`` or a person's id (``WHO_RULE``), and whether whoever closed it was
the owner then (``closed_by_owner``: kept on the row, so that it outlives a change of owner; what
the owner closed stays closed to anyone but the owner, ``outward.service``). A row is never changed
back: an address opened again is a new row, so that the last closing, and whose it was, stays
known. One live row (not closed) per tool, host and port.

``service_accounts`` keeps the accounts whose secret the gatekeeper's vault holds, by reference
(``ref``, ``account.<uuid hex>``: a namespace of their own, never a provider key's): a ``name``
Domo gave it, whose it is (``person_id``; ``None``, the house's), the hosts it is bound to and
where it is placed in a request (``place``: the place as the vault keeps it, ``domo_common.
gatekeeper_api.InjectionLocation``: its ``kind``, a header's name and template, or a login's path,
encoding and field names; never a value), and who gave it. A row is made when Domo asks for the
account, before its form is filled: the vault then holds its secret, or not yet. A house account
serves a guest only when the owner opened it to guests (``for_guests``), and Domo on his own
unless the owner closed it to him (``for_domo``, spec 13.5). A removed account is kept, marked
(``removed_at``, ``removed_by``, and ``removed_by_owner`` when the owner removed it, kept as for an
address): the vault cannot erase, so the core refuses it from then on; an account given again is a
new row. One live row per person (or the house) and name. Nothing here holds a secret.

The table is not ``accounts``: that name is the people's logins (``accounts.models.Account``).
"""

import uuid
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

# Who opened or closed an address, or removed an account: Domo, or a person by their id.
DOMO: Final = "domo"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def who_rule(column: str) -> str:
    """The check that ``column`` says ``"domo"`` or a person's id."""
    return f"{column} = '{DOMO}' OR {column} ~ '^{_UUID}$'"


# The name of an account, as the database checks it and the code.
ACCOUNT_NAME_PATTERN: Final = "^[a-z][a-z0-9_]{1,39}$"
# The reference of an account in the vault: ``account.`` and 32 hexadecimal digits.
ACCOUNT_REF_PREFIX: Final = "account."
ACCOUNT_REF_PATTERN: Final = "^account\\.[0-9a-f]{32}$"
# The kinds of place of an account in a request (``domo_common.gatekeeper_api.InjectionLocation``,
# whose ``kind`` the column ``place`` holds with the rest of the place).
AccountPlace = Literal["header", "basic", "login"]


def account_ref(account_id: uuid.UUID) -> str:
    """The vault's reference of an account whose id is ``account_id``."""
    return f"{ACCOUNT_REF_PREFIX}{account_id.hex}"


class OpenAddress(Base):
    """An address a program may reach (see the module docstring): ``host`` in lower case, as the
    tool writes it (a name or an IP literal), and ``port``. Live while ``closed_at`` is empty."""

    __tablename__ = "open_addresses"
    __table_args__ = (
        CheckConstraint(
            "char_length(host) BETWEEN 1 AND 253 AND host = lower(host)", name="host_format"
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port_range"),
        CheckConstraint(
            "tool_name IS NULL OR tool_name ~ '^[a-z][a-z0-9_]{0,39}$'", name="tool_name_format"
        ),
        CheckConstraint(who_rule("opened_by"), name="opened_by_format"),
        CheckConstraint(f"closed_by IS NULL OR {who_rule('closed_by')}", name="closed_by_format"),
        CheckConstraint("(closed_at IS NULL) = (closed_by IS NULL)", name="closed_with_whom"),
        CheckConstraint("NOT closed_by_owner OR closed_at IS NOT NULL", name="owner_closed_it"),
        # One live row per tool (or none: a program run by hand), host and port.
        Index(
            "uq_open_addresses_live",
            "tool_name",
            "host",
            "port",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_name: Mapped[str | None]
    host: Mapped[str]
    port: Mapped[int]
    on_host: Mapped[bool] = mapped_column(server_default=text("false"))
    opened_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    opened_by: Mapped[str]
    closed_at: Mapped[datetime | None]
    closed_by: Mapped[str | None]
    closed_by_owner: Mapped[bool] = mapped_column(server_default=text("false"), default=False)


class ServiceAccount(Base):
    """An account at a service (see the module docstring). ``hosts`` is the list of
    ``{"host", "port"}`` it is bound to; ``given_by`` the person who gave it in the form. A
    person's account goes with their row (spec 5.8); a house account stays when the row of whoever
    gave it goes, forgetting who gave it (``given_by`` empty)."""

    __tablename__ = "service_accounts"
    __table_args__ = (
        CheckConstraint(f"name ~ '{ACCOUNT_NAME_PATTERN}'", name="name_format"),
        CheckConstraint(f"ref ~ '{ACCOUNT_REF_PATTERN}'", name="ref_format"),
        CheckConstraint(
            "jsonb_typeof(hosts) = 'array' AND jsonb_array_length(hosts) >= 1",
            name="hosts_is_a_list",
        ),
        CheckConstraint(
            "jsonb_typeof(place) = 'object' "
            "AND coalesce(place ->> 'kind', '') IN ('header', 'basic', 'login')",
            name="place_values",
        ),
        CheckConstraint(
            f"removed_by IS NULL OR {who_rule('removed_by')}", name="removed_by_format"
        ),
        CheckConstraint("(removed_at IS NULL) = (removed_by IS NULL)", name="removed_with_whom"),
        CheckConstraint("NOT removed_by_owner OR removed_at IS NOT NULL", name="owner_removed_it"),
        # One live account per person (or the house) and name.
        Index(
            "uq_service_accounts_live",
            "person_id",
            "name",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_service_accounts_given_by", "given_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str]
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    ref: Mapped[str] = mapped_column(unique=True)
    hosts: Mapped[list[object]] = mapped_column(JSONB)
    place: Mapped[dict[str, object]] = mapped_column(JSONB)
    given_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    given_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    removed_at: Mapped[datetime | None]
    removed_by: Mapped[str | None]
    removed_by_owner: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    for_guests: Mapped[bool] = mapped_column(server_default=text("false"))
    for_domo: Mapped[bool] = mapped_column(server_default=text("true"))
