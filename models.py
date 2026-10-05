"""도메인 모델.

이 파일 + ``migrations/`` 의 Alembic 이력이 스키마의 Single Source of Truth 다.
``SQLSchema.txt`` 는 더 이상 스키마 생성 수단이 아니다.

설계 근거는 ``docs/refactoring/database/01-target-sqlite-schema.md`` 참조.

핵심 규칙
--------
1. 회계성 데이터(계산/정산/납부)는 마스터 삭제로 연쇄 삭제되지 않는다 (FK RESTRICT).
2. 금액은 부동소수점으로 저장하지 않는다 (``Money`` / ``ExactDecimal``).
3. ``billing_month`` 는 항상 그 달 1일로 정규화된다 (validator + CHECK).
4. 이월 여부는 문자열이 아니라 ``FinalInvoiceCharge.is_carryover`` 컬럼이 보존한다.
5. ``unit_snapshot`` 은 계산 시점 세대 상태의 불변 박제다. JSON 을 유지한다.
"""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import validates

from db_types import ExactDecimal, Money
from extensions import db

# ---------------------------------------------------------------------------
# 값 집합 (SQLite 는 ENUM 이 없으므로 CHECK 제약으로 강제한다)
# ---------------------------------------------------------------------------
TV_DISTRIBUTION_MODES = ("INDIVIDUAL", "EQUAL")
DISTRIBUTION_METHODS = ("BY_RESIDENTS", "BY_UNITS")
INVOICE_ITEM_TYPES = ("ELECTRIC", "WATER", "COMMON")


def _in_check(column, values):
    """``column IN ('A','B')`` CHECK 절 문자열을 만든다."""
    joined = ", ".join("'{}'".format(v) for v in values)
    return "{} IN ({})".format(column, joined)


def _day_is_first(column):
    """``billing_month`` 가 그 달 1일인지 검사하는 CHECK 절.

    SQLAlchemy ``Date`` 는 SQLite 에 ``'YYYY-MM-DD'`` 문자열로 저장되므로
    9~10번째 문자가 일자다.
    """
    return "substr({}, 9, 2) = '01'".format(column)


def _non_negative_decimal(column):
    """TEXT 로 저장된 ExactDecimal 의 부호 검사.

    TEXT 를 정수와 직접 비교하면 SQLite 의 타입 우선순위(INTEGER < TEXT) 때문에
    항상 참이 되므로 반드시 CAST 가 필요하다.
    """
    return "CAST({} AS REAL) >= 0".format(column)


class BillingMonthMixin:
    """``billing_month`` 를 그 달 1일로 정규화한다."""

    @validates("billing_month")
    def _normalize_billing_month(self, key, value):
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date) and value.day != 1:
            return value.replace(day=1)
        return value


class TimestampMixin:
    """생성/수정 시각. 기존 동작(naive UTC)을 그대로 유지한다."""

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# 마스터
# ---------------------------------------------------------------------------
class Floor(TimestampMixin, db.Model):
    __tablename__ = "floors"

    id = db.Column(db.Integer, primary_key=True)
    # 지하층은 음수로 표현한다. 따라서 부호 CHECK 를 걸지 않는다.
    floor_number = db.Column(db.Integer, nullable=False, unique=True)
    name = db.Column(db.String(50))
    electric_contract_number = db.Column(db.String(50))

    units = db.relationship(
        "Unit",
        back_populates="floor",
        cascade="all, delete-orphan",
        passive_deletes=True,          # DB 의 ON DELETE CASCADE 에 위임
        order_by="Unit.unit_name",
    )
    electric_bills = db.relationship(
        "ElectricBill",
        back_populates="floor_ref",
        passive_deletes="all",         # 회계 데이터: DB 의 RESTRICT 가 그대로 발동
    )


