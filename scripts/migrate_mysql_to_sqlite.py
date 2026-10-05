#!/usr/bin/env python
"""레거시 MySQL 데이터를 SQLite 로 이전한다 (일회성).

설계: docs/refactoring/database/02-migration-plan.md

안전 원칙
--------
1. **MySQL 원본을 절대 변경하지 않는다.** 세션을 READ ONLY 로 열고
   INSERT/UPDATE/DELETE/ALTER/DROP 을 한 줄도 실행하지 않는다.
2. 대상 SQLite 파일이 이미 존재하고 비어있지 않으면 거부한다 (``--force`` 필요).
3. 원본 스키마를 가정하지 않는다. ``information_schema`` 로 실제 컬럼을 조사한 뒤
   존재하는 컬럼만 읽는다.
4. 애매한 데이터를 임의로 변환하지 않는다. ``requires-review`` 로 리포트한다.
5. SQLite 측 전체가 하나의 트랜잭션이다.

사용법
-----
::

    pip install -r requirements-legacy.txt

    # 1) 검증만 (대상 DB 를 만들지 않는다)
    python scripts/migrate_mysql_to_sqlite.py \
        --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
        --report-only

    # 2) 실제 이전
    python scripts/migrate_mysql_to_sqlite.py \
        --mysql-uri "mysql+mysqlconnector://user:pw@localhost/bill_calculator" \
        --sqlite-path data/bill_calculator.db
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import DateTime as SQLDateTime, create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

#: 레거시 시스템이 이월 여부를 판정하던 문자열 규칙.
#: **이 스크립트에서 단 한 번만 사용된다.** 애플리케이션 코드는 더 이상 쓰지 않는다.
LEGACY_CARRYOVER_KEYWORDS = ("미납", "초과납부", "환급", "이월")

#: '[이월]' 접두사가 없으면서 키워드만 포함하는 항목은 사후 검토 대상으로 수집한다.
EXPLICIT_CARRYOVER_PREFIX = "[이월]"


class Report:
    """이전 과정에서 수집한 문제 목록."""

    def __init__(self):
        self.blocking = []
        self.warnings = []
        self.ambiguous_carryover = []
        self.counts = {}
        self.aggregates = {}
        self.balance_check = None

    def block(self, table, row_id, issue, detail=""):
        self.blocking.append(
            {"table": table, "id": row_id, "issue": issue, "detail": str(detail)}
        )

    def warn(self, table, row_id, issue, detail=""):
        self.warnings.append(
            {"table": table, "id": row_id, "issue": issue, "detail": str(detail)}
        )

    def as_dict(self, source, target):
        try:
            source = make_url(source).render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001
            source = "(접속 URI를 표시할 수 없음)"
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "target": str(target) if target else None,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "ambiguous_carryover": self.ambiguous_carryover,
            "counts": self.counts,
            "aggregates": self.aggregates,
            "balance_check": self.balance_check,
        }


# ---------------------------------------------------------------------------
# 값 변환
# ---------------------------------------------------------------------------
def to_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text_value = str(value).strip().replace(",", "")
    if text_value == "":
        return None
    return Decimal(text_value)


def to_money(value, report, table, row_id, column, default=0):
    """통화 컬럼 → 정수 원. 소수부가 있으면 blocking 으로 보고한다."""
    dec = to_decimal(value)
    if dec is None:
        return default
    if dec != dec.to_integral_value():
        report.block(
            table,
            row_id,
            "money_has_fraction",
            "{}={} — 통화 컬럼에 소수부가 있어 정수 원으로 변환하면 정보가 손실됩니다".format(
                column, dec
            ),
        )
        # 값을 임의로 반올림하지 않는다. 호출자는 blocking 이 있으면 중단한다.
        return int(dec.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return int(dec)


def to_exact(value, default="0.00"):
    """ExactDecimal 컬럼 → 소수 2자리 정규화 문자열."""
    dec = to_decimal(value)
    if dec is None:
        return Decimal(default)
    return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(int(value))
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y")


def normalize_month(value):
    """DATE/문자열을 그 달 1일로 정규화한다. 해석 불가 시 None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    text_value = str(value).strip()
    if not text_value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text_value[: len(fmt) + 2], fmt).date().replace(day=1)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_value).date().replace(day=1)
    except ValueError:
        return None


def normalize_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip()).date()
    except ValueError:
        return None


def normalize_datetime(value):
    """드라이버가 문자열로 반환한 레거시 DATETIME 을 모델 입력으로 변환한다."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("올바르지 않은 레거시 날짜/시간: {}".format(value))


def parse_json_column(value):
    """MySQL JSON 컬럼 값을 파이썬 객체로 만든다."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return json.loads(text_value)
    except (ValueError, TypeError):
        return None


def legacy_is_carryover(description):
    """레거시 문자열 규칙. 이 스크립트 밖에서는 사용하지 않는다."""
    desc = (description or "").lower()
    return any(k in desc for k in LEGACY_CARRYOVER_KEYWORDS)


