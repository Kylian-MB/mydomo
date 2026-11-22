"""The tools Domo makes: their tables, their builds, the action kinds he opens.

``made_tools`` (a name of 1 to 40 characters of ``a-z0-9_`` starting with a letter, unique among
the tools that are not removed: a removed tool frees its name; a tool is Domo's, nobody's in
particular, with its operations and the bounds Domo set for it alone, ``settings``, its launches
per day among them: no ceiling, as every setting of his),
``made_tool_versions`` (the code, by fingerprint; a version a build submitted points at it, and
has no tool until the build makes one; one that passed keeps the description, parameters and
operations it made the tool with, so that Domo may go back to it), ``hand_runs`` (a program
Domo ran by hand, kept for his setting ``hand_runs_kept_days``: its code, operations and
arguments, who opened it and for whom;
a build may start from one,
``tool_builds.from_run_id``, and a stop or a run handed over may be one's instead of a tool's),
``made_tool_triggers`` (for whom it launches, a person or
the house, and whose return fires a return trigger; with the tries of a firing the sandbox did not
answer), ``made_tool_runs`` (a launch handed over to the background, or a wake whose turn could
not run, from then to its delivery, with its tries; a wake of a launch for the house is the
owner's), ``action_kinds`` and ``tool_builds`` (one build per name in ``waiting`` or
``building``, by a partial unique index). A person's row takes with it the triggers for them or
for their return and their runs; a build forgets who opened it (``SET NULL``) and cannot be
deleted while a plan waits on it (``RESTRICT``). ``plans`` gains
``after_build_id`` (a plan waiting for a build) and ``waits_since`` (since when it waits for
it: both or neither), and ``needs_a_moment`` accepts it. The kinds of
tasks of a model call gain ``toolmaking`` and ``tool_review``. A right may be Domo's own
(``policy_rights.for_domo``, 2026-11-18: one subject at most, a person, a role or Domo; a right
of his is no rule for everyone; one in force per subject and type, his counted apart).
``tools_spaces`` (2026-11-18) keeps the roles of the tools' own database: the house's, and one
corner per person (a role name and a schema name that say nothing of whose they are, the
password; a removed person leaves theirs without anyone, ``SET NULL``, for the upkeep).
``launch_stops`` (a launch stopped for a yes, its arguments and their digest, the yes used once),
``standing_agreements`` (an operation that passes without a yes: an amount at most, a number of
times per day, week or month, the weekdays) and ``declared_spending`` (what a program said it
spent, with the agreement it passed under) keep the yes of programs; a person's row takes theirs
with it, the house's have none. A request, and an action under a reply, may be Domo's own
(``confirmations.by_domo``, ``message_actions.by_domo``: made in a turn he acted in on his
own, or, for an action, one of his own acts in anyone's turn; ``confirmations.counted_for_domo``:
his history counted it already, 2026-11-22), and so may a note
(``notes.by_domo``, 2026-11-21: then no person is its author, ``notes.author_id`` empty; exactly
one of them names it). ``tool_settings`` (2026-11-21)
keeps the bounds of his tools Domo set for the whole house, a whole number by key;
``made_tool_launches`` (2026-11-21) the trace of each launch of a tool (going with its tool; why
the core or the sandbox ended one, with the setting that did, 2026-11-22), and a model call
gains the build and the tool it was made for (``SET NULL``); ``tool_trouble_days`` how many times
Domo was woken on a day because a tool was set aside or a build abandoned. Going back down
undoes all of it (Domo is not installed anywhere yet: this is for a development database); the
plans that wait for a build are dropped first, as the old constraint would refuse them, and so are
the calls of the new tasks, Domo's own rights and his own notes.

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_UUID = sa.text("gen_random_uuid()")
NOW = sa.text("clock_timestamp()")
NAME_RULE = "name ~ '^[a-z][a-z0-9_]{0,39}$'"
# A float of four bytes (``real``): the seconds of a launch.
_REAL: sa.types.TypeEngine[float] = sa.REAL()

# A note is a person's or Domo's own, exactly one of them (``notes.models.ONE_AUTHOR``).
_ONE_AUTHOR = "(author_id IS NULL) = by_domo"

_RIGHTS = "policy_rights"
_RIGHTS_INDEX = "uq_policy_rights_subject_action_type"
_AT_MOST_ONE_BEFORE = "NOT (person_id IS NOT NULL AND role IS NOT NULL)"
_AT_MOST_ONE_NOW = (
    "NOT (person_id IS NOT NULL AND role IS NOT NULL) "
    "AND NOT (for_domo AND (person_id IS NOT NULL OR role IS NOT NULL))"
)
_EVERYONE_BEFORE = (
    "person_id IS NOT NULL OR role IS NOT NULL OR (access IS NULL AND approver IS NULL "
    "AND limit_expression IS NULL)"
)
_EVERYONE_NOW = (
    "person_id IS NOT NULL OR role IS NOT NULL OR for_domo OR (access IS NULL AND approver IS "
    "NULL AND limit_expression IS NULL)"
)

_OLD_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival', 'memory')"
)
_NEW_TASKS = (
    "('conversation', 'judge', 'intent', 'rights_writer', 'key_check', 'upkeep', "
    "'writing', 'checking', 'arrival', 'memory', 'toolmaking', 'tool_review')"
)


def _set_tasks(tasks: str) -> None:
    op.drop_constraint(op.f("ck_model_calls_task_values"), "model_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_model_calls_task_values"), "model_calls", f"task IN {tasks}"
    )


def _set_needs_a_moment(condition: str) -> None:
    op.drop_constraint(op.f("ck_plans_needs_a_moment"), "plans", type_="check")
    op.create_check_constraint(op.f("ck_plans_needs_a_moment"), "plans", condition)


def _set_subjects(*, at_most_one: str, everyone: str, index_columns: list[str]) -> None:
    """The rules of a right's subject (``policy_rights``): one at most, what a rule for everyone
    says, and one right in force per subject and action type."""
    op.drop_constraint(op.f("ck_policy_rights_at_most_one_subject"), _RIGHTS, type_="check")
    op.drop_constraint(op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, type_="check")
    op.drop_index(_RIGHTS_INDEX, table_name=_RIGHTS)
    op.create_check_constraint(op.f("ck_policy_rights_at_most_one_subject"), _RIGHTS, at_most_one)
    op.create_check_constraint(op.f("ck_policy_rights_everyone_only_asks"), _RIGHTS, everyone)
    op.create_index(
        _RIGHTS_INDEX,
        _RIGHTS,
        index_columns,
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )


def upgrade() -> None:
    op.add_column(
        _RIGHTS, sa.Column("for_domo", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    for table in ("confirmations", "message_actions"):
        op.add_column(
            table, sa.Column("by_domo", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.add_column(
        "confirmations",
        sa.Column("counted_for_domo", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    _set_subjects(
        at_most_one=_AT_MOST_ONE_NOW,
        everyone=_EVERYONE_NOW,
        index_columns=["person_id", "role", "for_domo", "action_type"],
    )
    op.add_column(
        "notes", sa.Column("by_domo", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.alter_column("notes", "author_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(op.f("ck_notes_one_author"), "notes", _ONE_AUTHOR)

    op.create_table(
        "made_tools",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("origin", sa.Text(), server_default=sa.text("'domo'"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'ready'"), nullable=False),
        sa.Column(
            "operations", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("aside_reason", sa.Text(), nullable=True),
        sa.Column("launches_day", sa.Date(), nullable=True),
        sa.Column("launches_today", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wakes_today", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failures_in_a_row", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(NAME_RULE, name=op.f("ck_made_tools_name_format")),
        sa.CheckConstraint(
            "jsonb_typeof(settings) = 'object'", name=op.f("ck_made_tools_settings_is_an_object")
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 200",
            name=op.f("ck_made_tools_description_length"),
        ),
        sa.CheckConstraint(
            "origin IN ('domo', 'project')", name=op.f("ck_made_tools_origin_values")
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'set_aside', 'removed')", name=op.f("ck_made_tools_state_values")
        ),
        sa.CheckConstraint(
            "launches_today >= 0 AND wakes_today >= 0 AND failures_in_a_row >= 0",
            name=op.f("ck_made_tools_counts_positive"),
        ),
        sa.CheckConstraint(
            "aside_reason IS NULL OR char_length(aside_reason) BETWEEN 1 AND 300",
            name=op.f("ck_made_tools_aside_reason_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_made_tools")),
    )
    op.create_index(
        "uq_made_tools_name",
        "made_tools",
        ["name"],
        unique=True,
        postgresql_where=sa.text("state <> 'removed'"),
    )

    op.create_table(
        "made_tool_versions",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("build_id", sa.Uuid(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'checking'"), nullable=False),
        sa.Column(
            "checks", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("operations", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_made_tool_versions_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "state IN ('checking', 'ready', 'refused')",
            name=op.f("ck_made_tool_versions_state_values"),
        ),
        sa.CheckConstraint(
            "tool_id IS NOT NULL OR build_id IS NOT NULL",
            name=op.f("ck_made_tool_versions_tool_or_build"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_made_tool_versions_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_made_tool_versions")),
    )
    op.create_index("ix_made_tool_versions_tool_id", "made_tool_versions", ["tool_id"])
    op.create_index("ix_made_tool_versions_build_id", "made_tool_versions", ["build_id"])
    # The two tables point at each other: the tool's current version is added once both exist.
    op.create_foreign_key(
        op.f("fk_made_tools_current_version_id_made_tool_versions"),
        "made_tools",
        "made_tool_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "hand_runs",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "operations", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "arguments", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("opened_by", sa.Uuid(), nullable=True),
        sa.Column("for_person_id", sa.Uuid(), nullable=True),
        sa.Column("max_seconds", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_hand_runs_fingerprint_format")
        ),
        sa.CheckConstraint("max_seconds >= 1", name=op.f("ck_hand_runs_max_seconds_positive")),
        sa.ForeignKeyConstraint(
            ["opened_by"],
            ["persons.id"],
            name=op.f("fk_hand_runs_opened_by_persons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["for_person_id"],
            ["persons.id"],
            name=op.f("fk_hand_runs_for_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hand_runs")),
    )
    op.create_index("ix_hand_runs_for_person_id", "hand_runs", ["for_person_id"])
    op.create_index("ix_hand_runs_opened_by", "hand_runs", ["opened_by"])
    op.create_index("ix_hand_runs_created_at", "hand_runs", ["created_at"])

    op.create_table(
        "made_tool_triggers",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("repeat", sa.Text(), nullable=True),
        sa.Column("returner_id", sa.Uuid(), nullable=True),
        sa.Column("for_person_id", sa.Uuid(), nullable=True),
        sa.Column(
            "arguments", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("state", sa.Text(), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("missed_tries", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("missed_since", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('time', 'arrival', 'return')",
            name=op.f("ck_made_tool_triggers_kind_values"),
        ),
        sa.CheckConstraint(
            "missed_tries >= 0", name=op.f("ck_made_tool_triggers_missed_tries_positive")
        ),
        sa.CheckConstraint(
            "state IN ('waiting', 'done', 'cancelled')",
            name=op.f("ck_made_tool_triggers_state_values"),
        ),
        sa.CheckConstraint(
            "repeat IS NULL OR char_length(repeat) BETWEEN 1 AND 200",
            name=op.f("ck_made_tool_triggers_repeat_length"),
        ),
        sa.CheckConstraint(
            "state <> 'waiting' OR kind <> 'time' OR due_at IS NOT NULL",
            name=op.f("ck_made_tool_triggers_time_needs_due"),
        ),
        sa.CheckConstraint(
            "kind <> 'return' OR returner_id IS NOT NULL",
            name=op.f("ck_made_tool_triggers_return_needs_returner"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_made_tool_triggers_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["returner_id"],
            ["persons.id"],
            name=op.f("fk_made_tool_triggers_returner_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["for_person_id"],
            ["persons.id"],
            name=op.f("fk_made_tool_triggers_for_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_made_tool_triggers")),
    )
    op.create_index("ix_made_tool_triggers_tool_id", "made_tool_triggers", ["tool_id"])
    op.create_index("ix_made_tool_triggers_returner_id", "made_tool_triggers", ["returner_id"])
    op.create_index("ix_made_tool_triggers_for_person_id", "made_tool_triggers", ["for_person_id"])
    op.create_index(
        "ix_made_tool_triggers_due_at",
        "made_tool_triggers",
        ["due_at"],
        postgresql_where=sa.text("state = 'waiting'"),
    )

    op.create_table(
        "made_tool_runs",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("hand_run_id", sa.Uuid(), nullable=True),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("for_house", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'run'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "also", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tries", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed', 'set_aside')",
            name=op.f("ck_made_tool_runs_status_values"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_made_tool_runs_finished_when_not_running"),
        ),
        sa.CheckConstraint("kind IN ('run', 'wake')", name=op.f("ck_made_tool_runs_kind_values")),
        sa.CheckConstraint("tries >= 0", name=op.f("ck_made_tool_runs_tries_positive")),
        sa.CheckConstraint(
            "for_house = (person_id IS NULL)",
            name=op.f("ck_made_tool_runs_for_house_or_person"),
        ),
        sa.CheckConstraint(
            "(tool_id IS NULL) <> (hand_run_id IS NULL)",
            name=op.f("ck_made_tool_runs_tool_or_hand_run"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_made_tool_runs_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["hand_run_id"],
            ["hand_runs.id"],
            name=op.f("fk_made_tool_runs_hand_run_id_hand_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_made_tool_runs_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_made_tool_runs")),
    )
    op.create_index("ix_made_tool_runs_tool_id", "made_tool_runs", ["tool_id"])
    op.create_index("ix_made_tool_runs_hand_run_id", "made_tool_runs", ["hand_run_id"])
    op.create_index("ix_made_tool_runs_person_id", "made_tool_runs", ["person_id"])
    op.create_index(
        "ix_made_tool_runs_handed_over_at",
        "made_tool_runs",
        ["handed_over_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )

    op.create_table(
        "action_kinds",
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("costs", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("irreversible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("physical", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("outward", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "facts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "default_roles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "char_length(action_type) BETWEEN 1 AND 100",
            name=op.f("ck_action_kinds_action_type_length"),
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 300",
            name=op.f("ck_action_kinds_description_length"),
        ),
        sa.PrimaryKeyConstraint("action_type", name=op.f("pk_action_kinds")),
    )

    op.create_table(
        "tool_builds",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("opened_by", sa.Uuid(), nullable=True),
        sa.Column(
            "examples", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("from_run_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("rounds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("waits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("waited_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("refused_in_a_row", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_progress_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False
        ),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(NAME_RULE, name=op.f("ck_tool_builds_name_format")),
        sa.CheckConstraint("char_length(purpose) >= 1", name=op.f("ck_tool_builds_purpose_length")),
        sa.CheckConstraint(
            "state IN ('waiting', 'building', 'ready', 'abandoned')",
            name=op.f("ck_tool_builds_state_values"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR char_length(outcome) BETWEEN 1 AND 300",
            name=op.f("ck_tool_builds_outcome_length"),
        ),
        sa.CheckConstraint("rounds >= 0", name=op.f("ck_tool_builds_rounds_positive")),
        sa.CheckConstraint(
            "waits >= 0 AND waited_seconds >= 0", name=op.f("ck_tool_builds_waits_positive")
        ),
        sa.CheckConstraint(
            "refused_in_a_row >= 0", name=op.f("ck_tool_builds_refused_in_a_row_positive")
        ),
        sa.ForeignKeyConstraint(
            ["opened_by"],
            ["persons.id"],
            name=op.f("fk_tool_builds_opened_by_persons"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_tool_builds_tool_id_made_tools"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_run_id"],
            ["hand_runs.id"],
            name=op.f("fk_tool_builds_from_run_id_hand_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_builds")),
    )
    op.create_index(
        "uq_tool_builds_name_in_progress",
        "tool_builds",
        ["name"],
        unique=True,
        postgresql_where=sa.text("state IN ('waiting', 'building')"),
    )
    op.create_index("ix_tool_builds_opened_by", "tool_builds", ["opened_by"])
    # A version a build submitted points at it: added once both tables exist.
    op.create_foreign_key(
        op.f("fk_made_tool_versions_build_id_tool_builds"),
        "made_tool_versions",
        "tool_builds",
        ["build_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("plans", sa.Column("after_build_id", sa.Uuid(), nullable=True))
    op.add_column("plans", sa.Column("waits_since", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_plans_waits_since_with_build"),
        "plans",
        "(after_build_id IS NULL) = (waits_since IS NULL)",
    )
    op.create_foreign_key(
        op.f("fk_plans_after_build_id_tool_builds"),
        "plans",
        "tool_builds",
        ["after_build_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_plans_after_build_id",
        "plans",
        ["after_build_id"],
        postgresql_where=sa.text("state = 'waiting'"),
    )
    _set_needs_a_moment(
        "state <> 'waiting' OR due_at IS NOT NULL OR on_return OR after_build_id IS NOT NULL"
    )
    _set_tasks(_NEW_TASKS)

    op.create_table(
        "tools_spaces",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("role_name", sa.Text(), nullable=False),
        sa.Column("schema_name", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("kind IN ('house', 'corner')", name=op.f("ck_tools_spaces_kind_values")),
        sa.CheckConstraint(
            "(kind = 'corner') = (schema_name IS NOT NULL)",
            name=op.f("ck_tools_spaces_corner_has_schema"),
        ),
        sa.CheckConstraint(
            "kind = 'corner' OR person_id IS NULL",
            name=op.f("ck_tools_spaces_house_has_no_person"),
        ),
        sa.CheckConstraint(
            "role_name ~ '^jt_[a-z0-9_]{1,40}$'", name=op.f("ck_tools_spaces_role_name_format")
        ),
        sa.CheckConstraint(
            "schema_name IS NULL OR schema_name ~ '^c_[0-9a-f]{12}$'",
            name=op.f("ck_tools_spaces_schema_name_format"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_tools_spaces_person_id_persons"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tools_spaces")),
        sa.UniqueConstraint("role_name", name=op.f("uq_tools_spaces_role_name")),
        sa.UniqueConstraint("schema_name", name=op.f("uq_tools_spaces_schema_name")),
    )
    op.create_index(
        "uq_tools_spaces_person_id",
        "tools_spaces",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text("person_id IS NOT NULL"),
    )
    _create_yes_tables()

    op.create_table(
        "tool_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(value) = 'number'", name=op.f("ck_tool_settings_value_is_a_number")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_tool_settings")),
    )
    _create_launches()
    _create_trouble_days()


def _create_trouble_days() -> None:
    """How many times Domo was woken on a day of the household because a tool of his was set
    aside or a build abandoned (2026-11-21, ``madetools.troubles``)."""
    op.create_table(
        "tool_trouble_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("wakes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("wakes >= 0", name=op.f("ck_tool_trouble_days_wakes_positive")),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_tool_trouble_days")),
    )


def _create_launches() -> None:
    """The trace of each launch of a tool, and the build and the tool a model call was made for
    (2026-11-21)."""
    op.create_table(
        "made_tool_launches",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("for_kind", sa.Text(), nullable=False),
        sa.Column("by_turn", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("seconds", _REAL, nullable=True),
        sa.Column("requests", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reads", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("answer_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("setting", sa.Text(), nullable=True),
        sa.Column("setting_value", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "cause IN ('turn', 'trigger', 'tool')", name=op.f("ck_made_tool_launches_cause_values")
        ),
        sa.CheckConstraint(
            "for_kind IN ('person', 'house')", name=op.f("ck_made_tool_launches_for_kind_values")
        ),
        sa.CheckConstraint(
            "status IN ('done', 'failed', 'refused', 'set_aside', 'stopped')",
            name=op.f("ck_made_tool_launches_status_values"),
        ),
        sa.CheckConstraint(
            "requests >= 0 AND reads >= 0 AND answer_bytes >= 0",
            name=op.f("ck_made_tool_launches_counts_positive"),
        ),
        sa.CheckConstraint(
            "error IS NULL OR char_length(error) <= 2000",
            name=op.f("ck_made_tool_launches_error_length"),
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 2000",
            name=op.f("ck_made_tool_launches_reason_length"),
        ),
        sa.CheckConstraint(
            "(setting IS NULL) = (setting_value IS NULL)",
            name=op.f("ck_made_tool_launches_setting_with_value"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_made_tool_launches_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["made_tool_versions.id"],
            name=op.f("fk_made_tool_launches_version_id_made_tool_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_made_tool_launches")),
    )
    op.create_index("ix_made_tool_launches_tool_id_at", "made_tool_launches", ["tool_id", "at"])
    op.create_index("ix_made_tool_launches_version_id", "made_tool_launches", ["version_id"])
    op.create_index("ix_made_tool_launches_at", "made_tool_launches", ["at"])

    op.add_column("model_calls", sa.Column("tool_id", sa.Uuid(), nullable=True))
    op.add_column("model_calls", sa.Column("build_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_model_calls_tool_id_made_tools"),
        "model_calls",
        "made_tools",
        ["tool_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_model_calls_build_id_tool_builds"),
        "model_calls",
        "tool_builds",
        ["build_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_model_calls_tool_id", "model_calls", ["tool_id"])
    op.create_index("ix_model_calls_build_id", "model_calls", ["build_id"])


def _drop_launches() -> None:
    """``_create_launches`` undone."""
    op.drop_index("ix_model_calls_build_id", table_name="model_calls")
    op.drop_index("ix_model_calls_tool_id", table_name="model_calls")
    op.drop_constraint(
        op.f("fk_model_calls_build_id_tool_builds"), "model_calls", type_="foreignkey"
    )
    op.drop_constraint(op.f("fk_model_calls_tool_id_made_tools"), "model_calls", type_="foreignkey")
    op.drop_column("model_calls", "build_id")
    op.drop_column("model_calls", "tool_id")
    op.drop_table("made_tool_launches")


_OPERATION_RULE = "operation ~ '^[a-z][a-z0-9_]{0,29}$'"


def _create_yes_tables() -> None:
    """The launches stopped for a yes, the agreements that last, and what programs said they
    spent (2026-11-18)."""
    op.create_table(
        "launch_stops",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("hand_run_id", sa.Uuid(), nullable=True),
        sa.Column("for_person_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("occurrence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("approver", sa.Text(), nullable=False),
        sa.Column("ask", sa.Text(), nullable=False),
        sa.Column(
            "arguments", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("arguments_digest", sa.Text(), nullable=False),
        sa.Column(
            "answered_before",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "declared", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("yes_approver", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("yes_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "why IN ('costs', 'owner_rule', 'approval')", name=op.f("ck_launch_stops_why_values")
        ),
        sa.CheckConstraint(
            "approver IN ('self', 'adult', 'owner')", name=op.f("ck_launch_stops_approver_values")
        ),
        sa.CheckConstraint(
            "yes_approver IS NULL OR yes_approver IN ('self', 'adult', 'owner')",
            name=op.f("ck_launch_stops_yes_approver_values"),
        ),
        sa.CheckConstraint(
            "(yes_used_at IS NULL) = (yes_approver IS NULL)",
            name=op.f("ck_launch_stops_yes_approver_with_its_use"),
        ),
        sa.CheckConstraint(
            "ask IN ('yes', 'explicit_yes', 'proof')", name=op.f("ck_launch_stops_ask_values")
        ),
        sa.CheckConstraint(
            f"operation IS NULL OR {_OPERATION_RULE}", name=op.f("ck_launch_stops_operation_format")
        ),
        sa.CheckConstraint(
            "amount_cents IS NULL OR amount_cents >= 0",
            name=op.f("ck_launch_stops_amount_positive"),
        ),
        sa.CheckConstraint("occurrence >= 0", name=op.f("ck_launch_stops_occurrence_positive")),
        sa.CheckConstraint(
            "arguments_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_launch_stops_digest_format")
        ),
        sa.CheckConstraint(
            "(tool_id IS NULL) <> (hand_run_id IS NULL)",
            name=op.f("ck_launch_stops_tool_or_hand_run"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_launch_stops_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["hand_run_id"],
            ["hand_runs.id"],
            name=op.f("fk_launch_stops_hand_run_id_hand_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["for_person_id"],
            ["persons.id"],
            name=op.f("fk_launch_stops_for_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_launch_stops")),
    )
    op.create_index("ix_launch_stops_tool_id", "launch_stops", ["tool_id"])
    op.create_index("ix_launch_stops_hand_run_id", "launch_stops", ["hand_run_id"])
    op.create_index("ix_launch_stops_for_person_id", "launch_stops", ["for_person_id"])

    op.create_table(
        "standing_agreements",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("for_person_id", sa.Uuid(), nullable=True),
        sa.Column("amount_cents_at_most", sa.Integer(), nullable=False),
        sa.Column("times", sa.Integer(), nullable=False),
        sa.Column("per", sa.Text(), nullable=False),
        sa.Column("weekdays", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("given_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "amount_cents_at_most > 0", name=op.f("ck_standing_agreements_amount_positive")
        ),
        sa.CheckConstraint("times > 0", name=op.f("ck_standing_agreements_times_positive")),
        sa.CheckConstraint(
            "per IN ('day', 'week', 'month')", name=op.f("ck_standing_agreements_per_values")
        ),
        sa.CheckConstraint(_OPERATION_RULE, name=op.f("ck_standing_agreements_operation_format")),
        sa.CheckConstraint(
            "weekdays IS NULL OR (cardinality(weekdays) BETWEEN 1 AND 7 "
            "AND weekdays <@ ARRAY[1, 2, 3, 4, 5, 6, 7])",
            name=op.f("ck_standing_agreements_weekdays_values"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_standing_agreements_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["for_person_id"],
            ["persons.id"],
            name=op.f("fk_standing_agreements_for_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["given_by"],
            ["persons.id"],
            name=op.f("fk_standing_agreements_given_by_persons"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_standing_agreements")),
    )
    op.create_index("ix_standing_agreements_tool_id", "standing_agreements", ["tool_id"])
    op.create_index(
        "ix_standing_agreements_for_person_id", "standing_agreements", ["for_person_id"]
    )

    op.create_table(
        "declared_spending",
        sa.Column("id", sa.Uuid(), server_default=NEW_UUID, nullable=False),
        sa.Column("for_person_id", sa.Uuid(), nullable=True),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("agreement_id", sa.Uuid(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_declared_spending_amount_positive")),
        sa.ForeignKeyConstraint(
            ["for_person_id"],
            ["persons.id"],
            name=op.f("fk_declared_spending_for_person_id_persons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["made_tools.id"],
            name=op.f("fk_declared_spending_tool_id_made_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agreement_id"],
            ["standing_agreements.id"],
            name=op.f("fk_declared_spending_agreement_id_standing_agreements"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_declared_spending")),
    )
    op.create_index(
        "ix_declared_spending_for_person_id_at", "declared_spending", ["for_person_id", "at"]
    )
    op.create_index("ix_declared_spending_agreement_id", "declared_spending", ["agreement_id"])
    op.create_index("ix_declared_spending_tool_id", "declared_spending", ["tool_id"])


def downgrade() -> None:
    op.drop_table("tool_trouble_days")
    _drop_launches()
    op.drop_table("tool_settings")
    op.drop_table("declared_spending")
    op.drop_table("standing_agreements")
    op.drop_table("launch_stops")

    op.drop_index("uq_tools_spaces_person_id", table_name="tools_spaces")
    op.drop_table("tools_spaces")

    op.execute("DELETE FROM model_calls WHERE task IN ('toolmaking', 'tool_review')")
    _set_tasks(_OLD_TASKS)
    op.execute(
        "DELETE FROM plans WHERE after_build_id IS NOT NULL "
        "AND due_at IS NULL AND NOT on_return AND state = 'waiting'"
    )
    _set_needs_a_moment("state <> 'waiting' OR due_at IS NOT NULL OR on_return")
    op.drop_index("ix_plans_after_build_id", table_name="plans")
    op.drop_constraint(op.f("fk_plans_after_build_id_tool_builds"), "plans", type_="foreignkey")
    op.drop_constraint(op.f("ck_plans_waits_since_with_build"), "plans", type_="check")
    op.drop_column("plans", "waits_since")
    op.drop_column("plans", "after_build_id")
    op.drop_constraint(
        op.f("fk_made_tool_versions_build_id_tool_builds"), "made_tool_versions", type_="foreignkey"
    )
    op.drop_table("tool_builds")
    op.drop_table("action_kinds")
    op.drop_table("made_tool_runs")
    op.drop_table("made_tool_triggers")
    op.drop_table("hand_runs")
    op.drop_constraint(
        op.f("fk_made_tools_current_version_id_made_tool_versions"),
        "made_tools",
        type_="foreignkey",
    )
    op.drop_table("made_tool_versions")
    op.drop_table("made_tools")

    # Domo's own notes go: the notes of before are a person's.
    op.drop_constraint(op.f("ck_notes_one_author"), "notes", type_="check")
    op.execute("DELETE FROM notes WHERE by_domo")
    op.alter_column("notes", "author_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("notes", "by_domo")

    op.drop_column("confirmations", "counted_for_domo")
    for table in ("message_actions", "confirmations"):
        op.drop_column(table, "by_domo")

    # Domo's own rights go: the rules of before would read them as rules for everyone.
    op.execute("DELETE FROM policy_rights WHERE for_domo")
    _set_subjects(
        at_most_one=_AT_MOST_ONE_BEFORE,
        everyone=_EVERYONE_BEFORE,
        index_columns=["person_id", "role", "action_type"],
    )
    op.drop_column(_RIGHTS, "for_domo")
