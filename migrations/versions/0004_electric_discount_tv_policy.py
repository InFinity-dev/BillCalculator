"""store the electric discount and TV fee policy used by each calculation

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # electric_bills는 여러 회계 테이블이 FK로 참조한다. SQLite batch mode는
    # 원본 테이블을 DROP/재생성하므로 실제 참조 행이 있으면 foreign_keys=ON에서
    # 실패한다. SQLite가 지원하는 ADD COLUMN을 사용해 테이블을 재작성하지 않는다.
    op.add_column(
        "electric_bills",
        sa.Column(
            "discount_tv_policy",
            sa.String(length=30),
            sa.CheckConstraint(
                "discount_tv_policy IN ('COMBINED_FLOOR_ZERO', 'TV_PROTECTED')",
                name="discount_tv_policy_valid",
            ),
            nullable=False,
            server_default="COMBINED_FLOOR_ZERO",
        ),
    )


def downgrade():
    op.drop_column("electric_bills", "discount_tv_policy")
