"""The tables of the tools Domo makes (spec 5.2, 5.3, 5.5 to 5.8).

A made tool is a small program Domo wrote, kept as versions of its code; the code never runs in
the core (the sandbox runs it). Nothing here holds a secret. ``name`` is 1 to 40 characters of
``a-z``, ``0-9`` and ``_`` starting with a letter, and unique in the household; a removed tool
frees its name.

A tool is Domo's (the owner, 2026-11-18): it belongs to nobody of the household, and each
launch of it is made for someone, a person or the house (``madetools.launch.ForWhom``). A program
Domo runs by hand (``HandRun``) is launched the same way, and kept ``hand_runs_kept_days`` (a
setting of Domo's, ``madetools.settings``). A person's row takes
with it the triggers that launch a tool for them or that their return fires, their runs and the
programs run by hand for them (``ON DELETE CASCADE``); the tools stay. A build is never deleted
with a person: it forgets who opened it (``ON DELETE SET NULL``), as other people's plans may wait
on it (``plans.after_build_id`` restricts the deletion of a build)."""

import uuid
from datetime import date, datetime
from typing import Literal

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from domo_core.db import Base

Origin = Literal["domo", "project"]
ToolState = Literal["ready", "set_aside", "removed"]
VersionState = Literal["checking", "ready", "refused"]
TriggerKind = Literal["time", "arrival", "return"]
TriggerState = Literal["waiting", "done", "cancelled"]
BuildState = Literal["waiting", "building", "ready", "abandoned"]

# The name of a tool: what the database checks (``NAME_RULE``) and the code (``NAME_PATTERN``).
NAME_PATTERN = "^[a-z][a-z0-9_]{0,39}$"
NAME_RULE = f"name ~ '{NAME_PATTERN}'"
# The name a tool is offered to the model under: ``t_<name>``. No tool of the code starts with it.
OFFERED_PREFIX = "t_"