# ---------------------------------------------------------------------------
# MySQL 읽기
# ---------------------------------------------------------------------------
class LegacySource:
    """레거시 DB 를 읽기 전용으로 조사/조회한다."""

    def __init__(self, uri):
        self.uri = uri
        self.engine = create_engine(uri)
        self._columns = {}

    def open(self):
        self.connection = self.engine.connect()
        # 원본을 절대 변경하지 않는다는 것을 엔진 수준에서도 강제한다.
        try:
            self.connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
        except Exception:  # noqa: BLE001 - SQLite 등 미지원 엔진 대비 (테스트용)
            pass
        return self

    def close(self):
        self.connection.close()
        self.engine.dispose()

    def columns(self, table):
        """실제 존재하는 컬럼 목록. 스키마를 가정하지 않기 위한 introspection."""
        if table not in self._columns:
            insp = inspect(self.engine)
            if table not in insp.get_table_names():
                self._columns[table] = []
            else:
                self._columns[table] = [c["name"] for c in insp.get_columns(table)]
        return self._columns[table]

    def has_table(self, table):
        return bool(self.columns(table))

    def rows(self, table, order_by="id"):
        """존재하는 컬럼만 SELECT 해서 dict 로 반환한다."""
        cols = self.columns(table)
        if not cols:
            return []
        column_sql = ", ".join("`{}`".format(c) for c in cols)
        order_sql = " ORDER BY `{}`".format(order_by) if order_by in cols else ""
        result = self.connection.execute(
            text("SELECT {} FROM `{}`{}".format(column_sql, table, order_sql))
        )
        return [dict(zip(cols, row)) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# 이전
# ---------------------------------------------------------------------------
class Migrator:
    def __init__(self, source, report, skip_invalid=False):
        self.source = source
        self.report = report
        self.skip_invalid = skip_invalid
        self.extracted = {}
        self.legacy_balances = {}

    # -- Extract + Transform ------------------------------------------------
    def extract(self):
        self._extract_settings()
        self._extract_floors()
        self._extract_units()
        self._extract_electric()
        self._extract_water()
        self._extract_common()
        self._extract_invoices()
        self._extract_payments()
        self._compute_legacy_balances()

    def _extract_settings(self):
        from settings_registry import SETTING_KEYS

        rows = self.source.rows("settings")
        kept = []
        for row in rows:
            key = row.get("setting_key")
            if key not in SETTING_KEYS:
                self.report.warn(
                    "settings", row.get("id"), "unknown_setting_key",
                    "레지스트리에 없는 키 '{}' 는 이전하지 않습니다".format(key),
                )
                continue
            kept.append({"setting_key": key, "setting_value": row.get("setting_value")})
        self.extracted["settings"] = kept

    def _extract_floors(self):
        rows = self.source.rows("floors")
        seen_numbers = {}
        out = []
        for row in rows:
            number = row.get("floor_number")
            if number in seen_numbers:
                self.report.block(
                    "floors", row["id"], "duplicate_floor_number",
                    "floor_number={} (id {} 와 충돌)".format(number, seen_numbers[number]),
                )
                continue
            seen_numbers[number] = row["id"]
            out.append(
                {
                    "id": row["id"],
                    "floor_number": number,
                    "name": row.get("name"),
                    # 레거시 스키마에 없을 수 있는 컬럼
                    "electric_contract_number": row.get("electric_contract_number"),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["floors"] = out

    def _extract_units(self):
        rows = self.source.rows("units")
        seen = {}
        out = []
        for row in rows:
            key = (row.get("floor_id"), (row.get("unit_name") or "").strip())
            if key in seen:
                self.report.block(
                    "units", row["id"], "duplicate_unit_name",
                    "floor_id={}, unit_name='{}' (id {} 와 충돌). "
                    "어느 쪽이 올바른지 코드로 판단할 수 없어 자동 변경하지 않습니다.".format(
                        key[0], key[1], seen[key]
                    ),
                )
                continue
            seen[key] = row["id"]
            residents = row.get("residents_count")
            residents = 1 if residents is None else int(residents)
            if residents < 0:
                self.report.block(
                    "units", row["id"], "negative_residents_count", residents
                )
            out.append(
                {
                    "id": row["id"],
                    "floor_id": row.get("floor_id"),
                    "unit_name": key[1],
                    "memo": row.get("memo"),
                    "electric_welfare": to_bool(row.get("electric_welfare")),
                    "electric_voucher": to_bool(row.get("electric_voucher")),
                    "has_tv": to_bool(row.get("has_tv"), default=True),
                    "water_welfare": to_bool(row.get("water_welfare")),
                    "residents_count": residents,
                    "is_vacant": to_bool(row.get("is_vacant")),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["units"] = out

    def _extract_electric(self):
        bills = []
        months = []
        seen_bill_key = {}
        for row in self.source.rows("electric_bills"):
            bill_id = row["id"]
            month = normalize_month(row.get("billing_month"))
            if month is None:
                self.report.block("electric_bills", bill_id, "invalid_billing_month",
                                  row.get("billing_month"))
                continue
            if row.get("billing_month") and normalize_date(row.get("billing_month")) != month:
                self.report.warn("electric_bills", bill_id, "billing_month_normalized",
                                 "{} -> {}".format(row.get("billing_month"), month))

            key = (row.get("floor_id"), month)
            if key in seen_bill_key:
                self.report.block(
                    "electric_bills", bill_id, "duplicate_floor_month",
                    "floor_id={}, billing_month={} (id {} 와 충돌)".format(
                        key[0], month, seen_bill_key[key]
                    ),
                )
                continue
            seen_bill_key[key] = bill_id

            mode = row.get("tv_distribution_mode")
            if mode not in ("INDIVIDUAL", "EQUAL"):
                self.report.warn("electric_bills", bill_id, "tv_mode_defaulted",
                                 "{} -> INDIVIDUAL".format(mode))
                mode = "INDIVIDUAL"

            tv_units_count = row.get("tv_units_count")
            if tv_units_count:
                self.report.warn(
                    "electric_bills", bill_id, "tv_units_count_dropped",
                    "값 {} — 이 컬럼은 읽는 코드가 없어 이전하지 않습니다".format(tv_units_count),
                )

            # monthly_details JSON → electric_bill_months 행
            details = parse_json_column(row.get("monthly_details")) or []
            seen_months = {}
            for index, item in enumerate(details):
                if not isinstance(item, dict):
                    self.report.block("electric_bills", bill_id, "monthly_detail_not_object",
                                      repr(item))
                    continue
                parsed = normalize_month(item.get("month"))
                if parsed is None:
                    self.report.block(
                        "electric_bills", bill_id, "monthly_detail_missing_month",
                        "monthly_details[{}].month={!r} — 고지월을 임의로 추정하지 않습니다 "
                        "(정산월과 고지월은 독립된 개념)".format(index, item.get("month")),
                    )
                    continue
                if parsed in seen_months:
                    self.report.block(
                        "electric_bills", bill_id, "monthly_detail_duplicate_month",
                        "{} 가 중복되었습니다".format(parsed),
                    )
                    continue
                seen_months[parsed] = index
                months.append(
                    {
                        "electric_bill_id": bill_id,
                        "billing_month": parsed,
                        "amount": to_money(item.get("amount"), self.report,
                                           "electric_bill_months", bill_id, "amount"),
                        "welfare_discount": to_money(item.get("welfare"), self.report,
                                                     "electric_bill_months", bill_id, "welfare"),
                        "voucher_discount": to_money(item.get("voucher"), self.report,
                                                     "electric_bill_months", bill_id, "voucher"),
                        "tv_fee": to_money(item.get("tv_fee"), self.report,
                                           "electric_bill_months", bill_id, "tv_fee"),
                        "created_at": row.get("created_at") or datetime.utcnow(),
                    }
                )

            bills.append(
                {
                    "id": bill_id,
                    "billing_month": month,
                    "floor_id": row.get("floor_id"),
                    "total_amount": to_money(row.get("total_amount"), self.report,
                                             "electric_bills", bill_id, "total_amount"),
                    "welfare_discount": to_money(row.get("welfare_discount"), self.report,
                                                 "electric_bills", bill_id, "welfare_discount"),
                    "voucher_discount": to_money(row.get("voucher_discount"), self.report,
                                                 "electric_bills", bill_id, "voucher_discount"),
                    "tv_fee_total": to_money(row.get("tv_fee_total"), self.report,
                                             "electric_bills", bill_id, "tv_fee_total"),
                    "tv_distribution_mode": mode,
                    "billing_months_count": int(row.get("billing_months_count") or len(seen_months) or 1),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["electric_bills"] = bills
        self.extracted["electric_bill_months"] = months

        self.extracted["electric_readings"] = self._extract_detail_rows(
            "electric_readings",
            key_columns=("electric_bill_id", "unit_id"),
            builder=self._build_reading,
        )
        self.extracted["electric_bill_details"] = self._extract_detail_rows(
            "electric_bill_details",
            key_columns=("electric_bill_id", "unit_id"),
            builder=self._build_electric_detail,
        )

    def _extract_detail_rows(self, table, key_columns, builder):
        out = []
        seen = {}
        for row in self.source.rows(table):
            key = tuple(row.get(c) for c in key_columns)
            if key in seen:
                self.report.block(
                    table, row["id"], "duplicate_detail",
                    "{}={} (id {} 와 충돌). 금액 이중계상 가능성이 있어 자동 병합하지 않습니다.".format(
                        key_columns, key, seen[key]
                    ),
                )
                continue
            seen[key] = row["id"]
            built = builder(row)
            if built is not None:
                out.append(built)
        return out

    def _build_reading(self, row):
        prev = to_exact(row.get("previous_reading"))
        curr = to_exact(row.get("current_reading"))
        for name, value in (("previous_reading", prev), ("current_reading", curr)):
            if value < 0:
                self.report.block("electric_readings", row["id"], "negative_reading",
                                  "{}={}".format(name, value))
        return {
            "id": row["id"],
            "electric_bill_id": row.get("electric_bill_id"),
            "unit_id": row.get("unit_id"),
            "previous_reading": prev,
            "current_reading": curr,
            "created_at": row.get("created_at") or datetime.utcnow(),
            "updated_at": row.get("updated_at") or datetime.utcnow(),
        }

    def _build_electric_detail(self, row):
        final_amount = to_exact(row.get("final_amount"))
        if final_amount < 0:
            self.report.block("electric_bill_details", row["id"], "negative_final_amount",
                              final_amount)
        charged = to_money(row.get("charged_amount"), self.report,
                           "electric_bill_details", row["id"], "charged_amount")
        if charged % 10 != 0:
            self.report.warn("electric_bill_details", row["id"], "charged_not_multiple_of_10",
                             charged)
        return {
            "id": row["id"],
            "electric_bill_id": row.get("electric_bill_id"),
            "unit_id": row.get("unit_id"),
            # 음수 사용량/배분액은 도메인상 허용된다. 검증하지 않는다.
            "usage_amount": to_exact(row.get("usage_amount")),
            "base_amount": to_exact(row.get("base_amount")),
            "welfare_discount": to_exact(row.get("welfare_discount")),
            "voucher_discount": to_exact(row.get("voucher_discount")),
            "tv_fee": to_exact(row.get("tv_fee")),
            "final_amount": final_amount,
            "charged_amount": charged,
            "unit_snapshot": self._check_snapshot(
                "electric_bill_details", row["id"], parse_json_column(row.get("unit_snapshot"))
            ),
            "created_at": row.get("created_at") or datetime.utcnow(),
        }

    def _check_snapshot(self, table, row_id, snapshot):
        required = (
            "unit_name", "electric_welfare", "electric_voucher",
            "has_tv", "water_welfare", "residents_count", "is_vacant",
        )
        if snapshot is None:
            self.report.warn(table, row_id, "snapshot_missing", "")
            return None
        if not isinstance(snapshot, dict):
            self.report.warn(table, row_id, "snapshot_not_object", repr(snapshot))
            return None
        missing = [k for k in required if k not in snapshot]
        if missing:
            # 값을 채워 넣지 않는다. 스냅샷은 당시 상태의 박제이므로 추정이 곧 왜곡이다.
            self.report.warn(table, row_id, "snapshot_missing_keys", ",".join(missing))
        return snapshot

    def _extract_water(self):
        bills = []
        seen_month = {}
        for row in self.source.rows("water_bills"):
            month = normalize_month(row.get("billing_month"))
            if month is None:
                self.report.block("water_bills", row["id"], "invalid_billing_month",
                                  row.get("billing_month"))
                continue
            if month in seen_month:
                self.report.block("water_bills", row["id"], "duplicate_billing_month",
                                  "{} (id {} 와 충돌)".format(month, seen_month[month]))
                continue
            seen_month[month] = row["id"]
            bills.append(
                {
                    "id": row["id"],
                    "billing_month": month,
                    "total_amount": to_money(row.get("total_amount"), self.report,
                                             "water_bills", row["id"], "total_amount"),
                    "welfare_discount_total": to_money(
                        row.get("welfare_discount_total"), self.report,
                        "water_bills", row["id"], "welfare_discount_total"),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["water_bills"] = bills

        self.extracted["water_bill_details"] = self._extract_detail_rows(
            "water_bill_details",
            key_columns=("water_bill_id", "unit_id"),
            builder=self._build_water_detail,
        )

    def _build_water_detail(self, row):
        charged = to_money(row.get("charged_amount"), self.report,
                           "water_bill_details", row["id"], "charged_amount")
        if charged % 10 != 0:
            self.report.warn("water_bill_details", row["id"], "charged_not_multiple_of_10",
                             charged)
        base = to_exact(row.get("base_amount"))
        final_amount = to_exact(row.get("final_amount"))
        for name, value in (("base_amount", base), ("final_amount", final_amount)):
            if value < 0:
                self.report.block("water_bill_details", row["id"], "negative_amount",
                                  "{}={}".format(name, value))
        return {
            "id": row["id"],
            "water_bill_id": row.get("water_bill_id"),
            "unit_id": row.get("unit_id"),
            "base_amount": base,
            "welfare_discount": to_exact(row.get("welfare_discount")),
            "final_amount": final_amount,
            "charged_amount": charged,
            "unit_snapshot": self._check_snapshot(
                "water_bill_details", row["id"], parse_json_column(row.get("unit_snapshot"))
            ),
            "is_excluded": to_bool(row.get("is_excluded")),
            "created_at": row.get("created_at") or datetime.utcnow(),
        }

    def _extract_common(self):
        bills = []
        for row in self.source.rows("common_bills"):
            month = normalize_month(row.get("billing_month"))
            if month is None:
                self.report.block("common_bills", row["id"], "invalid_billing_month",
                                  row.get("billing_month"))
                continue
            method = row.get("distribution_method")
            if method not in ("BY_RESIDENTS", "BY_UNITS"):
                self.report.warn("common_bills", row["id"], "distribution_method_defaulted",
                                 "{} -> BY_RESIDENTS".format(method))
                method = "BY_RESIDENTS"
            bills.append(
                {
                    "id": row["id"],
                    "billing_month": month,
                    "description": row.get("description"),
                    "total_amount": to_money(row.get("total_amount"), self.report,
                                             "common_bills", row["id"], "total_amount"),
                    "distribution_method": method,
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["common_bills"] = bills

        self.extracted["common_bill_details"] = self._extract_detail_rows(
            "common_bill_details",
            key_columns=("common_bill_id", "unit_id"),
            builder=self._build_common_detail,
        )

    def _build_common_detail(self, row):
        charged = to_money(row.get("charged_amount"), self.report,
                           "common_bill_details", row["id"], "charged_amount")
        if charged % 10 != 0:
            self.report.warn("common_bill_details", row["id"], "charged_not_multiple_of_10",
                             charged)
        amount = to_exact(row.get("amount"))
        if amount < 0:
            self.report.block("common_bill_details", row["id"], "negative_amount", amount)
        return {
            "id": row["id"],
            "common_bill_id": row.get("common_bill_id"),
            "unit_id": row.get("unit_id"),
            "amount": amount,
            "charged_amount": charged,
            "unit_snapshot": self._check_snapshot(
                "common_bill_details", row["id"], parse_json_column(row.get("unit_snapshot"))
            ),
            "created_at": row.get("created_at") or datetime.utcnow(),
        }

    def _extract_invoices(self):
        combos = []
        for row in self.source.rows("invoice_combinations"):
            combos.append(
                {
                    "id": row["id"],
                    "invoice_name": row.get("invoice_name") or "(이름 없음)",
                    "memo": row.get("memo"),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["invoice_combinations"] = combos

        electric_ids = {b["id"] for b in self.extracted.get("electric_bills", [])}
        water_ids = {b["id"] for b in self.extracted.get("water_bills", [])}
        common_ids = {b["id"] for b in self.extracted.get("common_bills", [])}
        id_sets = {"ELECTRIC": electric_ids, "WATER": water_ids, "COMMON": common_ids}
        fk_names = {
            "ELECTRIC": "electric_bill_id",
            "WATER": "water_bill_id",
            "COMMON": "common_bill_id",
        }

        available_columns = set(self.source.columns("invoice_combination_items"))
        legacy_single_column = (
            "item_id" in available_columns and "electric_bill_id" not in available_columns
        )

        items = []
        for row in self.source.rows("invoice_combination_items"):
            row_id = row["id"]
            item_type = row.get("item_type")
            if item_type not in id_sets:
                self.report.block("invoice_combination_items", row_id, "invalid_item_type",
                                  item_type)
                continue

            if legacy_single_column:
                # SQLSchema.txt 기준으로 만들어진 DB (item_id 단일 컬럼)
                bill_id = row.get("item_id")
                fks = {name: None for name in fk_names.values()}
                fks[fk_names[item_type]] = bill_id
            else:
                fks = {name: row.get(name) for name in fk_names.values()}
                filled = [k for k, v in fks.items() if v is not None]
                expected = fk_names[item_type]
                if filled != [expected]:
                    self.report.block(
                        "invoice_combination_items", row_id, "polymorphic_mismatch",
                        "item_type={} 인데 채워진 FK={}".format(item_type, filled),
                    )
                    continue
                bill_id = fks[expected]

            if bill_id is None or bill_id not in id_sets[item_type]:
                self.report.block(
                    "invoice_combination_items", row_id, "dangling_bill_reference",
                    "item_type={}, bill_id={}".format(item_type, bill_id),
                )
                continue

            month = normalize_month(row.get("billing_month"))
            if month is None:
                self.report.block("invoice_combination_items", row_id,
                                  "invalid_billing_month", row.get("billing_month"))
                continue

            items.append(
                {
                    "id": row_id,
                    "combination_id": row.get("combination_id"),
                    "item_type": item_type,
                    "billing_month": month,
                    "item_description": (row.get("item_description") or "")[:200],
                    **fks,
                }
            )
        self.extracted["invoice_combination_items"] = items

        combo_memos = {c["id"]: c["memo"] for c in combos}
        invoices = []
        charges = []
        seen_invoice_key = {}
        for row in self.source.rows("final_invoices"):
            row_id = row["id"]
            key = (row.get("combination_id"), row.get("unit_id"))
            if key in seen_invoice_key:
                self.report.block(
                    "final_invoices", row_id, "duplicate_combination_unit",
                    "{} (id {} 와 충돌)".format(key, seen_invoice_key[key]),
                )
                continue
            seen_invoice_key[key] = row_id

            legacy_memo = row.get("memo")
            expected_memo = combo_memos.get(row.get("combination_id"))
            if legacy_memo and legacy_memo != expected_memo:
                self.report.warn(
                    "final_invoices", row_id, "memo_differs_from_combination",
                    "final_invoices.memo 컬럼은 이전하지 않습니다. "
                    "조합 메모와 내용이 달라 확인이 필요합니다.",
                )

            invoices.append(
                {
                    "id": row_id,
                    "combination_id": row.get("combination_id"),
                    "unit_id": row.get("unit_id"),
                    "electric_amount": to_money(row.get("electric_amount"), self.report,
                                                "final_invoices", row_id, "electric_amount"),
                    "water_amount": to_money(row.get("water_amount"), self.report,
                                             "final_invoices", row_id, "water_amount"),
                    "common_amount": to_money(row.get("common_amount"), self.report,
                                              "final_invoices", row_id, "common_amount"),
                    "common_details": parse_json_column(row.get("common_details")),
                    "total_amount": to_money(row.get("total_amount"), self.report,
                                             "final_invoices", row_id, "total_amount"),
                    "unit_memo": row.get("unit_memo"),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                }
            )

            for order, charge in enumerate(parse_json_column(row.get("additional_charges")) or []):
                if not isinstance(charge, dict):
                    self.report.block("final_invoices", row_id, "charge_not_object",
                                      repr(charge))
                    continue
                description = (charge.get("description") or "").strip()
                if not description:
                    description = "(설명 없음)"
                    self.report.warn("final_invoices", row_id, "charge_missing_description",
                                     "index={}".format(order))
                amount = to_money(charge.get("amount"), self.report,
                                  "final_invoice_charges", row_id, "amount")
                is_carryover = legacy_is_carryover(description)

                # '[이월]' 접두사 없이 키워드만 포함하는 항목은 사후 검토 대상이다.
                # 레거시 시스템이 이 항목을 잔액에서 제외해 왔으므로 동일하게 True 로 이전해야
                # balance 가 보존된다. 임의로 False 로 바꾸면 데이터 왜곡이다.
                if is_carryover and not description.startswith(EXPLICIT_CARRYOVER_PREFIX):
                    self.report.ambiguous_carryover.append(
                        {
                            "final_invoice_id": row_id,
                            "description": description,
                            "amount": amount,
                            "assigned_is_carryover": True,
                            "note": "레거시 문자열 규칙상 잔액에서 제외되어 왔습니다. "
                                    "실제 이월 항목이 맞는지 확인하세요.",
                        }
                    )

                charges.append(
                    {
                        "final_invoice_id": row_id,
                        "description": description[:200],
                        "amount": amount,
                        "is_carryover": is_carryover,
                        "sort_order": order,
                        "created_at": row.get("created_at") or datetime.utcnow(),
                    }
                )

        self.extracted["final_invoices"] = invoices
        self.extracted["final_invoice_charges"] = charges

    def _extract_payments(self):
        out = []
        for row in self.source.rows("payments"):
            payment_date = normalize_date(row.get("payment_date"))
            if payment_date is None:
                self.report.block("payments", row["id"], "invalid_payment_date",
                                  row.get("payment_date"))
                continue
            amount = to_money(row.get("payment_amount"), self.report,
                              "payments", row["id"], "payment_amount")
            if amount < 0:
                self.report.block("payments", row["id"], "negative_payment_amount", amount)
            out.append(
                {
                    "id": row["id"],
                    "combination_id": row.get("combination_id"),
                    "unit_id": row.get("unit_id"),
                    "payment_date": payment_date,
                    "payment_amount": amount,
                    "payment_method": row.get("payment_method") or "계좌이체",
                    "memo": row.get("memo"),
                    "created_at": row.get("created_at") or datetime.utcnow(),
                    "updated_at": row.get("updated_at") or datetime.utcnow(),
                }
            )
        self.extracted["payments"] = out

    def _compute_legacy_balances(self):
        """레거시 알고리즘(문자열 판정)으로 세대별 잔액을 계산한다.

        이전 후 신규 알고리즘(is_carryover 컬럼) 결과와 대조하기 위한 기준값이다.
        """
        charges_by_invoice = {}
        for charge in self.extracted.get("final_invoice_charges", []):
            charges_by_invoice.setdefault(charge["final_invoice_id"], []).append(charge)

        billed = {}
        for invoice in self.extracted.get("final_invoices", []):
            unit_id = invoice["unit_id"]
            amount = (
                invoice["electric_amount"]
                + invoice["water_amount"]
                + invoice["common_amount"]
            )
            for charge in charges_by_invoice.get(invoice["id"], []):
                # 레거시 규칙 그대로: 문자열로 판정한 이월 항목은 제외
                if not legacy_is_carryover(charge["description"]):
                    amount += charge["amount"]
            billed[unit_id] = billed.get(unit_id, 0) + amount

        paid = {}
        for payment in self.extracted.get("payments", []):
            paid[payment["unit_id"]] = paid.get(payment["unit_id"], 0) + payment["payment_amount"]

        units = {u["id"] for u in self.extracted.get("units", [])}
        self.legacy_balances = {
            unit_id: billed.get(unit_id, 0) - paid.get(unit_id, 0) for unit_id in units
        }


# ---------------------------------------------------------------------------
# SQLite 적재
# ---------------------------------------------------------------------------
def load_into_sqlite(migrator, sqlite_path, report):
    """추출·변환된 데이터를 SQLite 에 적재하고 검증한다."""
    os.environ["BILLCALC_DB_PATH"] = str(sqlite_path)
    # app 을 늦게 import 해야 위 환경변수가 반영된다.
    from flask_migrate import upgrade

    import app as app_module
    from extensions import db
    from models import (
        CommonBill, CommonBillDetail, ElectricBill, ElectricBillDetail,
        ElectricBillMonth, ElectricReading, FinalInvoice, FinalInvoiceCharge,
        Floor, InvoiceCombination, InvoiceCombinationItem, Payment, Setting,
        Unit, WaterBill, WaterBillDetail,
    )

    application = app_module.app
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///{}".format(sqlite_path)

    model_by_table = {
        "settings": Setting,
        "floors": Floor,
        "units": Unit,
        "electric_bills": ElectricBill,
        "electric_bill_months": ElectricBillMonth,
        "electric_readings": ElectricReading,
        "electric_bill_details": ElectricBillDetail,
        "water_bills": WaterBill,
        "water_bill_details": WaterBillDetail,
        "common_bills": CommonBill,
        "common_bill_details": CommonBillDetail,
        "invoice_combinations": InvoiceCombination,
        "invoice_combination_items": InvoiceCombinationItem,
        "final_invoices": FinalInvoice,
        "final_invoice_charges": FinalInvoiceCharge,
        "payments": Payment,
    }

    with application.app_context():
        # 스키마는 반드시 마이그레이션으로 만든다 (create_all 사용 금지).
        upgrade()

        try:
            for table, model in model_by_table.items():
                datetime_columns = {
                    column.name for column in model.__table__.columns
                    if isinstance(column.type, SQLDateTime)
                }
                for row in migrator.extracted.get(table, []):
                    converted = {
                        key: normalize_datetime(value) if key in datetime_columns else value
                        for key, value in row.items()
                    }
                    db.session.add(model(**converted))
                db.session.flush()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        _verify(migrator, report, db, model_by_table)
        db.session.remove()
        db.engine.dispose()


def _verify(migrator, report, db, model_by_table):
    """이전 결과를 검증한다: row count, 금액 aggregate, 세대별 balance."""
    from sqlalchemy import func

    import balance as balance_service
    from models import (
        CommonBillDetail, CommonBill, ElectricBill, ElectricBillDetail,
        FinalInvoice, Payment, Unit, WaterBill, WaterBillDetail,
    )

    # 1) row count
    for table, model in model_by_table.items():
        expected = len(migrator.extracted.get(table, []))
        actual = db.session.query(func.count(model.id)).scalar()
        report.counts[table] = {"expected": expected, "actual": actual,
                                "ok": expected == actual}
        if expected != actual:
            report.block(table, None, "row_count_mismatch",
                         "expected={} actual={}".format(expected, actual))

    # 2) 금액 aggregate
    aggregates = {
        "sum_electric_charged": (ElectricBillDetail, ElectricBillDetail.charged_amount),
        "sum_water_charged": (WaterBillDetail, WaterBillDetail.charged_amount),
        "sum_common_charged": (CommonBillDetail, CommonBillDetail.charged_amount),
        "sum_final_invoice_total": (FinalInvoice, FinalInvoice.total_amount),
        "sum_payments": (Payment, Payment.payment_amount),
        "sum_electric_bill_total": (ElectricBill, ElectricBill.total_amount),
        "sum_water_bill_total": (WaterBill, WaterBill.total_amount),
        "sum_common_bill_total": (CommonBill, CommonBill.total_amount),
    }
    source_columns = {
        "sum_electric_charged": ("electric_bill_details", "charged_amount"),
        "sum_water_charged": ("water_bill_details", "charged_amount"),
        "sum_common_charged": ("common_bill_details", "charged_amount"),
        "sum_final_invoice_total": ("final_invoices", "total_amount"),
        "sum_payments": ("payments", "payment_amount"),
        "sum_electric_bill_total": ("electric_bills", "total_amount"),
        "sum_water_bill_total": ("water_bills", "total_amount"),
        "sum_common_bill_total": ("common_bills", "total_amount"),
    }
    for name, (_model, column) in aggregates.items():
        table, field = source_columns[name]
        expected = sum(r.get(field, 0) or 0 for r in migrator.extracted.get(table, []))
        actual = int(db.session.query(func.coalesce(func.sum(column), 0)).scalar() or 0)
        report.aggregates[name] = {"expected": expected, "actual": actual,
                                   "ok": expected == actual}
        if expected != actual:
            report.block(table, None, "aggregate_mismatch",
                         "{}: expected={} actual={}".format(name, expected, actual))

    # 3) 세대별 balance — 레거시 문자열 알고리즘 vs 신규 is_carryover 알고리즘
    mismatches = []
    units = Unit.query.all()
    new_balances = balance_service.all_unit_balances(units)
    for unit in units:
        legacy = migrator.legacy_balances.get(unit.id, 0)
        new = new_balances[unit.id]["balance"]
        if legacy != new:
            mismatches.append(
                {"unit_id": unit.id, "legacy_balance": legacy, "new_balance": new}
            )
            report.block("balance", unit.id, "balance_mismatch",
                         "legacy={} new={}".format(legacy, new))
    report.balance_check = {
        "units_checked": len(units),
        "mismatches": mismatches,
        "ok": not mismatches,
    }


def _remove_stage_files(path):
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="레거시 MySQL → SQLite 데이터 이전")
    parser.add_argument("--mysql-uri", required=True,
                        help="SQLAlchemy URI. 예: mysql+mysqlconnector://user:pw@host/bill_calculator")
    parser.add_argument("--sqlite-path", default="data/bill_calculator.db")
    parser.add_argument("--report", default="data/migration_report.json")
    parser.add_argument("--report-only", action="store_true",
                        help="검증만 수행하고 대상 DB 를 만들지 않는다")
    parser.add_argument("--skip-invalid", action="store_true",
                        help="blocking 위반이 있어도 진행한다 (리포트 확인 필수)")
    parser.add_argument("--force", action="store_true",
                        help="대상 파일이 이미 존재해도 덮어쓴다")
    args = parser.parse_args(argv)

    sqlite_path = Path(args.sqlite_path).resolve()
    report = Report()

    if not args.report_only:
        if sqlite_path.exists() and sqlite_path.stat().st_size > 0 and not args.force:
            print("대상 파일이 이미 존재합니다: {}".format(sqlite_path))
            print("덮어쓰려면 --force 를 지정하세요. (먼저 백업을 권장합니다)")
            return 2

    source = LegacySource(args.mysql_uri).open()
    try:
        migrator = Migrator(source, report, skip_invalid=args.skip_invalid)
        migrator.extract()
    finally:
        source.close()

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        report_path.write_text(
            json.dumps(report.as_dict(args.mysql_uri, None), ensure_ascii=False,
                       indent=2, default=str),
            encoding="utf-8",
        )
        _print_summary(report, report_path)
        return 0 if not report.blocking else 1

    if report.blocking and not args.skip_invalid:
        report_path.write_text(
            json.dumps(report.as_dict(args.mysql_uri, sqlite_path), ensure_ascii=False,
                       indent=2, default=str),
            encoding="utf-8",
        )
        print("검토가 필요한 문제 {}건이 있어 이전을 중단했습니다.".format(len(report.blocking)))
        print("리포트: {}".format(report_path))
        print("확인 후 그래도 진행하려면 --skip-invalid 를 지정하세요.")
        return 1

    if any(Path(str(sqlite_path) + suffix).exists() for suffix in ("-wal", "-shm")):
        print("대상 DB의 WAL 파일이 남아 있습니다. 앱을 종료하고 체크포인트 후 다시 실행하세요.")
        return 2

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    stage_fd, stage_name = tempfile.mkstemp(
        prefix=".billcalc-migration-", suffix=".db", dir=str(sqlite_path.parent)
    )
    os.close(stage_fd)
    staged_path = Path(stage_name)

    try:
        load_into_sqlite(migrator, staged_path, report)
        # 임시 DB의 WAL 을 본 파일에 반영한 뒤 단일 파일로 교체한다.
        with sqlite3.connect(str(staged_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
    except Exception as exc:  # noqa: BLE001
        report.block("__load__", None, "load_failed", exc)
        _remove_stage_files(staged_path)
        report_path.write_text(
            json.dumps(report.as_dict(args.mysql_uri, sqlite_path), ensure_ascii=False,
                       indent=2, default=str),
            encoding="utf-8",
        )
        print("적재 중 실패했습니다: {}".format(exc))
        print("리포트: {}".format(report_path))
        return 1

    report_path.write_text(
        json.dumps(report.as_dict(args.mysql_uri, sqlite_path), ensure_ascii=False,
                   indent=2, default=str),
        encoding="utf-8",
    )
    _print_summary(report, report_path)

    verification_failed = any(
        not c["ok"] for c in report.counts.values()
    ) or any(not a["ok"] for a in report.aggregates.values()) or (
        report.balance_check and not report.balance_check["ok"]
    )
    if verification_failed:
        _remove_stage_files(staged_path)
        print("검증에 실패했습니다. 기존 대상 DB와 MySQL 원본은 그대로입니다.")
        return 1

    try:
        os.replace(str(staged_path), str(sqlite_path))
    except OSError as exc:
        _remove_stage_files(staged_path)
        print("검증된 DB 교체에 실패했습니다. 기존 대상 DB는 그대로입니다: {}".format(exc))
        return 1

    print("이전 완료: {}".format(sqlite_path))
    print("MySQL 원본은 변경되지 않았습니다. 직접 확인 후 삭제하세요.")
    return 0


def _print_summary(report, report_path):
    print("검토 필요(blocking): {}건".format(len(report.blocking)))
    print("경고(warning): {}건".format(len(report.warnings)))
    print("이월 판정 검토 대상: {}건".format(len(report.ambiguous_carryover)))
    if report.balance_check:
        status = "일치" if report.balance_check["ok"] else "불일치"
        print("세대별 잔액 대조: {} ({}세대)".format(status, report.balance_check["units_checked"]))
    print("리포트: {}".format(report_path))


if __name__ == "__main__":
    sys.exit(main())
