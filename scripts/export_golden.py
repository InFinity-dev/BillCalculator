#!/usr/bin/env python
"""현재 DB 에서 골든 마스터를 추출한다.

운영 데이터로 리팩토링 전후를 대조할 때 사용한다.
읽기 전용이며 DB 를 변경하지 않는다.

사용법::

    # 현재 SQLite DB 에서
    python scripts/export_golden.py --output data/golden_before.json

    # 특정 DB 파일에서
    BILLCALC_DB_PATH=/path/to/other.db python scripts/export_golden.py \
        --output data/golden_other.json

출력 형식은 tests/golden/dataset.py 의 snapshot() 과 동일하다.
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description="골든 마스터 추출 (읽기 전용)")
    parser.add_argument("--output", default="data/golden.json")
    args = parser.parse_args(argv)

    import app as app_module
    from tests.golden import dataset

    with app_module.app.app_context():
        snapshot = dataset.snapshot()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("골든 마스터 저장: {}".format(out_path))
    print("  전기 detail  {}건".format(len(snapshot["electric_bill_details"])))
    print("  수도 detail  {}건".format(len(snapshot["water_bill_details"])))
    print("  공동 detail  {}건".format(len(snapshot["common_bill_details"])))
    print("  정산서       {}건".format(len(snapshot["final_invoices"])))
    print("  집계         {}".format(snapshot["aggregates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
