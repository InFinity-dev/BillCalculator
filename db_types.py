"""SQLite 용 금액 / 계량값 타입.

SQLAlchemy 의 ``Numeric`` 은 SQLite 에서 REAL(부동소수점)로 저장되며 다음 경고를 낸다::

    SAWarning: Dialect sqlite+pysqlite does *not* support Decimal objects natively,
    and SQLAlchemy must convert from floating point - rounding errors and other
    issues may occur.

금액을 다루는 애플리케이션이므로 부동소수점 경유를 허용하지 않는다.
대신 의미에 따라 두 가지 타입을 사용한다.

``Money``
    통화 금액. INTEGER(원) 로 저장한다.
    사용자 입력 총액, 할인 총액, ``charged_amount``, 정산서 금액, 납부액이 해당한다.
    이 값들은 도메인상 항상 정수 원이다 (``charged_amount`` 는 10원 단위 올림 결과).

``ExactDecimal``
    분수 의미를 갖는 값. TEXT 로 정규화 저장하고 ``Decimal`` 로 복원한다.
    배분 중간값(``base_amount``, ``final_amount``, 세대별 할인/TV)과
    계량값(검침값, 사용량)이 해당한다.

    TEXT 저장이라 SQL 정렬/집계가 사전식이 되지만, 감사 결과 이 컬럼들은
    SQL 에서 정렬·집계되지 않고 전부 Python/Jinja 측에서 합산된다.
    (docs/refactoring/database/00-current-db-audit.md 10절)

    부호 검사가 필요한 경우 CHECK 제약에서 ``CAST(col AS REAL)`` 을 써야 한다.
    TEXT 를 정수와 직접 비교하면 SQLite 의 타입 우선순위 때문에 항상 참이 된다.

두 타입 모두 MySQL ``DECIMAL(x, 2)`` 과 동일한 ROUND_HALF_UP(0에서 먼 쪽으로 반올림)을
사용하므로, 기존 데이터의 값이 그대로 보존된다.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from sqlalchemy import Integer, String
from sqlalchemy.types import TypeDecorator

#: ExactDecimal 의 소수 자릿수. 기존 MySQL DECIMAL(x, 2) 와 동일하게 맞춘다.
DECIMAL_SCALE = 2
_QUANT = Decimal(1).scaleb(-DECIMAL_SCALE)  # Decimal('0.01')


def _to_decimal(value):
    """임의의 스칼라를 Decimal 로 변환한다. 변환 불가 시 ValueError."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("bool 은 금액으로 사용할 수 없습니다: %r" % (value,))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # float 는 str() 을 거쳐야 이진 부동소수점 오차가 확산되지 않는다.
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text == "":
            return Decimal(0)
        try:
            return Decimal(text)
        except InvalidOperation:
            raise ValueError("금액으로 해석할 수 없습니다: %r" % (value,))
    raise ValueError("지원하지 않는 금액 타입입니다: %r" % (type(value),))


class Money(TypeDecorator):
    """통화 금액(원). INTEGER 로 저장하고 int 로 복원한다.

    Python 측에서 int 는 Decimal 과 자유롭게 연산되므로
    (``Decimal('0') + 12500 -> Decimal('12500')``) 기존 계산 코드가 그대로 동작한다.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return int(_to_decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return int(value)


class ExactDecimal(TypeDecorator):
    """분수 의미를 갖는 값. TEXT 로 정확히 저장하고 Decimal 로 복원한다."""

    impl = String
    cache_ok = True

    def __init__(self, scale=DECIMAL_SCALE, **kwargs):
        self.scale = scale
        self._quant = Decimal(1).scaleb(-scale)
        super().__init__(**kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(_to_decimal(value).quantize(self._quant, rounding=ROUND_HALF_UP))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


def money_to_int(value, default=0):
    """Money 컬럼에 넣기 전 파이썬 값을 정수 원으로 변환한다.

    레거시 마이그레이션과 라우트 계층에서 공용으로 쓴다.
    """
    if value is None:
        return default
    try:
        return int(_to_decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except ValueError:
        return default


def has_fraction(value):
    """통화 값으로 부적절한 소수부를 가지고 있는지 판정한다.

    레거시 마이그레이션이 ``requires-review`` 를 탐지할 때 사용한다.
    """
    if value is None:
        return False
    try:
        dec = _to_decimal(value)
    except ValueError:
        return True
    return dec != dec.to_integral_value()
