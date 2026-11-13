import csv
import json
from datetime import datetime, timezone

from scripts.collect_growth_metrics import (
    build_snapshot,
    classify_asset,
    load_previous_snapshot,
    write_outputs,
)


def _release(windows=4, mac_arm=2, checksum=1):
    return [
        {
            "tag_name": "v1.0.0",
            "name": "BillCalculator v1.0.0",
            "published_at": "2026-11-13T06:34:31Z",
            "assets": [
                {
                    "name": "BillCalculator-Setup-1.0.0-Windows-x64.exe",
                    "download_count": windows,
                    "size": 100,
                    "browser_download_url": "https://example.com/windows",
                },
                {
                    "name": "BillCalculator-1.0.0-macOS-arm64.dmg",
                    "download_count": mac_arm,
                    "size": 200,
                    "browser_download_url": "https://example.com/mac",
                },
                {
                    "name": "SHA256SUMS.txt",
                    "download_count": checksum,
                    "size": 10,
                    "browser_download_url": "https://example.com/checksum",
                },
            ],
        }
    ]


def test_classify_asset_excludes_support_files_from_installer_total():
    assert classify_asset("BillCalculator.exe") == "installer"
    assert classify_asset("BillCalculator.DMG") == "installer"
    assert classify_asset("SHA256SUMS.txt") == "support"


def test_build_snapshot_calculates_installer_delta_from_previous_day():
    previous = build_snapshot(
        "InFinity-dev/BillCalculator",
        _release(windows=1, mac_arm=1, checksum=1),
        {"available": False, "reason": "test"},
        collected_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )
    current = build_snapshot(
        "InFinity-dev/BillCalculator",
        _release(windows=4, mac_arm=2, checksum=3),
        {"available": False, "reason": "test"},
        previous=previous,
        collected_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
    )

    assert previous["totals"]["installer_downloads"] == 2
    assert current["totals"]["installer_downloads"] == 6
    assert current["totals"]["installer_daily_change"] == 4
    assert current["totals"]["support_downloads"] == 3
    assert current["totals"]["support_daily_change"] == 2


def test_write_outputs_creates_daily_json_and_csv_history(tmp_path):
    first = build_snapshot(
        "InFinity-dev/BillCalculator",
        _release(windows=1, mac_arm=1),
        {"available": False, "reason": "test"},
        collected_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )
    write_outputs(tmp_path, first)

    previous = load_previous_snapshot(tmp_path, "2026-11-14")
    second = build_snapshot(
        "InFinity-dev/BillCalculator",
        _release(windows=2, mac_arm=3),
        {
            "available": True,
            "views": {"count": 11, "uniques": 2},
            "clones": {"count": 30, "uniques": 15},
            "referrers": [],
            "popular_paths": [],
        },
        previous=previous,
        collected_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
    )
    write_outputs(tmp_path, second)

    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["date_kst"] == "2026-11-14"
    assert (tmp_path / "snapshots" / "2026-11-13.json").exists()
    assert (tmp_path / "snapshots" / "2026-11-14.json").exists()

    with (tmp_path / "daily-summary.csv").open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[-1]["installer_downloads"] == "5"
    assert rows[-1]["installer_daily_change"] == "3"
    assert rows[-1]["unique_visitors_14d"] == "2"

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "설치 파일 누적 다운로드: **5회**" in readme
    assert "전일 대비: **+3회**" in readme
