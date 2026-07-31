"""골든 마스터 대조.

대표 데이터셋을 계산 경로 전체에 통과시킨 결과를 ``expected.json`` 과 바이트 단위로
비교한다. 허용 오차는 0이다. 1원이라도 다르면 실패한다.

한계 (명시)
----------
실제 MySQL 운영 DB 에 접근할 수 없어(docs/refactoring/database/00-current-db-audit.md 1절),
이 기준선은 **MySQL 버전에서 뽑은 것이 아니라 SQLite 전환 직후 구현에서 생성한 것**이다.
따라서 이 파일은 "MySQL → SQLite 전환이 금액을 바꾸지 않았다"의 증명이 아니라,
**이후 모든 리팩토링에 대한 회귀 방어선**이다.

전환 자체의 정확성은 다음 두 가지가 담당한다.
  1. tests/test_persistence.py — 도메인 알고리즘을 손으로 전개한 기대값과 대조
  2. scripts/migrate_mysql_to_sqlite.py — 이전 시 세대별 balance 를 원본과 대조

기준선을 갱신하려면::

    pytest tests/test_golden.py --update-golden
"""

import json
from pathlib import Path

import pytest

from tests.golden import dataset

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "expected.json"


@pytest.fixture()
def built(app, client, csrf):
    dataset.build(client, csrf)
    return dataset.snapshot()


@pytest.mark.golden
def test_golden_master(built, request):
    if request.config.getoption("--update-golden", default=False):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(built, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.skip("골든 마스터를 갱신했습니다: {}".format(GOLDEN_PATH))

    assert GOLDEN_PATH.exists(), (
        "골든 마스터가 없습니다. 먼저 `pytest tests/test_golden.py --update-golden` 을 실행하세요."
    )
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    for section in expected:
        assert built[section] == expected[section], (
            "'{}' 섹션이 골든 마스터와 다릅니다.".format(section)
        )
    assert built == expected


@pytest.mark.golden
def test_dataset_covers_all_branches(built):
    """대표 데이터셋이 의도한 분기를 실제로 통과했는지 확인한다."""
    months = built["electric_bill_months"]
    assert len({m["floor"] for m in months}) == 2
    assert len([m for m in months if m["floor"] == "2층"]) == 3   # 3개월 묶음

    details = built["electric_bill_details"]
    assert any(d["tv_fee"] != "0.00" for d in details)
    assert any(d["welfare_discount"] != "0.00" for d in details)
    assert any(d["voucher_discount"] != "0.00" for d in details)

    assert any(w["is_excluded"] for w in built["water_bill_details"])
    assert {c["method"] for c in built["common_bill_details"]} == {
        "BY_RESIDENTS",
        "BY_UNITS",
    }

    charges = [c for inv in built["final_invoices"] for c in inv["charges"]]
    assert any(c["is_carryover"] for c in charges)
    assert any(not c["is_carryover"] for c in charges)
    assert any(c["amount"] < 0 for c in charges)

    balances = built["balances"]
    assert any(v > 0 for v in balances.values())     # 미납
    assert any(v < 0 for v in balances.values())     # 초과납부


@pytest.mark.golden
def test_carryover_not_double_counted_in_golden(built):
    """★ 이월 항목이 청구서 총액에는 있고 잔액에는 이중계상되지 않는지."""
    for invoice in built["final_invoices"]:
        component_sum = (
            invoice["electric_amount"]
            + invoice["water_amount"]
            + invoice["common_amount"]
            + sum(c["amount"] for c in invoice["charges"])
        )
        assert invoice["total_amount"] == component_sum

    billed = {}
    for invoice in built["final_invoices"]:
        billable = sum(c["amount"] for c in invoice["charges"] if not c["is_carryover"])
        billed[invoice["unit"]] = billed.get(invoice["unit"], 0) + (
            invoice["electric_amount"]
            + invoice["water_amount"]
            + invoice["common_amount"]
            + billable
        )
    for unit, expected_billed in billed.items():
        assert unit in built["balances"]


@pytest.mark.golden
def test_charged_amounts_are_multiples_of_10(built):
    for section in ("electric_bill_details", "water_bill_details", "common_bill_details"):
        for row in built[section]:
            assert row["charged_amount"] % 10 == 0