class Unit(TimestampMixin, db.Model):
    __tablename__ = "units"
    __table_args__ = (
        UniqueConstraint("floor_id", "unit_name", name="uq_units_floor_id_unit_name"),
        CheckConstraint("residents_count >= 0", name="residents_count_non_negative"),
        Index("ix_units_floor_id", "floor_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    floor_id = db.Column(
        db.Integer, db.ForeignKey("floors.id", ondelete="CASCADE"), nullable=False
    )
    unit_name = db.Column(db.String(50), nullable=False)
    memo = db.Column(db.Text)
    electric_welfare = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    electric_voucher = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    has_tv = db.Column(db.Boolean, nullable=False, default=True, server_default=text("1"))
    water_welfare = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    residents_count = db.Column(
        db.Integer, nullable=False, default=1, server_default=text("1")
    )
    is_vacant = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )

    floor = db.relationship("Floor", back_populates="units")

    # 아래는 전부 회계성 데이터다. passive_deletes='all' 로 SQLAlchemy 가
    # FK 를 NULL 로 바꾸려 시도하지 않게 하고, DB 의 RESTRICT 가 그대로 발동하게 한다.
    electric_readings = db.relationship(
        "ElectricReading", back_populates="unit", passive_deletes="all"
    )
    electric_bill_details = db.relationship(
        "ElectricBillDetail", back_populates="unit", passive_deletes="all"
    )
    water_bill_details = db.relationship(
        "WaterBillDetail", back_populates="unit", passive_deletes="all"
    )
    common_bill_details = db.relationship(
        "CommonBillDetail", back_populates="unit", passive_deletes="all"
    )
    final_invoices = db.relationship(
        "FinalInvoice", back_populates="unit", passive_deletes="all"
    )
    payments = db.relationship("Payment", back_populates="unit", passive_deletes="all")


class Setting(TimestampMixin, db.Model):
    """설정 key-value 저장소.

    테이블 구조를 typed 컬럼으로 바꾸지 않은 이유는
    docs/refactoring/database/01-target-sqlite-schema.md 11절 참조.
    키·타입·기본값의 단일 정의는 ``settings_registry.py`` 가 담당한다.
    """

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(50), unique=True, nullable=False)
    # VARCHAR(255) 였으나 정산서 고정 메모/푸터가 255자를 넘을 수 있어 TEXT 로 넓힌다.
    setting_value = db.Column(db.Text)


# ---------------------------------------------------------------------------
# 전기
# ---------------------------------------------------------------------------
class ElectricBill(BillingMonthMixin, TimestampMixin, db.Model):
    __tablename__ = "electric_bills"
    __table_args__ = (
        UniqueConstraint(
            "floor_id", "billing_month", name="uq_electric_bills_floor_id_billing_month"
        ),
        CheckConstraint(_day_is_first("billing_month"), name="billing_month_first_day"),
        CheckConstraint(
            _in_check("tv_distribution_mode", TV_DISTRIBUTION_MODES),
            name="tv_distribution_mode_valid",
        ),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint("welfare_discount >= 0", name="welfare_discount_non_negative"),
        CheckConstraint("voucher_discount >= 0", name="voucher_discount_non_negative"),
        CheckConstraint("tv_fee_total >= 0", name="tv_fee_total_non_negative"),
        CheckConstraint(
            "billing_months_count >= 0", name="billing_months_count_non_negative"
        ),
        Index("ix_electric_bills_billing_month", "billing_month"),
    )

    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False)
    floor_id = db.Column(
        db.Integer, db.ForeignKey("floors.id", ondelete="RESTRICT"), nullable=False
    )
    total_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    welfare_discount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    voucher_discount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    tv_fee_total = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    tv_distribution_mode = db.Column(
        db.String(20), nullable=False, default="INDIVIDUAL", server_default="INDIVIDUAL"
    )
    billing_months_count = db.Column(
        db.Integer, nullable=False, default=1, server_default=text("1")
    )

    floor_ref = db.relationship("Floor", back_populates="electric_bills")
    months = db.relationship(
        "ElectricBillMonth",
        back_populates="electric_bill",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ElectricBillMonth.billing_month",
    )
    readings = db.relationship(
        "ElectricReading",
        back_populates="electric_bill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    details = db.relationship(
        "ElectricBillDetail",
        back_populates="electric_bill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invoice_items = db.relationship(
        "InvoiceCombinationItem",
        back_populates="electric_bill_ref",
        passive_deletes="all",
    )

    @property
    def monthly_details(self):
        """레거시 ``monthly_details`` JSON 과 동일한 읽기 전용 표현.

        템플릿과 조회 페이지 차트가 이 형태를 소비한다.
        저장은 ``months`` 관계로 이루어진다.
        """
        return [m.as_legacy_dict() for m in self.months]