class MadeTool(Base):
    """A tool of Domo's, nobody's in particular: where it comes from (``origin``), the version in
    force (``current_version_id``), its ``operations`` (what it does, by name: Domo's to
    write) and how it has been going: the launches of the day (``launches_today`` on
    ``launches_day``), the times it woke Domo that day (``wakes_today``, on the same day) and the
    failures since the last success. ``settings`` holds the bounds Domo set for this tool alone,
    the time of a launch and the launches a day its build gave among them (``madetools.settings``:
    a key of ``PER_TOOL`` to a whole number; a key it does not hold follows the house's)."""

    __tablename__ = "made_tools"
    __table_args__ = (
        CheckConstraint(NAME_RULE, name="name_format"),
        CheckConstraint("jsonb_typeof(settings) = 'object'", name="settings_is_an_object"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 200", name="description_length"),
        CheckConstraint("origin IN ('domo', 'project')", name="origin_values"),
        CheckConstraint("state IN ('ready', 'set_aside', 'removed')", name="state_values"),
        CheckConstraint(
            "launches_today >= 0 AND wakes_today >= 0 AND failures_in_a_row >= 0",
            name="counts_positive",
        ),
        CheckConstraint(
            "aside_reason IS NULL OR char_length(aside_reason) BETWEEN 1 AND 300",
            name="aside_reason_length",
        ),
        # A name is taken until its tool is removed (``tool_remove`` keeps the row).
        Index(
            "uq_made_tools_name",
            "name",
            unique=True,
            postgresql_where=text("state <> 'removed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str]
    description: Mapped[str]
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB)
    origin: Mapped[Origin] = mapped_column(Text(), server_default=text("'domo'"))
    state: Mapped[ToolState] = mapped_column(Text(), server_default=text("'ready'"))
    operations: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tool_versions.id", ondelete="SET NULL", use_alter=True)
    )
    aside_reason: Mapped[str | None]
    launches_day: Mapped[date | None]
    launches_today: Mapped[int] = mapped_column(server_default=text("0"))
    wakes_today: Mapped[int] = mapped_column(server_default=text("0"))
    failures_in_a_row: Mapped[int] = mapped_column(server_default=text("0"))
    # When it was last launched: the tools offered in a turn are the most recently used.
    last_launched_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    changed_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ToolSetting(Base):
    """A bound of Domo's tools set for the whole house (``madetools.settings``): its ``key``, the
    whole number he gave it (``value``, a JSON number) and when (``set_at``). A key without a row
    has its start; Domo alone changes one, by a tool call in the core."""

    __tablename__ = "tool_settings"
    __table_args__ = (CheckConstraint("jsonb_typeof(value) = 'number'", name="value_is_a_number"),)

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(JSONB)
    set_at: Mapped[datetime]


class MadeToolVersion(Base):
    """One version of a tool's code. ``fingerprint`` is the sha256 of the code (hex); ``checks``
    what the checks said (``madetools.checks.CheckReport.for_model``: never the code). A version
    submitted by a build (``build_id``) of a tool that does not exist yet has no ``tool_id`` until
    the build makes the tool; one of a build that was abandoned keeps none. A version that passed
    keeps what it made the tool with (``description``, ``parameters``, ``operations``, as the tool
    keeps them), so that Domo may make it the one in force again (``tools.made_tools``'s
    ``tool_back``)."""

    __tablename__ = "made_tool_versions"
    __table_args__ = (
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint("state IN ('checking', 'ready', 'refused')", name="state_values"),
        CheckConstraint("tool_id IS NOT NULL OR build_id IS NOT NULL", name="tool_or_build"),
        Index("ix_made_tool_versions_tool_id", "tool_id"),
        Index("ix_made_tool_versions_build_id", "build_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="CASCADE")
    )
    build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_builds.id", ondelete="SET NULL")
    )
    fingerprint: Mapped[str]
    code: Mapped[str]
    state: Mapped[VersionState] = mapped_column(Text(), server_default=text("'checking'"))
    checks: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    description: Mapped[str | None]
    parameters: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    operations: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class HandRun(Base):
    """A program Domo ran by hand (spec 5.4: ``tools.program_run``), to answer at once what no
    tool does yet: its ``code`` (by ``fingerprint``, the sha256 of the code in hex), the
    ``operations`` it declared (as a tool keeps them, ``madetools.risks``), the ``arguments`` it
    was given, who opened it (``opened_by``: the person of the turn; none, Domo on his own), for
    whom it runs (``for_person_id``; none, the house) and the time a launch of it may take (its
    call's, or the house's ``run_seconds`` of then; 1 second at least, no ceiling). Kept the
    house's ``hand_runs_kept_days`` (``madetools.upkeep``) so that a stop of it can be run again
    and a build can start from its code (``ToolBuild.from_run_id``), then deleted; deleted with the
    person it ran for. The code and the arguments are never written to the audit."""

    __tablename__ = "hand_runs"
    __table_args__ = (
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint("max_seconds >= 1", name="max_seconds_positive"),
        Index("ix_hand_runs_for_person_id", "for_person_id"),
        Index("ix_hand_runs_opened_by", "opened_by"),
        Index("ix_hand_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    code: Mapped[str]
    fingerprint: Mapped[str]
    operations: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    opened_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    for_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    # Eight bytes: its call's time and ``run_seconds`` have no ceiling (``settings.held``).
    max_seconds: Mapped[int] = mapped_column(BigInteger())
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class MadeToolTrigger(Base):
    """When a tool launches by itself: at a time (``due_at``, ``repeat`` an iCalendar rule), when
    someone arrives, or when someone returns (``returner_id``: whose return), with the
    ``arguments`` Domo gave it, for someone (``for_person_id``: the person it launches for;
    none, the house). A trigger for someone who left waits, neither fired nor moved, until they
    come back (``madetools.triggers``). When it last fired (``last_fired_at``) and when it was set
    (``created_at``): an arrival trigger fires for the people who joined since. A firing the
    sandbox did not answer is tried again: how many times in a row (``missed_tries``) and since
    when (``missed_since``)."""

    __tablename__ = "made_tool_triggers"
    __table_args__ = (
        CheckConstraint("kind IN ('time', 'arrival', 'return')", name="kind_values"),
        CheckConstraint("missed_tries >= 0", name="missed_tries_positive"),
        CheckConstraint("state IN ('waiting', 'done', 'cancelled')", name="state_values"),
        CheckConstraint(
            "repeat IS NULL OR char_length(repeat) BETWEEN 1 AND 200", name="repeat_length"
        ),
        CheckConstraint(
            "state <> 'waiting' OR kind <> 'time' OR due_at IS NOT NULL", name="time_needs_due"
        ),
        # A return is someone's; an arrival names nobody (someone arrives in the household).
        CheckConstraint(
            "kind <> 'return' OR returner_id IS NOT NULL", name="return_needs_returner"
        ),
        Index("ix_made_tool_triggers_tool_id", "tool_id"),
        Index("ix_made_tool_triggers_returner_id", "returner_id"),
        Index("ix_made_tool_triggers_for_person_id", "for_person_id"),
        Index("ix_made_tool_triggers_due_at", "due_at", postgresql_where=text("state = 'waiting'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("made_tools.id", ondelete="CASCADE"))
    kind: Mapped[TriggerKind] = mapped_column(Text())
    due_at: Mapped[datetime | None]
    repeat: Mapped[str | None]
    returner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    for_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    state: Mapped[TriggerState] = mapped_column(Text(), server_default=text("'waiting'"))
    last_fired_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    missed_tries: Mapped[int] = mapped_column(server_default=text("0"))
    missed_since: Mapped[datetime | None]


LaunchCause = Literal["turn", "trigger", "tool"]
LaunchForKind = Literal["person", "house"]
LaunchEnd = Literal["done", "failed", "refused", "set_aside", "stopped"]
# How much of the error a program said a launch keeps (``MadeToolLaunch.error``).
ERROR_KEPT = 2000


class MadeToolLaunch(Base):
    """The trace of one launch of a tool (spec 12.2), for Domo to see how his tools behave
    (``madetools.launches``): when it ended (``at``), its ``cause``, the version that ran, whom it
    was for (``for_kind``: a person or the house, never who), whether a turn's call made it
    (``by_turn``; one that did not cost no turn of a model), how it ended (``status``), how long it
    ran (``seconds``), how many requests its program made (``reads`` of them reads), what its
    answer weighed as JSON (``answer_bytes``), the error its program said (``error``, its first
    ``ERROR_KEPT`` characters: the program's words, data for Domo, never in the audit log nor
    shown to anyone) and, for a launch that did not end ``done``, why the core or the sandbox ended
    or refused it, in their own words (``reason``, its first ``ERROR_KEPT`` characters), with the
    setting that did it and its figure (``setting``, ``setting_value``, as the database keeps a
    count: ``settings.held``). Kept ``launches_kept_days`` (``madetools.upkeep``); it goes with its
    tool."""

    __tablename__ = "made_tool_launches"
    __table_args__ = (
        CheckConstraint("cause IN ('turn', 'trigger', 'tool')", name="cause_values"),
        CheckConstraint("for_kind IN ('person', 'house')", name="for_kind_values"),
        CheckConstraint(
            "status IN ('done', 'failed', 'refused', 'set_aside', 'stopped')", name="status_values"
        ),
        CheckConstraint(
            "requests >= 0 AND reads >= 0 AND answer_bytes >= 0", name="counts_positive"
        ),
        CheckConstraint(
            f"error IS NULL OR char_length(error) <= {ERROR_KEPT}", name="error_length"
        ),
        CheckConstraint(
            f"reason IS NULL OR char_length(reason) <= {ERROR_KEPT}", name="reason_length"
        ),
        CheckConstraint("(setting IS NULL) = (setting_value IS NULL)", name="setting_with_value"),
        Index("ix_made_tool_launches_tool_id_at", "tool_id", "at"),
        Index("ix_made_tool_launches_version_id", "version_id"),
        Index("ix_made_tool_launches_at", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("made_tools.id", ondelete="CASCADE"))
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tool_versions.id", ondelete="SET NULL")
    )
    at: Mapped[datetime]
    cause: Mapped[LaunchCause] = mapped_column(Text())
    for_kind: Mapped[LaunchForKind] = mapped_column(Text())
    by_turn: Mapped[bool]
    status: Mapped[LaunchEnd] = mapped_column(Text())
    seconds: Mapped[float | None] = mapped_column(REAL())
    # Eight bytes: their settings have no ceiling (``requests_per_launch``, ``output_bytes``).
    requests: Mapped[int] = mapped_column(BigInteger(), server_default=text("0"))
    reads: Mapped[int] = mapped_column(BigInteger(), server_default=text("0"))
    answer_bytes: Mapped[int] = mapped_column(BigInteger(), server_default=text("0"))
    error: Mapped[str | None]
    reason: Mapped[str | None]
    setting: Mapped[str | None]
    setting_value: Mapped[int | None] = mapped_column(BigInteger())


RunState = Literal["running", "done", "failed", "set_aside"]
RunKind = Literal["run", "wake"]


class MadeToolRun(Base):
    """What Domo is to be woken with for a person (``person_id``), or for the house
    (``for_house``: the owner of the moment it is delivered, who answers for the house), until the
    turn that tells it ran (``madetools.handover``). It is of a tool (``tool_id``) or of a program
    run by hand (``hand_run_id``), never both.

    A ``run`` is a launch a turn stopped waiting for (it lasted more than the turn waits,
    ``madetools.handover.HAND_OVER_SECONDS``, or the turn was stopped), from the moment it was
    handed over (``handed_over_at``, ``running``). Once the launch ended (``finished_at``): how it
    ended (``status``), what it gave back (``result``) and what the programs of its chain asked to
    tell that person (``also``, a list of ``{"tool", "content"}``). A ``wake`` is what tools
    asked to tell a person (``also`` alone; ``tool_id`` is the first of them) whose turn could not
    run when they asked: kept ended (``done``) for the upkeep to try again.

    Delivering one only wakes: claimed (``claimed_at``), then settled (``delivered_at``) once the
    turn ran, or once it was given up after a day of tries that failed (``tries``, each a turn that
    could not run). The tools' output, never written to the audit log."""

    __tablename__ = "made_tool_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'done', 'failed', 'set_aside')", name="status_values"
        ),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_when_not_running"
        ),
        CheckConstraint("kind IN ('run', 'wake')", name="kind_values"),
        CheckConstraint("tries >= 0", name="tries_positive"),
        CheckConstraint("for_house = (person_id IS NULL)", name="for_house_or_person"),
        CheckConstraint("(tool_id IS NULL) <> (hand_run_id IS NULL)", name="tool_or_hand_run"),
        Index("ix_made_tool_runs_tool_id", "tool_id"),
        Index("ix_made_tool_runs_hand_run_id", "hand_run_id"),
        Index("ix_made_tool_runs_person_id", "person_id"),
        Index(
            "ix_made_tool_runs_handed_over_at",
            "handed_over_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="CASCADE")
    )
    hand_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("hand_runs.id", ondelete="CASCADE")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    for_house: Mapped[bool] = mapped_column(server_default=text("false"))
    kind: Mapped[RunKind] = mapped_column(Text(), server_default=text("'run'"))
    status: Mapped[RunState] = mapped_column(Text(), server_default=text("'running'"))
    result: Mapped[object] = mapped_column(JSONB, nullable=True)
    also: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    handed_over_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    claimed_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    tries: Mapped[int] = mapped_column(server_default=text("0"))


class ActionKind(Base):
    """A kind of action Domo opened for his tools (spec 5.6): the four risks the barrier reads
    (``costs``, ``irreversible``, ``physical``, ``outward``), the ``facts`` it shows and who may
    do it by default (``default_roles``)."""

    __tablename__ = "action_kinds"
    __table_args__ = (
        CheckConstraint("char_length(action_type) BETWEEN 1 AND 100", name="action_type_length"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 300", name="description_length"),
    )

    action_type: Mapped[str] = mapped_column(primary_key=True)
    costs: Mapped[bool] = mapped_column(server_default=text("false"))
    irreversible: Mapped[bool] = mapped_column(server_default=text("false"))
    physical: Mapped[bool] = mapped_column(server_default=text("false"))
    outward: Mapped[bool] = mapped_column(server_default=text("false"))
    facts: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    default_roles: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    description: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class ToolBuild(Base):
    """A tool being built, or to change (``tool_id``): what it is for, the ``examples`` it must
    pass and who opened it (whose key pays for it), and the program run by hand it starts from
    (``from_run_id``: its rounds are told that code, while the run is kept). At most one build per
    name is waiting or building."""

    __tablename__ = "tool_builds"
    __table_args__ = (
        CheckConstraint(NAME_RULE, name="name_format"),
        # Its length is Domo's ``purpose_chars``: no ceiling here.
        CheckConstraint("char_length(purpose) >= 1", name="purpose_length"),
        CheckConstraint(
            "state IN ('waiting', 'building', 'ready', 'abandoned')", name="state_values"
        ),
        CheckConstraint(
            "outcome IS NULL OR char_length(outcome) BETWEEN 1 AND 300", name="outcome_length"
        ),
        CheckConstraint("rounds >= 0", name="rounds_positive"),
        CheckConstraint("waits >= 0 AND waited_seconds >= 0", name="waits_positive"),
        CheckConstraint("refused_in_a_row >= 0", name="refused_in_a_row_positive"),
        Index(
            "uq_tool_builds_name_in_progress",
            "name",
            unique=True,
            postgresql_where=text("state IN ('waiting', 'building')"),
        ),
        Index("ix_tool_builds_opened_by", "opened_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str]
    purpose: Mapped[str]
    opened_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    examples: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="SET NULL")
    )
    from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("hand_runs.id", ondelete="SET NULL")
    )
    state: Mapped[BuildState] = mapped_column(Text(), server_default=text("'waiting'"))
    rounds: Mapped[int] = mapped_column(server_default=text("0"))
    # How many times in a row it waited for a model, the sandbox or the reviewer: the longer it
    # waits, the later it is tried again (``madetools.build.retried_after``).
    waits: Mapped[int] = mapped_column(server_default=text("0"))
    # How long it waited in all, in seconds, its checks or its model out of reach: a guard against a
    # build stuck for good (its ``give_up_hours``, a setting of Domo's).
    waited_seconds: Mapped[int] = mapped_column(server_default=text("0"))
    # How many counted rounds in a row made no tool (nothing usable submitted, or a version
    # refused), and the time before which it is not taken again (``None``: whenever): past a few
    # such rounds, the next ones are paced (``madetools.build_writes.pause_after``), the build
    # ``waiting`` until then.
    refused_in_a_row: Mapped[int] = mapped_column(server_default=text("0"))
    not_before: Mapped[datetime | None]
    last_progress_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    outcome: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


StopWhy = Literal["costs", "owner_rule", "approval"]
StopApprover = Literal["self", "adult", "owner"]
StopAsk = Literal["yes", "explicit_yes", "proof"]
AgreementPer = Literal["day", "week", "month"]


class LaunchStop(Base):
    """A launch that stopped for a yes (spec 5.3, 5.8: ``madetools.yes``): the tool, or the program
    run by hand (``hand_run_id``: one of the two, and the stop goes with it), for whom
    (``for_person_id``; none, the house), the operation it began (``None``: it had begun none, a
    read before any) and what it said it costs (``amount_cents``, ``None`` when nothing), why it
    needs a yes (``costs``, ``owner_rule``, ``approval``), whose (``approver``: ``self``, the one it
    is for, Domo for the house; or a person, ``adult`` or ``owner``) and what kind (``ask``), with
    the arguments of the launch and their digest (sha256 of their canonical JSON). The yes given for
    it is used once, by the relaunch (``yes_used_at``): the same tool, for the same one, with the
    same arguments, and for its operation up to its amount. The arguments are the launch's, never
    written to the audit."""

    __tablename__ = "launch_stops"
    __table_args__ = (
        CheckConstraint("why IN ('costs', 'owner_rule', 'approval')", name="why_values"),
        CheckConstraint("approver IN ('self', 'adult', 'owner')", name="approver_values"),
        CheckConstraint(
            "yes_approver IS NULL OR yes_approver IN ('self', 'adult', 'owner')",
            name="yes_approver_values",
        ),
        CheckConstraint(
            "(yes_used_at IS NULL) = (yes_approver IS NULL)", name="yes_approver_with_its_use"
        ),
        CheckConstraint("ask IN ('yes', 'explicit_yes', 'proof')", name="ask_values"),
        CheckConstraint(
            "operation IS NULL OR operation ~ '^[a-z][a-z0-9_]{0,29}$'", name="operation_format"
        ),
        CheckConstraint("amount_cents IS NULL OR amount_cents >= 0", name="amount_positive"),
        CheckConstraint("occurrence >= 0", name="occurrence_positive"),
        CheckConstraint("arguments_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("(tool_id IS NULL) <> (hand_run_id IS NULL)", name="tool_or_hand_run"),
        Index("ix_launch_stops_tool_id", "tool_id"),
        Index("ix_launch_stops_hand_run_id", "hand_run_id"),
        Index("ix_launch_stops_for_person_id", "for_person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="CASCADE")
    )
    hand_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("hand_runs.id", ondelete="CASCADE")
    )
    for_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    operation: Mapped[str | None]
    # Which begin of its operation in the run it stopped in (from 1; 0 before any): its yes
    # applies there in the relaunch, and nowhere else.
    occurrence: Mapped[int] = mapped_column(server_default=text("0"))
    amount_cents: Mapped[int | None]
    why: Mapped[StopWhy] = mapped_column(Text())
    approver: Mapped[StopApprover] = mapped_column(Text())
    ask: Mapped[StopAsk] = mapped_column(Text())
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    arguments_digest: Mapped[str]
    # The yeses the stopped run was given, each at its place (``{"operation", "occurrence",
    # "amount_cents", "why", "approver", "ask"}``), and the money it had declared, by begin
    # (``"<operation>#<occurrence>"`` → ``{"id", "cents"}``: the declaration, and what was let
    # through last): a relaunch neither forgets the first nor declares the second again.
    answered_before: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    declared: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Who really gave its yes, as an approver (``self``: the one it is for; ``adult``, ``owner``:
    # such a person, never the requester): what the relaunch carries, never what was asked.
    yes_approver: Mapped[StopApprover | None] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    yes_used_at: Mapped[datetime | None]


class StandingAgreement(Base):
    """An agreement that lasts (spec 5.3: "order without asking me on Fridays, up to 40 euros"):
    an operation of a tool done for someone (``for_person_id``; none, the house) passes without a
    yes when it says it costs ``amount_cents_at_most`` at most, ``times`` times at most per
    calendar ``per`` (day, ISO week or month, in the household's zone), on the ``weekdays`` it
    names (ISO numbers, Monday 1 to Sunday 7; ``None``: any day). Who kept it (``given_by``: a
    person; the owner for the house), and when it was withdrawn (``withdrawn_at``: it no longer
    counts). The barrier's code reads it (``madetools.yes.agreement_covers``), never a model."""

    __tablename__ = "standing_agreements"
    __table_args__ = (
        CheckConstraint("amount_cents_at_most > 0", name="amount_positive"),
        CheckConstraint("times > 0", name="times_positive"),
        CheckConstraint("per IN ('day', 'week', 'month')", name="per_values"),
        CheckConstraint("operation ~ '^[a-z][a-z0-9_]{0,29}$'", name="operation_format"),
        CheckConstraint(
            "weekdays IS NULL OR (cardinality(weekdays) BETWEEN 1 AND 7 "
            "AND weekdays <@ ARRAY[1, 2, 3, 4, 5, 6, 7])",
            name="weekdays_values",
        ),
        Index("ix_standing_agreements_tool_id", "tool_id"),
        Index("ix_standing_agreements_for_person_id", "for_person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("made_tools.id", ondelete="CASCADE"))
    operation: Mapped[str]
    for_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    amount_cents_at_most: Mapped[int]
    times: Mapped[int]
    per: Mapped[AgreementPer] = mapped_column(Text())
    weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(Integer()))
    given_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    withdrawn_at: Mapped[datetime | None]


class DeclaredSpending(Base):
    """What a program said an operation it began costs, let through (``madetools.yes.declare``):
    for whom (``for_person_id``; none, the house), the tool (none: a program run by hand, whose
    spending outlives it) and the operation, the amount in whole
    cents of euros, and the standing agreement it passed under (``agreement_id``; none, a yes).
    Kept apart from what models cost; the owner's limits read it (``policy.facts``: ``spent``), and
    an agreement counts its times on it."""

    __tablename__ = "declared_spending"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        Index("ix_declared_spending_for_person_id_at", "for_person_id", "at"),
        Index("ix_declared_spending_agreement_id", "agreement_id"),
        Index("ix_declared_spending_tool_id", "tool_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    for_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("made_tools.id", ondelete="CASCADE")
    )
    operation: Mapped[str]
    amount_cents: Mapped[int]
    agreement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("standing_agreements.id", ondelete="SET NULL")
    )
    at: Mapped[datetime]


class ToolTroubleDay(Base):
    """How many times Domo was woken on a ``day`` of the household because a tool of his was set
    aside or a build abandoned (``madetools.troubles``: ``trouble_wakes_per_day`` at most, a
    setting of his). Only the day of now is kept."""

    __tablename__ = "tool_trouble_days"
    __table_args__ = (CheckConstraint("wakes >= 0", name="wakes_positive"),)

    day: Mapped[date] = mapped_column(primary_key=True)
    # Eight bytes: ``trouble_wakes_per_day`` has no ceiling.
    wakes: Mapped[int] = mapped_column(BigInteger(), server_default=text("0"))
