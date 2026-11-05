"""enrich calculation snapshots with immutable unit and floor identity

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


DETAIL_TABLES = (
    "electric_bill_details",
    "water_bill_details",
    "common_bill_details",
)
IDENTITY_KEYS = ("unit_id", "floor_id", "floor_number", "floor_name")


def _decode_snapshot(raw, table_name, row_id):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            "{}.id={} unit_snapshot을 해석할 수 없어 마이그레이션을 중단합니다.".format(
                table_name, row_id
            )
        )
    if not isinstance(value, dict):
        raise RuntimeError(
            "{}.id={} unit_snapshot이 객체가 아니어 마이그레이션을 중단합니다.".format(
                table_name, row_id
            )
        )
    return value


def upgrade():
    connection = op.get_bind()
    for table_name in DETAIL_TABLES:
        rows = connection.execute(sa.text(
            "SELECT d.id, d.unit_id, d.unit_snapshot, u.floor_id, "
            "f.floor_number, COALESCE(f.name, '') AS floor_name "
            "FROM {0} d JOIN units u ON u.id = d.unit_id "
            "JOIN floors f ON f.id = u.floor_id".format(table_name)
        )).mappings()
        for row in rows:
            snapshot = _decode_snapshot(row["unit_snapshot"], table_name, row["id"])
            snapshot.update({
                "unit_id": row["unit_id"],
                "floor_id": row["floor_id"],
                "floor_number": row["floor_number"],
                "floor_name": row["floor_name"],
            })
            connection.execute(
                sa.text("UPDATE {0} SET unit_snapshot = :snapshot WHERE id = :id".format(table_name)),
                {"snapshot": json.dumps(snapshot, ensure_ascii=False), "id": row["id"]},
            )


def downgrade():
    connection = op.get_bind()
    for table_name in DETAIL_TABLES:
        rows = connection.execute(
            sa.text("SELECT id, unit_snapshot FROM {}".format(table_name))
        ).mappings()
        for row in rows:
            snapshot = _decode_snapshot(row["unit_snapshot"], table_name, row["id"])
            for key in IDENTITY_KEYS:
                snapshot.pop(key, None)
            connection.execute(
                sa.text("UPDATE {0} SET unit_snapshot = :snapshot WHERE id = :id".format(table_name)),
                {"snapshot": json.dumps(snapshot, ensure_ascii=False), "id": row["id"]},
            )
