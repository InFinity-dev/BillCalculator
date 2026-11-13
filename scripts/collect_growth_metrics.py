#!/usr/bin/env python3
"""GitHub Release 다운로드와 저장소 트래픽을 날짜별로 보관한다."""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
from datetime import datetime, timezone
from html import escape
from math import ceil
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_ROOT = "https://api.github.com"
SEOUL = ZoneInfo("Asia/Seoul")
INSTALLER_SUFFIXES = (".exe", ".dmg", ".msi", ".pkg", ".appimage", ".deb", ".rpm")
CHART_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"


def _ssl_context() -> ssl.SSLContext:
    configured = os.environ.get("SSL_CERT_FILE")
    candidates = [
        Path(configured) if configured else None,
        Path("/etc/ssl/cert.pem"),
        Path("/etc/ssl/certs/ca-certificates.crt"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return ssl.create_default_context(cafile=str(candidate))
    return ssl.create_default_context()


def _request_json(path: str, token: str | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BillCalculator-growth-metrics",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(f"{API_ROOT}{path}", headers=headers)
    with urlopen(request, timeout=30, context=_ssl_context()) as response:
        return json.load(response)


def fetch_releases(repository: str, token: str | None) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    encoded_repository = quote(repository, safe="/")
    page = 1

    while True:
        batch = _request_json(
            f"/repos/{encoded_repository}/releases?per_page=100&page={page}", token
        )
        if not isinstance(batch, list):
            raise RuntimeError("GitHub Release 응답이 목록 형식이 아닙니다.")
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def fetch_traffic(repository: str, token: str | None) -> dict[str, Any]:
    if not token:
        return {
            "available": False,
            "reason": "저장소 트래픽 조회 토큰이 설정되지 않았습니다.",
        }

    encoded_repository = quote(repository, safe="/")
    endpoints = {
        "views": "views",
        "clones": "clones",
        "referrers": "popular/referrers",
        "popular_paths": "popular/paths",
    }

    try:
        payload = {
            key: _request_json(
                f"/repos/{encoded_repository}/traffic/{endpoint}", token
            )
            for key, endpoint in endpoints.items()
        }
    except HTTPError as exc:
        return {
            "available": False,
            "reason": f"저장소 트래픽 API를 사용할 수 없습니다. HTTP {exc.code}",
        }
    except URLError as exc:
        return {
            "available": False,
            "reason": f"저장소 트래픽 API 연결에 실패했습니다: {exc.reason}",
        }

    return {"available": True, **payload}


def classify_asset(name: str) -> str:
    return "installer" if name.lower().endswith(INSTALLER_SUFFIXES) else "support"


def _flatten_assets(releases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for release in releases:
        tag = str(release.get("tag_name") or "")
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            flattened.append(
                {
                    "tag": tag,
                    "release_name": str(release.get("name") or tag),
                    "published_at": release.get("published_at"),
                    "name": name,
                    "kind": classify_asset(name),
                    "download_count": int(asset.get("download_count") or 0),
                    "size": int(asset.get("size") or 0),
                    "browser_download_url": asset.get("browser_download_url"),
                }
            )
    return flattened


def _asset_counts(snapshot: dict[str, Any] | None) -> dict[str, int]:
    if not snapshot:
        return {}
    return {
        f"{asset['tag']}::{asset['name']}": int(asset["download_count"])
        for asset in snapshot.get("assets", [])
    }


def build_snapshot(
    repository: str,
    releases: list[dict[str, Any]],
    traffic: dict[str, Any],
    previous: dict[str, Any] | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    collected_at = collected_at or datetime.now(timezone.utc)
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=timezone.utc)
    collected_at = collected_at.astimezone(timezone.utc)

    assets = _flatten_assets(releases)
    previous_counts = _asset_counts(previous)
    has_previous = previous is not None

    for asset in assets:
        key = f"{asset['tag']}::{asset['name']}"
        previous_count = previous_counts.get(key, 0)
        asset["previous_download_count"] = previous_count if has_previous else None
        asset["daily_change"] = (
            asset["download_count"] - previous_count if has_previous else None
        )

    installer_assets = [asset for asset in assets if asset["kind"] == "installer"]
    support_assets = [asset for asset in assets if asset["kind"] == "support"]
    installer_total = sum(asset["download_count"] for asset in installer_assets)
    support_total = sum(asset["download_count"] for asset in support_assets)

    previous_installer_total = None
    previous_support_total = None
    if previous:
        previous_installer_total = int(
            previous.get("totals", {}).get("installer_downloads", 0)
        )
        previous_support_total = int(
            previous.get("totals", {}).get("support_downloads", 0)
        )

    return {
        "schema_version": 1,
        "repository": repository,
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "date_kst": collected_at.astimezone(SEOUL).date().isoformat(),
        "totals": {
            "installer_downloads": installer_total,
            "installer_daily_change": (
                installer_total - previous_installer_total
                if previous_installer_total is not None
                else None
            ),
            "support_downloads": support_total,
            "support_daily_change": (
                support_total - previous_support_total
                if previous_support_total is not None
                else None
            ),
        },
        "assets": assets,
        "traffic": traffic,
    }


def load_previous_snapshot(output_dir: Path, date_kst: str) -> dict[str, Any] | None:
    snapshots_dir = output_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    candidates = sorted(
        path for path in snapshots_dir.glob("*.json") if path.stem < date_kst
    )
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def _format_delta(value: int | None) -> str:
    if value is None:
        return "기준선"
    return f"{value:+,}"


def _format_delta_with_unit(value: int | None) -> str:
    if value is None:
        return "기준선"
    return f"{value:+,}회"


def _svg_document(width: int, height: int, title: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
            f"  <title id=\"title\">{escape(title)}</title>",
            "  <desc id=\"desc\">GitHub Release 설치 파일 다운로드 집계 그래프</desc>",
            "  <rect width=\"100%\" height=\"100%\" rx=\"16\" fill=\"#ffffff\"/>",
            "  <rect x=\"0.5\" y=\"0.5\" width=\"959\" "
            f'height="{height - 1}" rx="15.5" fill="none" stroke="#dbe3ef"/>',
            f'  <g font-family="{CHART_FONT}" fill="#172033">',
            *body,
            "  </g>",
            "</svg>",
            "",
        ]
    )


def render_download_trend_svg(snapshots: list[dict[str, Any]]) -> str:
    recent = snapshots[-30:]
    width, height = 960, 620
    left, right = 76, 924
    plot_width = right - left
    daily_top, daily_bottom = 112, 280
    cumulative_top, cumulative_bottom = 398, 548
    body = [
        '    <text x="40" y="48" font-size="24" font-weight="700">최근 30일 다운로드 추이</text>',
        '    <text x="40" y="76" font-size="14" fill="#64748b">매일 오전 9시 17분(한국시간) 사이의 누적값 차이</text>',
        '    <text x="40" y="104" font-size="14" font-weight="600" fill="#334155">일간 신규 다운로드</text>',
    ]

    if not recent:
        body.append(
            '    <text x="480" y="310" text-anchor="middle" font-size="16" '
            'fill="#64748b">아직 수집된 데이터가 없습니다.</text>'
        )
        return _svg_document(width, height, "BillCalculator 다운로드 추이", body)

    daily_values = [
        max(int(item["totals"].get("installer_daily_change") or 0), 0)
        for item in recent
    ]
    daily_max = max(max(daily_values), 1)
    cumulative_values = [
        int(item["totals"].get("installer_downloads", 0)) for item in recent
    ]
    cumulative_max = max(max(cumulative_values), 1)
    slot = plot_width / max(len(recent), 1)
    bar_width = min(34, max(8, slot * 0.58))
    label_step = max(1, ceil(len(recent) / 8))

    daily_ticks = (
        list(range(daily_max, -1, -1))
        if daily_max <= 4
        else [round(daily_max * (4 - index) / 4) for index in range(5)]
    )
    daily_tick_divisor = max(len(daily_ticks) - 1, 1)
    for index, value in enumerate(daily_ticks):
        y = daily_top + (daily_bottom - daily_top) * index / daily_tick_divisor
        body.extend(
            [
                f'    <line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
                'stroke="#e8edf5" stroke-width="1"/>',
                f'    <text x="{left - 12}" y="{y + 5:.1f}" text-anchor="end" '
                f'font-size="12" fill="#64748b">{value}</text>',
            ]
        )

    for index, (item, daily_value) in enumerate(zip(recent, daily_values)):
        center_x = left + slot * (index + 0.5)
        bar_height = (daily_value / daily_max) * (daily_bottom - daily_top)
        bar_y = daily_bottom - bar_height
        original_delta = item["totals"].get("installer_daily_change")
        color = "#dc2626" if isinstance(original_delta, int) and original_delta < 0 else "#315fba"
        if bar_height > 0:
            body.append(
                f'    <rect x="{center_x - bar_width / 2:.1f}" y="{bar_y:.1f}" '
                f'width="{bar_width:.1f}" height="{bar_height:.1f}" rx="4" fill="{color}"/>'
            )
        else:
            body.append(
                f'    <circle cx="{center_x:.1f}" cy="{daily_bottom}" r="3" fill="{color}"/>'
            )

        if len(recent) <= 10 or index % label_step == 0 or index == len(recent) - 1:
            label = escape(str(item["date_kst"])[5:])
            body.append(
                f'    <text x="{center_x:.1f}" y="{daily_bottom + 24}" text-anchor="middle" '
                f'font-size="12" fill="#64748b">{label}</text>'
            )

    body.extend(
        [
            '    <text x="40" y="374" font-size="14" font-weight="600" fill="#334155">누적 설치 파일 다운로드</text>',
            f'    <text x="{right}" y="374" text-anchor="end" font-size="22" '
            f'font-weight="700" fill="#315fba">{cumulative_values[-1]:,}회</text>',
        ]
    )

    for index in range(4):
        value = round(cumulative_max * (3 - index) / 3)
        y = cumulative_top + (cumulative_bottom - cumulative_top) * index / 3
        body.extend(
            [
                f'    <line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
                'stroke="#e8edf5" stroke-width="1"/>',
                f'    <text x="{left - 12}" y="{y + 5:.1f}" text-anchor="end" '
                f'font-size="12" fill="#64748b">{value}</text>',
            ]
        )

    points = []
    for index, value in enumerate(cumulative_values):
        x = left + slot * (index + 0.5)
        y = cumulative_bottom - (value / cumulative_max) * (
            cumulative_bottom - cumulative_top
        )
        points.append((x, y))

    if len(points) > 1:
        point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        body.append(
            f'    <polyline points="{point_text}" fill="none" stroke="#16a085" '
            'stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    for x, y in points:
        body.append(
            f'    <circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ffffff" '
            'stroke="#16a085" stroke-width="3"/>'
        )

    body.append(
        '    <text x="40" y="590" font-size="12" fill="#64748b">'
        "첫 수집일은 과거 일간 값을 알 수 없어 기준선으로 표시합니다.</text>"
    )
    return _svg_document(width, height, "BillCalculator 최근 30일 다운로드 추이", body)


def _platform_label(asset_name: str) -> str:
    lowered = asset_name.lower()
    if "windows" in lowered or lowered.endswith((".exe", ".msi")):
        return "Windows x64"
    if "macos-arm64" in lowered or "arm64" in lowered:
        return "Apple Silicon Mac"
    if "macos-x64" in lowered:
        return "Intel Mac"
    return "기타"


def render_platform_breakdown_svg(snapshot: dict[str, Any]) -> str:
    totals: dict[str, int] = {}
    for asset in snapshot.get("assets", []):
        if asset.get("kind") != "installer":
            continue
        label = _platform_label(str(asset.get("name", "")))
        totals[label] = totals.get(label, 0) + int(asset.get("download_count", 0))

    ordered_labels = ["Windows x64", "Apple Silicon Mac", "Intel Mac", "기타"]
    rows = [(label, totals[label]) for label in ordered_labels if label in totals]
    width = 960
    height = max(300, 170 + len(rows) * 58)
    max_value = max((value for _, value in rows), default=1)
    grand_total = sum(value for _, value in rows)
    colors = {
        "Windows x64": "#315fba",
        "Apple Silicon Mac": "#16a085",
        "Intel Mac": "#8b5cf6",
        "기타": "#64748b",
    }
    body = [
        '    <text x="40" y="48" font-size="24" font-weight="700">운영체제별 누적 다운로드</text>',
        f'    <text x="40" y="76" font-size="14" fill="#64748b">전체 버전 설치 파일 합계 · 총 {grand_total:,}회</text>',
    ]

    if not rows:
        body.append(
            '    <text x="480" y="170" text-anchor="middle" font-size="16" '
            'fill="#64748b">설치 파일 다운로드 데이터가 없습니다.</text>'
        )
        return _svg_document(width, height, "BillCalculator 운영체제별 다운로드", body)

    for index, (label, value) in enumerate(rows):
        y = 118 + index * 58
        bar_width = 560 * value / max_value if max_value else 0
        percent = value / grand_total * 100 if grand_total else 0
        body.extend(
            [
                f'    <text x="40" y="{y + 17}" font-size="14" font-weight="600">{escape(label)}</text>',
                f'    <rect x="210" y="{y}" width="560" height="24" rx="7" fill="#edf2f8"/>',
                f'    <rect x="210" y="{y}" width="{bar_width:.1f}" height="24" rx="7" '
                f'fill="{colors[label]}"/>',
                f'    <text x="900" y="{y + 17}" text-anchor="end" font-size="14" '
                f'font-weight="700">{value:,}회 · {percent:.1f}%</text>',
            ]
        )

    body.append(
        f'    <text x="40" y="{height - 28}" font-size="12" fill="#64748b">'
        "같은 사용자의 재다운로드도 각각 포함될 수 있습니다.</text>"
    )
    return _svg_document(width, height, "BillCalculator 운영체제별 누적 다운로드", body)


def render_readme(snapshot: dict[str, Any]) -> str:
    totals = snapshot["totals"]
    installer_assets = [
        asset for asset in snapshot["assets"] if asset["kind"] == "installer"
    ]
    traffic = snapshot.get("traffic", {})

    lines = [
        "# BillCalculator 성장 지표",
        "",
        "> GitHub Actions가 매일 자동 생성하는 공개 집계 데이터입니다. 개인을 식별하는 정보는 수집하지 않습니다.",
        "",
        f"- 마지막 수집: `{snapshot['collected_at']}`",
        f"- 한국 기준일: `{snapshot['date_kst']}`",
        f"- 설치 파일 누적 다운로드: **{totals['installer_downloads']:,}회**",
        f"- 전일 대비: **{_format_delta_with_unit(totals['installer_daily_change'])}**",
        "",
        "## 다운로드 추이",
        "",
        "![최근 30일 일간 및 누적 다운로드 추이](downloads-trend.svg)",
        "",
        "![운영체제별 누적 다운로드](platform-breakdown.svg)",
        "",
        "## 설치 파일별 다운로드",
        "",
        "| 버전 | 파일 | 누적 | 전일 대비 |",
        "|---|---|---:|---:|",
    ]

    for asset in installer_assets:
        lines.append(
            f"| `{asset['tag']}` | `{asset['name']}` | "
            f"{asset['download_count']:,} | {_format_delta(asset['daily_change'])} |"
        )

    lines.extend(["", "## 최근 14일 저장소 트래픽", ""])
    if traffic.get("available"):
        views = traffic.get("views", {})
        clones = traffic.get("clones", {})
        lines.extend(
            [
                f"- 조회: **{int(views.get('count', 0)):,}회** / "
                f"순 방문자 **{int(views.get('uniques', 0)):,}명**",
                f"- 클론: **{int(clones.get('count', 0)):,}회** / "
                f"순 클론 **{int(clones.get('uniques', 0)):,}명**",
            ]
        )
    else:
        lines.append(f"- 미수집: {traffic.get('reason', '권한이 없습니다.')}")

    lines.extend(
        [
            "",
            "## 데이터 파일",
            "",
            "- `latest.json`: 가장 최근 전체 원본",
            "- `snapshots/YYYY-MM-DD.json`: 날짜별 원본",
            "- `downloads.csv`: 파일별 누적 다운로드와 일간 변화",
            "- `daily-summary.csv`: 날짜별 제품 지표 요약",
            "- `downloads-trend.svg`: 최근 30일 일간·누적 다운로드 그래프",
            "- `platform-breakdown.svg`: 운영체제별 누적 다운로드 그래프",
            "",
            "GitHub의 `download_count`는 고유 사용자 수가 아니라 파일별 누적 요청 횟수입니다. "
            "체크섬 같은 보조 파일은 설치 파일 합계에서 제외합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_snapshots(output_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((output_dir / "snapshots").glob("*.json"))
    ]


def write_outputs(output_dir: Path, snapshot: dict[str, Any]) -> None:
    snapshots_dir = output_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    (snapshots_dir / f"{snapshot['date_kst']}.json").write_text(
        serialized, encoding="utf-8"
    )
    (output_dir / "latest.json").write_text(serialized, encoding="utf-8")

    snapshots = _load_snapshots(output_dir)
    (output_dir / "downloads-trend.svg").write_text(
        render_download_trend_svg(snapshots), encoding="utf-8"
    )
    (output_dir / "platform-breakdown.svg").write_text(
        render_platform_breakdown_svg(snapshot), encoding="utf-8"
    )
    (output_dir / "README.md").write_text(render_readme(snapshot), encoding="utf-8")
    with (output_dir / "downloads.csv").open("w", encoding="utf-8", newline="") as file:
        fieldnames = [
            "date_kst",
            "collected_at",
            "tag",
            "asset_name",
            "kind",
            "cumulative_downloads",
            "daily_change",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in snapshots:
            for asset in item.get("assets", []):
                writer.writerow(
                    {
                        "date_kst": item["date_kst"],
                        "collected_at": item["collected_at"],
                        "tag": asset["tag"],
                        "asset_name": asset["name"],
                        "kind": asset["kind"],
                        "cumulative_downloads": asset["download_count"],
                        "daily_change": (
                            "" if asset["daily_change"] is None else asset["daily_change"]
                        ),
                    }
                )

    with (output_dir / "daily-summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        fieldnames = [
            "date_kst",
            "collected_at",
            "installer_downloads",
            "installer_daily_change",
            "support_downloads",
            "views_14d",
            "unique_visitors_14d",
            "clones_14d",
            "unique_cloners_14d",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in snapshots:
            item_traffic = item.get("traffic", {})
            available = item_traffic.get("available")
            views = item_traffic.get("views", {}) if available else {}
            clones = item_traffic.get("clones", {}) if available else {}
            item_totals = item["totals"]
            writer.writerow(
                {
                    "date_kst": item["date_kst"],
                    "collected_at": item["collected_at"],
                    "installer_downloads": item_totals["installer_downloads"],
                    "installer_daily_change": (
                        ""
                        if item_totals["installer_daily_change"] is None
                        else item_totals["installer_daily_change"]
                    ),
                    "support_downloads": item_totals["support_downloads"],
                    "views_14d": views.get("count", ""),
                    "unique_visitors_14d": views.get("uniques", ""),
                    "clones_14d": clones.get("count", ""),
                    "unique_cloners_14d": clones.get("uniques", ""),
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="OWNER/REPO")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN") or None
    traffic_token = os.environ.get("METRICS_TRAFFIC_TOKEN") or token
    collected_at = datetime.now(timezone.utc)
    date_kst = collected_at.astimezone(SEOUL).date().isoformat()
    previous = load_previous_snapshot(args.output_dir, date_kst)

    releases = fetch_releases(args.repository, token)
    traffic = fetch_traffic(args.repository, traffic_token)
    snapshot = build_snapshot(
        args.repository,
        releases,
        traffic,
        previous=previous,
        collected_at=collected_at,
    )
    write_outputs(args.output_dir, snapshot)
    print(
        f"{snapshot['date_kst']} 설치 파일 누적 다운로드: "
        f"{snapshot['totals']['installer_downloads']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