class ElectricBillMonth(BillingMonthMixin, db.Model):
    """전기 고지월별 명세.

    기존 ``electric_bills.monthly_details`` JSON 을 대체한다.
    JSON 시절 ``month`` 필드는 문자열('YYYY-MM')과 date 객체가 혼재해
    템플릿마다 타입 분기가 필요했다. DATE 컬럼으로 확정해 그 문제를 제거한다.

    정렬 순서용 컬럼을 두지 않는다. UNIQUE(bill, month) 로 월 중복이 불가능하므로
    ``billing_month`` 오름차순이 전순서를 이루며, 도메인상 유일하게 올바른 표시 순서다.
    """

    __tablename__ = "electric_bill_months"
    __table_args__ = (
        UniqueConstraint(
            "electric_bill_id",
            "billing_month",
            name="uq_electric_bill_months_electric_bill_id_billing_month",
        ),
        CheckConstraint(_day_is_first("billing_month"), name="billing_month_first_day"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("welfare_discount >= 0", name="welfare_discount_non_negative"),
        CheckConstraint("voucher_discount >= 0", name="voucher_discount_non_negative"),
        CheckConstraint("tv_fee >= 0", name="tv_fee_non_negative"),
        Index("ix_electric_bill_months_billing_month", "billing_month"),
    )

    id = db.Column(db.Integer, primary_key=True)
    electric_bill_id = db.Column(
        db.Integer,
        db.ForeignKey("electric_bills.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing_month = db.Column(db.Date, nullable=False)
    amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    welfare_discount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    voucher_discount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    tv_fee = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    electric_bill = db.relationship("ElectricBill", back_populates="months")

    def as_legacy_dict(self):
        """기존 ``monthly_details`` JSON 항목과 동일한 형태로 직렬화한다.

        ``month`` 은 레거시가 저장하던 형식(``'YYYY-MM'``, ``<input type="month">`` 값)
        을 그대로 재현한다. 덕분에 이 값을 소비하는 5개 템플릿과 조회 페이지 차트를
        수정하지 않아도 된다. 동시에 레거시에 섞여 있던 date 객체 형태는 사라진다.
        """
        return {
            "month": self.billing_month.strftime("%Y-%m"),
            "amount": self.amount,
            "welfare": self.welfare_discount,
            "voucher": self.voucher_discount,
            "tv_fee": self.tv_fee,
        }


class ElectricReading(db.Model):
    __tablename__ = "electric_readings"
    __table_args__ = (
        UniqueConstraint(
            "electric_bill_id",
            "unit_id",
            name="uq_electric_readings_electric_bill_id_unit_id",
        ),
        CheckConstraint(
            _non_negative_decimal("previous_reading"), name="previous_reading_non_negative"
        ),
        CheckConstraint(
            _non_negative_decimal("current_reading"), name="current_reading_non_negative"
        ),
        Index("ix_electric_readings_unit_id", "unit_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    electric_bill_id = db.Column(
        db.Integer,
        db.ForeignKey("electric_bills.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    previous_reading = db.Column(ExactDecimal(), nullable=False)
    current_reading = db.Column(ExactDecimal(), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    electric_bill = db.relationship("ElectricBill", back_populates="readings")
    unit = db.relationship("Unit", back_populates="electric_readings")


class ElectricBillDetail(db.Model):
    """세대별 전기 배분 결과.

    ``usage_amount`` 와 ``base_amount`` 에는 부호 CHECK 를 걸지 않는다.
    신규 계산 요청은 음수 사용량을 거절하지만, 레거시 자료의 정합성 검사와
    이전 과정에서 기존 기록을 읽을 수 있도록 DB 컬럼의 부호는 유지한다.
    """

    __tablename__ = "electric_bill_details"
    __table_args__ = (
        UniqueConstraint(
            "electric_bill_id",
            "unit_id",
            name="uq_electric_bill_details_electric_bill_id_unit_id",
        ),
        CheckConstraint(
            _non_negative_decimal("welfare_discount"), name="welfare_discount_non_negative"
        ),
        CheckConstraint(
            _non_negative_decimal("voucher_discount"), name="voucher_discount_non_negative"
        ),
        CheckConstraint(_non_negative_decimal("tv_fee"), name="tv_fee_non_negative"),
        CheckConstraint(
            _non_negative_decimal("final_amount"), name="final_amount_non_negative"
        ),
        CheckConstraint("charged_amount >= 0", name="charged_amount_non_negative"),
        Index("ix_electric_bill_details_unit_id", "unit_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    electric_bill_id = db.Column(
        db.Integer,
        db.ForeignKey("electric_bills.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    usage_amount = db.Column(ExactDecimal(), nullable=False)
    base_amount = db.Column(ExactDecimal(), nullable=False)
    welfare_discount = db.Column(
        ExactDecimal(), nullable=False, default=0, server_default="0.00"
    )
    voucher_discount = db.Column(
        ExactDecimal(), nullable=False, default=0, server_default="0.00"
    )
    tv_fee = db.Column(ExactDecimal(), nullable=False, default=0, server_default="0.00")
    final_amount = db.Column(ExactDecimal(), nullable=False)
    charged_amount = db.Column(Money, nullable=False)
    unit_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    electric_bill = db.relationship("ElectricBill", back_populates="details")
    unit = db.relationship("Unit", back_populates="electric_bill_details")


# ---------------------------------------------------------------------------
# 수도
# ---------------------------------------------------------------------------
class WaterBill(BillingMonthMixin, TimestampMixin, db.Model):
    __tablename__ = "water_bills"
    __table_args__ = (
        CheckConstraint(_day_is_first("billing_month"), name="billing_month_first_day"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(
            "welfare_discount_total >= 0", name="welfare_discount_total_non_negative"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False, unique=True)
    total_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    welfare_discount_total = db.Column(
        Money, nullable=False, default=0, server_default=text("0")
    )

    details = db.relationship(
        "WaterBillDetail",
        back_populates="water_bill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invoice_items = db.relationship(
        "InvoiceCombinationItem", back_populates="water_bill_ref", passive_deletes="all"
    )


class WaterBillDetail(db.Model):
    __tablename__ = "water_bill_details"
    __table_args__ = (
        UniqueConstraint(
            "water_bill_id", "unit_id", name="uq_water_bill_details_water_bill_id_unit_id"
        ),
        CheckConstraint(
            _non_negative_decimal("base_amount"), name="base_amount_non_negative"
        ),
        CheckConstraint(
            _non_negative_decimal("welfare_discount"), name="welfare_discount_non_negative"
        ),
        CheckConstraint(
            _non_negative_decimal("final_amount"), name="final_amount_non_negative"
        ),
        CheckConstraint("charged_amount >= 0", name="charged_amount_non_negative"),
        Index("ix_water_bill_details_unit_id", "unit_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    water_bill_id = db.Column(
        db.Integer, db.ForeignKey("water_bills.id", ondelete="CASCADE"), nullable=False
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    base_amount = db.Column(ExactDecimal(), nullable=False)
    welfare_discount = db.Column(
        ExactDecimal(), nullable=False, default=0, server_default="0.00"
    )
    final_amount = db.Column(ExactDecimal(), nullable=False)
    charged_amount = db.Column(Money, nullable=False)
    unit_snapshot = db.Column(db.JSON)
    is_excluded = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    water_bill = db.relationship("WaterBill", back_populates="details")
    unit = db.relationship("Unit", back_populates="water_bill_details")


# ---------------------------------------------------------------------------
# 공동 공과금
# ---------------------------------------------------------------------------
class CommonBill(BillingMonthMixin, TimestampMixin, db.Model):
    """공동 공과금.

    같은 달에 인터넷/관리비/기타 등 여러 건이 정상이므로
    ``billing_month`` 에 UNIQUE 를 걸지 않는다.
    """

    __tablename__ = "common_bills"
    __table_args__ = (
        CheckConstraint(_day_is_first("billing_month"), name="billing_month_first_day"),
        CheckConstraint(
            _in_check("distribution_method", DISTRIBUTION_METHODS),
            name="distribution_method_valid",
        ),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        Index("ix_common_bills_billing_month", "billing_month"),
    )

    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255))
    total_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    distribution_method = db.Column(
        db.String(20),
        nullable=False,
        default="BY_RESIDENTS",
        server_default="BY_RESIDENTS",
    )

    details = db.relationship(
        "CommonBillDetail",
        back_populates="common_bill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invoice_items = db.relationship(
        "InvoiceCombinationItem", back_populates="common_bill_ref", passive_deletes="all"
    )


class CommonBillDetail(db.Model):
    __tablename__ = "common_bill_details"
    __table_args__ = (
        UniqueConstraint(
            "common_bill_id",
            "unit_id",
            name="uq_common_bill_details_common_bill_id_unit_id",
        ),
        CheckConstraint(_non_negative_decimal("amount"), name="amount_non_negative"),
        CheckConstraint("charged_amount >= 0", name="charged_amount_non_negative"),
        Index("ix_common_bill_details_unit_id", "unit_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    common_bill_id = db.Column(
        db.Integer, db.ForeignKey("common_bills.id", ondelete="CASCADE"), nullable=False
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    amount = db.Column(ExactDecimal(), nullable=False)
    charged_amount = db.Column(Money, nullable=False)
    unit_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    common_bill = db.relationship("CommonBill", back_populates="details")
    unit = db.relationship("Unit", back_populates="common_bill_details")


# ---------------------------------------------------------------------------
# 정산서
# ---------------------------------------------------------------------------
class InvoiceCombination(TimestampMixin, db.Model):
    __tablename__ = "invoice_combinations"
    __table_args__ = (Index("ix_invoice_combinations_created_at", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    invoice_name = db.Column(db.String(255), nullable=False)
    memo = db.Column(db.Text)

    items = db.relationship(
        "InvoiceCombinationItem",
        back_populates="combination",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invoices = db.relationship(
        "FinalInvoice",
        back_populates="combination",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # 입금 기록은 회계 데이터다. 정산서를 지운다고 함께 사라져서는 안 된다.
    payments = db.relationship(
        "Payment", back_populates="combination", passive_deletes="all"
    )


#: 다형 참조 정합성. item_type 과 실제로 채워진 FK 가 정확히 일치해야 한다.
#: 이 CHECK 가 없으면 "3개 모두 NULL", "2개 이상 non-NULL", "type 과 FK 불일치" 가
#: 전부 DB 에 저장될 수 있다.
_ITEM_POLYMORPHIC_CHECK = (
    "("
    " (item_type = 'ELECTRIC' AND electric_bill_id IS NOT NULL"
    "   AND water_bill_id IS NULL AND common_bill_id IS NULL)"
    " OR (item_type = 'WATER' AND water_bill_id IS NOT NULL"
    "   AND electric_bill_id IS NULL AND common_bill_id IS NULL)"
    " OR (item_type = 'COMMON' AND common_bill_id IS NOT NULL"
    "   AND electric_bill_id IS NULL AND water_bill_id IS NULL)"
    ")"
)


class InvoiceCombinationItem(BillingMonthMixin, db.Model):
    __tablename__ = "invoice_combination_items"
    __table_args__ = (
        CheckConstraint(
            _in_check("item_type", INVOICE_ITEM_TYPES), name="item_type_valid"
        ),
        CheckConstraint(_ITEM_POLYMORPHIC_CHECK, name="exactly_one_bill_reference"),
        CheckConstraint(_day_is_first("billing_month"), name="billing_month_first_day"),
        Index("ix_invoice_combination_items_combination_id", "combination_id"),
        Index("ix_invoice_combination_items_electric_bill_id", "electric_bill_id"),
        Index("ix_invoice_combination_items_water_bill_id", "water_bill_id"),
        Index("ix_invoice_combination_items_common_bill_id", "common_bill_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    combination_id = db.Column(
        db.Integer,
        db.ForeignKey("invoice_combinations.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_type = db.Column(db.String(20), nullable=False)
    billing_month = db.Column(db.Date, nullable=False)
    item_description = db.Column(db.String(200))

    electric_bill_id = db.Column(
        db.Integer, db.ForeignKey("electric_bills.id", ondelete="RESTRICT")
    )
    water_bill_id = db.Column(
        db.Integer, db.ForeignKey("water_bills.id", ondelete="RESTRICT")
    )
    common_bill_id = db.Column(
        db.Integer, db.ForeignKey("common_bills.id", ondelete="RESTRICT")
    )

    combination = db.relationship("InvoiceCombination", back_populates="items")
    electric_bill_ref = db.relationship(
        "ElectricBill", back_populates="invoice_items", lazy="joined"
    )
    water_bill_ref = db.relationship(
        "WaterBill", back_populates="invoice_items", lazy="joined"
    )
    common_bill_ref = db.relationship(
        "CommonBill", back_populates="invoice_items", lazy="joined"
    )

    @property
    def bill_id(self):
        """``item_type`` 에 해당하는 bill id 를 반환한다."""
        if self.item_type == "ELECTRIC":
            return self.electric_bill_id
        if self.item_type == "WATER":
            return self.water_bill_id
        if self.item_type == "COMMON":
            return self.common_bill_id
        return None


class FinalInvoice(db.Model):
    """세대별 확정 청구서.

    ``total_amount`` 에는 부호 CHECK 를 걸지 않는다.
    환급 항목이 청구액을 초과하면 음수가 될 수 있다.
    """

    __tablename__ = "final_invoices"
    __table_args__ = (
        UniqueConstraint(
            "combination_id", "unit_id", name="uq_final_invoices_combination_id_unit_id"
        ),
        CheckConstraint("electric_amount >= 0", name="electric_amount_non_negative"),
        CheckConstraint("water_amount >= 0", name="water_amount_non_negative"),
        CheckConstraint("common_amount >= 0", name="common_amount_non_negative"),
        Index("ix_final_invoices_unit_id", "unit_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    combination_id = db.Column(
        db.Integer,
        db.ForeignKey("invoice_combinations.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    electric_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    water_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    common_amount = db.Column(Money, nullable=False, default=0, server_default=text("0"))
    # 인쇄 표시 전용 스냅샷. 비즈니스 로직이 집계하지 않으므로 JSON 을 유지한다.
    common_details = db.Column(db.JSON)
    total_amount = db.Column(Money, nullable=False)
    unit_memo = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    combination = db.relationship("InvoiceCombination", back_populates="invoices")
    unit = db.relationship("Unit", back_populates="final_invoices")
    charges = db.relationship(
        "FinalInvoiceCharge",
        back_populates="final_invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FinalInvoiceCharge.sort_order",
    )

    @property
    def billable_charges_total(self):
        """잔액 계산에 포함되는 기타 항목 합계 (이월 제외).

        기존 구현은 ``description`` 문자열에 '미납/초과납부/환급/이월' 이 들어있는지로
        판정했다. 이제 ``is_carryover`` 컬럼만 본다.
        """
        return sum(c.amount for c in self.charges if not c.is_carryover)

    @property
    def carryover_charges_total(self):
        """참고용 이월 항목 합계."""
        return sum(c.amount for c in self.charges if c.is_carryover)

    @property
    def additional_charges(self):
        """레거시 ``additional_charges`` JSON 자리를 대신하는 읽기 전용 표현.

        ``description`` / ``amount`` 속성 접근이 동일하게 동작하므로
        정산서 조회·인쇄 템플릿을 그대로 쓸 수 있고, 추가로 ``is_carryover`` 를 노출한다.
        """
        return self.charges


class FinalInvoiceCharge(db.Model):
    """세대별 기타 부과/환급 항목.

    기존 ``final_invoices.additional_charges`` JSON 을 대체한다.

    핵심 변경: ``is_carryover`` 를 **명시적으로 저장**한다.
    프론트엔드는 이미 이 값을 보내고 있었으나 백엔드가 버린 뒤
    description 문자열로 역추론하고 있었다(4곳 중복).

    유지되는 불변식
    --------------
    - 이월 금액은 ``FinalInvoice.total_amount`` 에 **포함**된다 (청구서에 찍힌다).
    - 이월 금액은 잔액 계산의 청구액에는 **포함되지 않는다** (이중계상 방지).

    판정 수단만 문자열 → 컬럼으로 바뀌고 결과 숫자는 동일하다.
    ``amount`` 는 환급일 때 음수이므로 부호 CHECK 를 걸지 않는다.
    """

    __tablename__ = "final_invoice_charges"
    __table_args__ = (
        Index("ix_final_invoice_charges_final_invoice_id", "final_invoice_id"),
        Index("ix_final_invoice_charges_is_carryover", "is_carryover"),
    )

    id = db.Column(db.Integer, primary_key=True)
    final_invoice_id = db.Column(
        db.Integer, db.ForeignKey("final_invoices.id", ondelete="CASCADE"), nullable=False
    )
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(Money, nullable=False)
    is_carryover = db.Column(
        db.Boolean, nullable=False, default=False, server_default=text("0")
    )
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default=text("0"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    final_invoice = db.relationship("FinalInvoice", back_populates="charges")


# ---------------------------------------------------------------------------
# 납부
# ---------------------------------------------------------------------------
class Payment(TimestampMixin, db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("payment_amount >= 0", name="payment_amount_non_negative"),
        Index("ix_payments_unit_id", "unit_id"),
        Index("ix_payments_combination_id", "combination_id"),
        Index("ix_payments_payment_date", "payment_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # RESTRICT: 입금 기록이 있는 정산서는 삭제할 수 없다.
    combination_id = db.Column(
        db.Integer,
        db.ForeignKey("invoice_combinations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    payment_date = db.Column(db.Date, nullable=False)
    payment_amount = db.Column(Money, nullable=False)
    # 사용자가 '기타'로 임의 문자열을 넣을 수 있으므로 CHECK 를 걸지 않는다.
    payment_method = db.Column(
        db.String(50), nullable=False, default="계좌이체", server_default="계좌이체"
    )
    memo = db.Column(db.Text)

    unit = db.relationship("Unit", back_populates="payments")
    combination = db.relationship("InvoiceCombination", back_populates="payments")


#: 마이그레이션/검증 도구가 참조하는 테이블 목록 (FK 위상 정렬 순서).
TABLE_ORDER = (
    "settings",
    "floors",
    "units",
    "electric_bills",
    "electric_bill_months",
    "electric_readings",
    "electric_bill_details",
    "water_bills",
    "water_bill_details",
    "common_bills",
    "common_bill_details",
    "invoice_combinations",
    "invoice_combination_items",
    "final_invoices",
    "final_invoice_charges",
    "payments",
)
