"""An invitation the assistant makes, and one for a record that exists.

``invitations.created_by`` may be empty: the invitation is the assistant's own act
(``by_domo``, exactly then), whose reason it keeps (``reason``, exactly then, as a record he opens
keeps his). ``kind`` says what the link does: ``new`` makes a new person, ``return`` brings back
someone who left (a ``person_id``, as before), ``record`` gives an account to a record that exists
already (a ``person_id`` too). ``proposed_username`` is the username the assistant proposes on the
page (the person may change it). ``person_acts.invitation_id`` is the invitation an act made (the
assistant's own invitation of someone new has no record yet: the owner is told of it by its name).

The rows written before this migration: an invitation whose making is a stored action of the
assistant's own (``message_actions``: ``by_domo`` with an ``idea_reason``, of type
``household.invite``, done, about the invitation's name, made within a minute of it; a link given
again does not make an invitation his) becomes his: no ``created_by``, his reason, its link
waiting for the owner when it still waits for one, and the person it made, if any, invited by
nobody.

Going back down cancels the invitations of kind ``record`` and those of the assistant that still
wait (nothing would tell them apart afterwards), gives the owner as the maker of the assistant's
(the column holds a person again; with no owner, the one its link was for, else the first person)
and as the inviter of whoever joined with one, and empties the ``person_id`` of the ``record`` rows
(before this migration a ``person_id`` meant a return link).

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER = "(SELECT id FROM persons WHERE role = 'owner' LIMIT 1)"


def upgrade() -> None:
    op.alter_column("invitations", "created_by", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "invitations",
        sa.Column("by_domo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("invitations", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column(
        "invitations",
        sa.Column("kind", sa.String(), server_default=sa.text("'new'"), nullable=False),
    )
    op.add_column("invitations", sa.Column("proposed_username", sa.Text(), nullable=True))
    op.add_column(
        "person_acts",
        sa.Column(
            "invitation_id",
            sa.Uuid(),
            sa.ForeignKey(
                "invitations.id",
                name=op.f("fk_person_acts_invitation_id_invitations"),
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.execute("UPDATE invitations SET kind = 'return' WHERE person_id IS NOT NULL")
    # The invitations the assistant made in his own name before this migration (see above).
    op.execute(
        "UPDATE invitations AS i SET created_by = NULL, by_domo = true, "
        "reason = left(a.idea_reason, 300), "
        "link_for_id = CASE WHEN i.token_hash IS NULL AND i.accepted_at IS NULL "
        f"AND i.cancelled_at IS NULL THEN {_OWNER} ELSE i.link_for_id END "
        "FROM message_actions AS a "
        "WHERE a.by_domo AND a.idea_reason IS NOT NULL AND a.idea_reason <> '' "
        "AND a.action_type = 'household.invite' "
        "AND a.status = 'done' AND a.subject_key = 'invitee:' || lower(i.display_name) "
        "AND abs(extract(epoch FROM a.created_at - i.created_at)) <= 60"
    )
    op.execute(
        "UPDATE persons SET invited_by = NULL WHERE id IN "
        "(SELECT accepted_person_id FROM invitations "
        "WHERE by_domo AND accepted_person_id IS NOT NULL AND kind = 'new')"
    )
    op.create_check_constraint(
        op.f("ck_invitations_by_domo_has_no_maker"), "invitations", "(created_by IS NULL) = by_domo"
    )
    op.create_check_constraint(
        op.f("ck_invitations_reason_of_domo"), "invitations", "(reason IS NOT NULL) = by_domo"
    )
    op.create_check_constraint(
        op.f("ck_invitations_reason_length"), "invitations", "char_length(reason) BETWEEN 1 AND 300"
    )
    op.create_check_constraint(
        op.f("ck_invitations_kind_values"), "invitations", "kind IN ('new', 'return', 'record')"
    )
    op.create_check_constraint(
        op.f("ck_invitations_kind_has_a_person"),
        "invitations",
        "(kind = 'new') = (person_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_invitations_proposed_username_length"),
        "invitations",
        "char_length(proposed_username) BETWEEN 1 AND 64",
    )


def downgrade() -> None:
    op.drop_column("person_acts", "invitation_id")
    op.execute(
        "UPDATE invitations SET cancelled_at = clock_timestamp() "
        "WHERE (kind = 'record' OR by_domo) AND accepted_at IS NULL AND cancelled_at IS NULL"
    )
    for name in (
        "proposed_username_length",
        "kind_has_a_person",
        "kind_values",
        "reason_length",
        "reason_of_domo",
        "by_domo_has_no_maker",
    ):
        op.drop_constraint(op.f(f"ck_invitations_{name}"), "invitations", type_="check")
    op.execute(
        f"UPDATE invitations SET created_by = coalesce({_OWNER}, link_for_id, "
        "(SELECT id FROM persons ORDER BY created_at, id LIMIT 1)) WHERE created_by IS NULL"
    )
    op.execute(
        "UPDATE persons AS p SET invited_by = i.created_by FROM invitations AS i "
        "WHERE i.by_domo AND i.kind = 'new' AND i.accepted_person_id = p.id "
        "AND p.invited_by IS NULL"
    )
    op.execute("UPDATE invitations SET person_id = NULL WHERE kind = 'record'")
    op.drop_column("invitations", "proposed_username")
    op.drop_column("invitations", "kind")
    op.drop_column("invitations", "reason")
    op.drop_column("invitations", "by_domo")
    op.alter_column("invitations", "created_by", existing_type=sa.Uuid(), nullable=False)
