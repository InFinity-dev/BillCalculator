"""immutable invoice snapshots and source uniqueness

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _assert_no_duplicate_sources(connection):
    for column in ("electric_bill_id", "water_bill_id", "common_bill_id"):
        duplicate = connection.execute(sa.text(
            "SELECT {0}, COUNT(*) FROM invoice_combination_items "
            "WHERE {0} IS NOT NULL GROUP BY {0} HAVING COUNT(*) > 1 LIMIT 1".format(column)
        )).first()
        if duplicate:
            raise RuntimeError(
                "중복 사용된 정산 원본이 있어 0002 마이그레이션을 중단합니다: "
                "{}={} ({}건)".format(column, duplicate[0], duplicate[1])
            )


def upgrade():
    connection = op.get_bind()
    _assert_no_duplicate_sources(connection)

    with op.batch_alter_table("common_bills") as batch_op:
        batch_op.add_column(sa.Column("request_token", sa.String(length=120), nullable=True))
    op.create_index(
        "uq_common_bills_request_token",
        "common_bills",
        ["request_token"],
        unique=True,
        sqlite_where=sa.text("request_token IS NOT NULL"),
    )

    for old_name in (
        "ix_invoice_combination_items_electric_bill_id",
        "ix_invoice_combination_items_water_bill_id",
        "ix_invoice_combination_items_common_bill_id",
    ):
        op.drop_index(old_name, table_name="invoice_combination_items")
    for name, column in (
        ("uq_invoice_items_electric_bill_once", "electric_bill_id"),
        ("uq_invoice_items_water_bill_once", "water_bill_id"),
        ("uq_invoice_items_common_bill_once", "common_bill_id"),
    ):
        op.create_index(
            name,
            "invoice_combination_items",
            [column],
            unique=True,
            sqlite_where=sa.text("{} IS NOT NULL".format(column)),
        )

    with op.batch_alter_table("final_invoices") as batch_op:
        batch_op.add_column(sa.Column("unit_name_snapshot", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("floor_number_snapshot", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("floor_name_snapshot", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("unit_profile_memo_snapshot", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_source", sa.String(length=30), nullable=True))

    connection.execute(sa.text(
        "UPDATE final_invoices SET "
        "unit_name_snapshot = (SELECT u.unit_name FROM units u WHERE u.id = final_invoices.unit_id), "
        "floor_number_snapshot = (SELECT f.floor_number FROM units u JOIN floors f ON f.id = u.floor_id "
        " WHERE u.id = final_invoices.unit_id), "
        "floor_name_snapshot = COALESCE((SELECT f.name FROM units u JOIN floors f ON f.id = u.floor_id "
        " WHERE u.id = final_invoices.unit_id), ''), "
        "unit_profile_memo_snapshot = COALESCE((SELECT u.memo FROM units u WHERE u.id = final_invoices.unit_id), ''), "
        "snapshot_source = 'legacy_current_master'"
    ))
    missing = connection.execute(sa.text(
        "SELECT COUNT(*) FROM final_invoices WHERE unit_name_snapshot IS NULL "
        "OR floor_number_snapshot IS NULL"
    )).scalar()
    if missing:
        raise RuntimeError("세대 참조를 찾을 수 없는 과거 정산서 {}건이 있어 마이그레이션을 중단합니다.".format(missing))

    with op.batch_alter_table("final_invoices") as batch_op:
        batch_op.alter_column("unit_name_snapshot", existing_type=sa.String(length=50), nullable=False)
        batch_op.alter_column("floor_number_snapshot", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("floor_name_snapshot", existing_type=sa.String(length=50), nullable=False)
        batch_op.alter_column("unit_profile_memo_snapshot", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("snapshot_source", existing_type=sa.String(length=30), nullable=False)


def downgrade():
    with op.batch_alter_table("final_invoices") as batch_op:
        batch_op.drop_column("snapshot_source")
        batch_op.drop_column("unit_profile_memo_snapshot")
        batch_op.drop_column("floor_name_snapshot")
        batch_op.drop_column("floor_number_snapshot")
        batch_op.drop_column("unit_name_snapshot")

    for name in (
        "uq_invoice_items_electric_bill_once",
        "uq_invoice_items_water_bill_once",
        "uq_invoice_items_common_bill_once",
    ):
        op.drop_index(name, table_name="invoice_combination_items")
    for name, column in (
        ("ix_invoice_combination_items_electric_bill_id", "electric_bill_id"),
        ("ix_invoice_combination_items_water_bill_id", "water_bill_id"),
        ("ix_invoice_combination_items_common_bill_id", "common_bill_id"),
    ):
        op.create_index(name, "invoice_combination_items", [column], unique=False)

    op.drop_index("uq_common_bills_request_token", table_name="common_bills")
    with op.batch_alter_table("common_bills") as batch_op:
        batch_op.drop_column("request_token")
