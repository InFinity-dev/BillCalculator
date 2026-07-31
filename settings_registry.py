"""설정 키의 단일 정의 지점.

기존에는 동일한 키 목록이 다섯 곳(읽기/쓰기/Export/Import/시딩)에 중복 정의되어
있었고, 그 결과 시딩 목록에서 3개 키가 누락되어 있었다.
(codebase-analysis 06-technical-debt.md M-4)

이 모듈이 키·타입·기본값·검증을 한 곳에서 정의하고, 나머지 코드는 전부 여기를 참조한다.
설정을 추가할 때 고칠 곳은 아래 ``SETTING_DEFS`` 한 곳뿐이며 마이그레이션은 필요 없다.
"""

from decimal import Decimal

from db_types import money_to_int


class SettingDef:
    """설정 하나의 정의."""

    __slots__ = ("key", "kind", "default", "label")

    def __init__(self, key, kind, default, label):
        self.key = key
        self.kind = kind          # 'money' | 'text'
        self.default = default    # 저장 형식(문자열)
        self.label = label

    def parse(self, raw):
        """저장된 문자열을 도메인 타입으로 변환한다."""
        if self.kind == "money":
            return money_to_int(raw if raw not in (None, "") else self.default, 0)
        return raw if raw is not None else self.default

    def as_decimal(self, raw):
        """계산 코드가 기대하는 Decimal 형태로 변환한다."""
        return Decimal(self.parse(raw))

    def normalize(self, value):
        """사용자 입력을 저장 형식(문자열)으로 정규화한다."""
        if value is None:
            return self.default
        if self.kind == "money":
            return str(money_to_int(value, money_to_int(self.default, 0)))
        return str(value)


#: 알려진 전체 설정. 순서는 설정 화면 표시 순서와 무관하다.
SETTING_DEFS = (
    SettingDef("tv_fee", "money", "2500", "TV 수신료 (세대당)"),
    SettingDef("electric_welfare_amount", "money", "0", "전기 복지 할인 금액"),
    SettingDef("electric_voucher_amount", "money", "0", "전기 바우처 할인 금액"),
    SettingDef("water_welfare_amount", "money", "0", "수도 복지 할인 금액"),
    SettingDef("invoice_default_memo", "text", "", "정산서 고정 메모"),
    SettingDef(
        "invoice_footer",
        "text",
        "* Footer 문구를 설정에서 커스텀 할 수 있습니다.",
        "인쇄용 하단 안내문",
    ),
    SettingDef("electric_bill_url", "text", "", "전기요금 조회 URL"),
    SettingDef("water_bill_url", "text", "", "수도요금 조회 URL"),
    SettingDef("water_customer_number", "text", "", "주택 수도 고객번호"),
)

SETTING_MAP = {d.key: d for d in SETTING_DEFS}
SETTING_KEYS = tuple(d.key for d in SETTING_DEFS)


def get_def(key):
    """설정 정의를 조회한다. 미등록 키는 명시적으로 실패시킨다."""
    try:
        return SETTING_MAP[key]
    except KeyError:
        raise KeyError(
            "등록되지 않은 설정 키입니다: %r. settings_registry.SETTING_DEFS 에 추가하세요." % (key,)
        )


def default_for(key):
    return get_def(key).default


def defaults():
    """{key: 기본값 문자열} 전체."""
    return {d.key: d.default for d in SETTING_DEFS}
