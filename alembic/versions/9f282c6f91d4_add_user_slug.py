"""add user slug

Revision ID: 9f282c6f91d4
Revises: 74ec3efdd65e
Create Date: 2022-03-30 12:04:46.110521

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9f282c6f91d4"
down_revision = "74ec3efdd65e"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("user", sa.Column("slug", sa.String(length=255), nullable=True))
    op.drop_constraint("user_name_key", "user", type_="unique")
    op.create_unique_constraint(None, "user", ["slug"])
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, "user", type_="unique")
    op.create_unique_constraint("user_name_key", "user", ["name"])
    op.drop_column("user", "slug")
    # ### end Alembic commands ###