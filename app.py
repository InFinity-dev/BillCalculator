"""공과금 정산 시스템 — Flask 애플리케이션.

DB 계층은 SQLite 기반이며 스키마는 ``models.py`` + ``migrations/`` 가 정의한다.
MySQL 의존성과 하드코딩된 자격증명은 제거되었다.

실행::

    flask db upgrade        # 스키마 생성/갱신
    flask seed-settings     # 설정 기본값 시딩 (멱등)
    python app.py

계산 로직(전기/수도/공동 배분, grossing-up, 10원 단위 올림)은
DB 전환 과정에서 **변경하지 않았다**.
"""

import json
import math
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

import click
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

import balance as balance_service
from config import Config, ensure_data_dir
from extensions import db, migrate, register_sqlite_pragmas
from models import (
    CommonBill,
    CommonBillDetail,
    ElectricBill,
    ElectricBillDetail,
    ElectricBillMonth,
    ElectricReading,
    FinalInvoice,
    FinalInvoiceCharge,
    Floor,
    InvoiceCombination,
    InvoiceCombinationItem,
    Payment,
    Setting,
    Unit,
    WaterBill,
    WaterBillDetail,
)
from settings_registry import SETTING_DEFS, SETTING_KEYS, get_def

# =========================
# Safe numeric helpers & JSON provider (Decimal-safe)
# =========================
try:
    from flask.json.provider import DefaultJSONProvider
except ImportError:  # pragma: no cover - Flask 2 이하
    DefaultJSONProvider = None


def dec(val, q=None):
    """임의 입력을 Decimal 로 변환한다. 실패 시 0.

    계산 로직이 의존하는 함수이므로 동작을 변경하지 않았다.
    """
    if isinstance(val, Decimal):
        x = val
    elif isinstance(val, (int, float)):
        x = Decimal(str(val))
    else:
        try:
            s = (val if val is not None else "").strip()
        except (AttributeError, TypeError):
            s = str(val or "")
        if s == "":
            x = Decimal("0")
        else:
            try:
                x = Decimal(s.replace(",", ""))
            except InvalidOperation:
                x = Decimal("0")
    if q:
        try:
            x = x.quantize(q)
        except InvalidOperation:
            pass
    return x


def to_int(val, default=0):
    try:
        s = (val if val is not None else "").strip()
    except (AttributeError, TypeError):
        s = str(val or "")
    if s == "":
        return default
    try:
        return int(float(s.replace(",", "")))
    except (TypeError, ValueError):
        return default


# ======================================================
# Flask / DB bootstrap
# ======================================================
def create_app(config_object=Config):
    application = Flask(__name__)
    application.config.from_object(config_object)

    if DefaultJSONProvider is not None:

        class DecimalJSONProvider(DefaultJSONProvider):
            def default(self, o):
                if isinstance(o, Decimal):
                    return float(o)
                return super().default(o)

        application.json = DecimalJSONProvider(application)

    register_sqlite_pragmas(application.config["SQLITE_PRAGMAS"])
    ensure_data_dir()

    db.init_app(application)
    # render_as_batch: SQLite 는 ALTER TABLE 로 제약을 바꿀 수 없으므로
    # Alembic 이 "새 테이블 생성 → 복사 → 교체" 방식을 쓰게 한다.
    migrate.init_app(application, db, render_as_batch=True, compare_type=True)

    application.jinja_env.globals["csrf_token"] = generate_csrf_token
    register_cli(application)
    return application


# ======================================================
# CSRF
# ======================================================
def generate_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]


def csrf_protect(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == "POST":
            token = session.get("_csrf_token", None)
            req_token = request.form.get("_csrf_token") or (
                request.get_json(silent=True) or {}
            ).get("_csrf_token")
            if not token or token != req_token:
                return jsonify({"success": False, "message": "CSRF 토큰이 유효하지 않습니다."}), 403
        return f(*args, **kwargs)

    return decorated_function


# ======================================================
# Utils
# ======================================================
def round_up_to_10(amount):
    """10원 단위 올림. 도메인 정책이므로 구현을 변경하지 않는다."""
    return math.ceil(float(amount) / 10) * 10


def get_setting(key, default=None):
    """설정 값을 문자열로 조회한다.

    등록되지 않은 키는 즉시 실패시킨다 (오타로 인한 조용한 기본값 사용 방지).
    """
    definition = get_def(key)
    row = Setting.query.filter_by(setting_key=key).first()
    if row is None or row.setting_value is None:
        return definition.default if default is None else default
    return row.setting_value


def set_setting(key, value):
    definition = get_def(key)
    normalized = definition.normalize(value)
    row = Setting.query.filter_by(setting_key=key).first()
    if row:
        row.setting_value = normalized
    else:
        db.session.add(Setting(setting_key=key, setting_value=normalized))


def all_settings():
    """등록된 전체 설정을 {key: 문자열} 로 반환한다."""
    stored = {s.setting_key: s.setting_value for s in Setting.query.all()}
    return {
        d.key: stored.get(d.key) if stored.get(d.key) is not None else d.default
        for d in SETTING_DEFS
    }


def seed_settings():
    """등록된 설정 키의 기본값을 시딩한다. 기존 값은 덮어쓰지 않는다 (멱등)."""
    existing = {s.setting_key for s in Setting.query.all()}
    created = []
    for definition in SETTING_DEFS:
        if definition.key not in existing:
            db.session.add(
                Setting(setting_key=definition.key, setting_value=definition.default)
            )
            created.append(definition.key)
    db.session.commit()
    return created


def create_unit_snapshot(unit):
    """계산 시점의 세대 속성을 박제한다.

    이 스냅샷 덕분에 세대 마스터가 나중에 바뀌어도 과거 정산 내역이 왜곡되지 않는다.
    구조를 변경하지 않는다.
    """
    return {
        "unit_name": unit.unit_name,
        "electric_welfare": unit.electric_welfare,
        "electric_voucher": unit.electric_voucher,
        "has_tv": unit.has_tv,
        "water_welfare": unit.water_welfare,
        "residents_count": unit.residents_count,
        "is_vacant": unit.is_vacant,
    }


def first_of_month(d):
    return date(d.year, d.month, 1)


def parse_month_input(raw):
    """``<input type="month">`` 값('YYYY-MM') 또는 'YYYY-MM-DD' 를 그 달 1일로 변환한다.

    실패 시 None. 기존에는 세 라우트에 흩어져 있던 정규화를 한 곳으로 모았다.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def json_error(message, status=200, **extra):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return jsonify(payload), status


# ======================================================
# CLI
# ======================================================
def register_cli(application):
    @application.cli.command("seed-settings")
    def seed_settings_command():
        """설정 기본값을 시딩한다 (멱등)."""
        created = seed_settings()
        if created:
            click.echo("시딩된 설정: {}".format(", ".join(created)))
        else:
            click.echo("모든 설정이 이미 존재합니다.")

    @application.cli.command("check-db")
    def check_db_command():
        """DB 정합성을 검증한다."""
        from db_validate import run_all_checks, format_report

        click.echo(format_report(run_all_checks()))

    @application.cli.command("backup-db")
    @click.option("--output", default=None, help="백업 파일 경로")
    def backup_db_command(output):
        """SQLite 네이티브 backup API 로 안전하게 백업한다."""
        from db_backup import backup_database

        path = backup_database(output)
        click.echo("백업 완료: {}".format(path))


app = create_app()


# ======================================================
# Routes - Core pages
# ======================================================
@app.route("/")
def index():
    floors_count = Floor.query.count()
    units_count = Unit.query.count()
    vacant_count = Unit.query.filter_by(is_vacant=True).count()
    occupied_count = units_count - vacant_count
    return render_template(
        "index.html",
        floors_count=floors_count,
        units_count=units_count,
        vacant_count=vacant_count,
        occupied_count=occupied_count,
    )


@app.route("/settings", endpoint="settings")
def settings_page():
    floors = Floor.query.order_by(Floor.floor_number).all()
    ctx = dict(all_settings())
    ctx["floors"] = floors
    return render_template("settings.html", **ctx)


@app.route("/settings/save", methods=["POST"])
@csrf_protect
def save_settings():
    try:
        for key in SETTING_KEYS:
            if key in request.form:
                set_setting(key, request.form.get(key))
        db.session.commit()
        flash("설정이 저장되었습니다.", "success")
    except Exception as e:  # noqa: BLE001 - 사용자에게 실패를 알려야 한다
        db.session.rollback()
        flash("설정 저장 실패: {}".format(e), "error")
    return redirect(url_for("settings"))


@app.route("/settings/export")
def export_settings():
    floors = Floor.query.order_by(Floor.floor_number).all()
    payload = {
        "version": 2,
        "settings": all_settings(),
        "floors": [
            {
                "floor_number": f.floor_number,
                "name": f.name,
                "electric_contract_number": f.electric_contract_number,
                "units": [
                    {
                        "unit_name": u.unit_name,
                        "memo": u.memo,
                        "electric_welfare": bool(u.electric_welfare),
                        "electric_voucher": bool(u.electric_voucher),
                        "has_tv": bool(u.has_tv),
                        "water_welfare": bool(u.water_welfare),
                        "residents_count": u.residents_count,
                        "is_vacant": bool(u.is_vacant),
                    }
                    for u in f.units
                ],
            }
            for f in floors
        ],
    }
    return jsonify(payload)


def _validate_import_payload(data):
    """Import payload 를 검증하고 오류 목록을 반환한다."""
    errors = []
    if not isinstance(data, dict):
        return ["최상위 구조가 객체가 아닙니다."]

    settings_part = data.get("settings", {})
    if not isinstance(settings_part, dict):
        errors.append("'settings' 는 객체여야 합니다.")
    else:
        for key in settings_part:
            if key not in SETTING_KEYS:
                errors.append("알 수 없는 설정 키: {}".format(key))

    floors_part = data.get("floors", [])
    if not isinstance(floors_part, list):
        return errors + ["'floors' 는 배열이어야 합니다."]

    seen_floor_numbers = set()
    for idx, f in enumerate(floors_part):
        if not isinstance(f, dict):
            errors.append("floors[{}] 가 객체가 아닙니다.".format(idx))
            continue
        try:
            floor_number = int(f.get("floor_number"))
        except (TypeError, ValueError):
            errors.append("floors[{}].floor_number 가 정수가 아닙니다.".format(idx))
            continue
        if floor_number in seen_floor_numbers:
            errors.append("floor_number {} 가 중복되었습니다.".format(floor_number))
        seen_floor_numbers.add(floor_number)

        units_part = f.get("units", [])
        if not isinstance(units_part, list):
            errors.append("floors[{}].units 가 배열이 아닙니다.".format(idx))
            continue
        seen_unit_names = set()
        for uidx, u in enumerate(units_part):
            if not isinstance(u, dict):
                errors.append("floors[{}].units[{}] 가 객체가 아닙니다.".format(idx, uidx))
                continue
            name = (u.get("unit_name") or "").strip()
            if not name:
                errors.append("floors[{}].units[{}].unit_name 이 비어 있습니다.".format(idx, uidx))
                continue
            if name in seen_unit_names:
                errors.append(
                    "floor_number {} 안에서 세대명 '{}' 이 중복되었습니다.".format(floor_number, name)
                )
            seen_unit_names.add(name)
            residents = u.get("residents_count", 1)
            try:
                if int(residents) < 0:
                    errors.append("세대 '{}' 의 거주인원이 음수입니다.".format(name))
            except (TypeError, ValueError):
                errors.append("세대 '{}' 의 거주인원이 정수가 아닙니다.".format(name))
    return errors


@app.route("/settings/import", methods=["POST"])
@csrf_protect
def import_settings():
    """층/세대 설정을 병합(merge/upsert)한다.

    기존 구현은 ``Floor`` 를 전부 삭제한 뒤 재생성했다. FK 정책에 따라
    "전부 실패" 또는 "과거 계산/정산/납부 데이터 연쇄 삭제" 로 갈리는 구조였다.
    (codebase-analysis 06-technical-debt.md C-4)

    새 동작
    -------
    - 층은 ``floor_number``, 세대는 ``(floor, unit_name)`` 을 자연키로 upsert 한다.
    - **payload 에 없는 기존 층/세대는 삭제하지 않는다.** 회계 데이터가 보존된다.
    - 기존 세대의 ``id`` 가 유지되므로 과거 계산의 FK 참조가 그대로 유효하다.
    - 검증 실패 시 DB 를 전혀 건드리지 않는다.
    - 전체가 하나의 트랜잭션이며 중간 실패 시 전부 롤백된다.
    """
    data = request.get_json(silent=True)
    if data is None:
        return json_error("JSON 본문을 해석할 수 없습니다.")

    errors = _validate_import_payload(data)
    if errors:
        return json_error(
            "가져오기 데이터가 올바르지 않습니다.", errors=errors[:20]
        )

    summary = {
        "floors_created": 0,
        "floors_updated": 0,
        "units_created": 0,
        "units_updated": 0,
        "floors_kept": 0,
        "units_kept": 0,
    }

    try:
        for key, value in (data.get("settings") or {}).items():
            set_setting(key, value)

        existing_floors = {f.floor_number: f for f in Floor.query.all()}
        incoming_floor_numbers = set()

        for f in data.get("floors", []):
            floor_number = int(f.get("floor_number"))
            incoming_floor_numbers.add(floor_number)
            floor = existing_floors.get(floor_number)
            if floor is None:
                floor = Floor(floor_number=floor_number)
                db.session.add(floor)
                summary["floors_created"] += 1
            else:
                summary["floors_updated"] += 1
            floor.name = f.get("name") or floor.name
            floor.electric_contract_number = f.get("electric_contract_number")
            db.session.flush()

            existing_units = {u.unit_name: u for u in floor.units}
            for u in f.get("units", []):
                name = (u.get("unit_name") or "").strip()
                unit = existing_units.get(name)
                if unit is None:
                    unit = Unit(floor_id=floor.id, unit_name=name)
                    db.session.add(unit)
                    summary["units_created"] += 1
                else:
                    summary["units_updated"] += 1
                unit.memo = u.get("memo", "") or ""
                unit.electric_welfare = bool(u.get("electric_welfare", False))
                unit.electric_voucher = bool(u.get("electric_voucher", False))
                unit.has_tv = bool(u.get("has_tv", True))
                unit.water_welfare = bool(u.get("water_welfare", False))
                unit.residents_count = int(u.get("residents_count", 1))
                unit.is_vacant = bool(u.get("is_vacant", False))

            summary["units_kept"] += len(
                [n for n in existing_units if n not in {
                    (u.get("unit_name") or "").strip() for u in f.get("units", [])
                }]
            )

        summary["floors_kept"] = len(
            [n for n in existing_floors if n not in incoming_floor_numbers]
        )

        db.session.commit()
        return jsonify(
            {
                "success": True,
                "message": "설정을 가져왔습니다. 기존 계산/정산/납부 데이터는 삭제되지 않았습니다.",
                "summary": summary,
            }
        )
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error("Import 실패: {}".format(e))


@app.route("/floors/add", methods=["POST"])
@csrf_protect
def add_floor():
    try:
        floor_number_str = request.form.get("floor_number", "").strip()
        if not floor_number_str:
            return json_error("층 번호를 입력해주세요.")

        floor_number = to_int(floor_number_str, None)
        if floor_number is None:
            return json_error("층 번호는 정수로 입력해주세요.")

        name = request.form.get("name") or (
            "B{}층".format(abs(floor_number)) if floor_number < 0 else "{}층".format(floor_number)
        )
        electric_contract_number = request.form.get("electric_contract_number", "").strip() or None

        if Floor.query.filter_by(floor_number=floor_number).first():
            return json_error("이미 같은 층 번호가 존재합니다.")

        db.session.add(
            Floor(
                floor_number=floor_number,
                name=name,
                electric_contract_number=electric_contract_number,
            )
        )
        db.session.commit()
        return jsonify({"success": True, "message": "층이 추가되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("이미 같은 층 번호가 존재합니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error("층 추가 실패: {}".format(e))


@app.route("/floors/<int:floor_id>/update", methods=["POST"])
@csrf_protect
def update_floor(floor_id):
    try:
        floor = db.session.get(Floor, floor_id)
        if floor is None:
            return json_error("층을 찾을 수 없습니다.", status=404)
        new_name = (request.form.get("name") or "").strip()
        new_number_raw = (request.form.get("floor_number") or "").strip()
        new_contract = (request.form.get("electric_contract_number") or "").strip()

        if new_number_raw:
            new_number = to_int(new_number_raw, None)
            if new_number is None:
                return json_error("층 번호는 정수로 입력하세요.")
            exists = Floor.query.filter(
                Floor.floor_number == new_number, Floor.id != floor.id
            ).first()
            if exists:
                return json_error("이미 같은 층 번호가 존재합니다.")
            floor.floor_number = new_number
            if not new_name:
                new_name = (
                    "B{}층".format(abs(new_number)) if new_number < 0 else "{}층".format(new_number)
                )

        if new_name:
            floor.name = new_name

        floor.electric_contract_number = new_contract if new_contract else None

        db.session.commit()
        return jsonify({"success": True, "message": "층 정보가 수정되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


def _unit_reference_counts(unit_ids):
    """세대를 참조하는 회계 레코드 수를 센다 (삭제 사전 검사용)."""
    if not unit_ids:
        return {}
    checks = (
        ("전기 검침", ElectricReading),
        ("전기 정산 내역", ElectricBillDetail),
        ("수도 정산 내역", WaterBillDetail),
        ("공동 공과금 내역", CommonBillDetail),
        ("정산서", FinalInvoice),
        ("납부 내역", Payment),
    )
    counts = {}
    for label, model in checks:
        n = model.query.filter(model.unit_id.in_(unit_ids)).count()
        if n:
            counts[label] = n
    return counts


@app.route("/floors/<int:floor_id>/delete", methods=["POST"])
@csrf_protect
def delete_floor(floor_id):
    try:
        floor = db.session.get(Floor, floor_id)
        if floor is None:
            return json_error("층을 찾을 수 없습니다.", status=404)

        bill_count = ElectricBill.query.filter_by(floor_id=floor.id).count()
        if bill_count:
            return json_error(
                "이 층에는 전기요금 정산 내역 {}건이 있어 삭제할 수 없습니다. "
                "회계 기록을 보호하기 위한 제한입니다.".format(bill_count)
            )

        unit_ids = [u.id for u in floor.units]
        refs = _unit_reference_counts(unit_ids)
        if refs:
            detail = ", ".join("{} {}건".format(k, v) for k, v in refs.items())
            return json_error(
                "이 층의 세대에 {}이(가) 있어 삭제할 수 없습니다. "
                "과거 기록을 보존하려면 세대를 '공실'로 표시하세요.".format(detail)
            )

        db.session.delete(floor)
        db.session.commit()
        return jsonify({"success": True, "message": "층이 삭제되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("참조 중인 데이터가 있어 삭제할 수 없습니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/units/add", methods=["POST"])
@csrf_protect
def add_unit():
    try:
        floor_id = to_int(request.form.get("floor_id"), 0)
        if not floor_id:
            return json_error("층을 선택해주세요.")

        unit = Unit(
            floor_id=floor_id,
            unit_name=request.form.get("unit_name"),
            memo=request.form.get("memo", ""),
            electric_welfare=request.form.get("electric_welfare") == "true",
            electric_voucher=request.form.get("electric_voucher") == "true",
            has_tv=request.form.get("has_tv") == "true",
            water_welfare=request.form.get("water_welfare") == "true",
            residents_count=to_int(request.form.get("residents_count", "1"), 1),
            is_vacant=request.form.get("is_vacant") == "true",
        )
        db.session.add(unit)
        db.session.commit()
        return jsonify({"success": True, "message": "세대가 추가되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("같은 층에 동일한 세대명이 이미 존재합니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/units/<int:unit_id>/update", methods=["POST"])
@csrf_protect
def update_unit(unit_id):
    try:
        unit = db.session.get(Unit, unit_id)
        if unit is None:
            return json_error("세대를 찾을 수 없습니다.", status=404)
        unit.unit_name = request.form.get("unit_name", unit.unit_name)
        unit.memo = request.form.get("memo", "")
        unit.electric_welfare = request.form.get("electric_welfare") == "true"
        unit.electric_voucher = request.form.get("electric_voucher") == "true"
        unit.has_tv = request.form.get("has_tv") == "true"
        unit.water_welfare = request.form.get("water_welfare") == "true"
        unit.residents_count = to_int(
            request.form.get("residents_count"), unit.residents_count or 1
        )
        unit.is_vacant = request.form.get("is_vacant") == "true"
        db.session.commit()
        return jsonify({"success": True, "message": "세대 정보가 수정되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("같은 층에 동일한 세대명이 이미 존재합니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/units/<int:unit_id>/delete", methods=["POST"])
@csrf_protect
def delete_unit(unit_id):
    try:
        unit = db.session.get(Unit, unit_id)
        if unit is None:
            return json_error("세대를 찾을 수 없습니다.", status=404)

        refs = _unit_reference_counts([unit.id])
        if refs:
            detail = ", ".join("{} {}건".format(k, v) for k, v in refs.items())
            return json_error(
                "이 세대에는 {}이(가) 있어 삭제할 수 없습니다. "
                "과거 기록을 보존하려면 '공실'로 표시하세요.".format(detail)
            )

        db.session.delete(unit)
        db.session.commit()
        return jsonify({"success": True, "message": "세대가 삭제되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("참조 중인 데이터가 있어 삭제할 수 없습니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


# ======================================================
# Calculator (전기/수도/공동 계산)
# ======================================================
@app.route("/calculator")
def calculator():
    floors = Floor.query.order_by(Floor.floor_number).all()
    units = Unit.query.order_by(Unit.floor_id, Unit.unit_name).all()
    total_units = len(units)
    occupied_units = sum(1 for u in units if not u.is_vacant)
    vacant_units = total_units - occupied_units
    total_residents = sum(u.residents_count for u in units if not u.is_vacant)

    floors_json = [
        {
            "id": f.id,
            "name": f.name,
            "floor_number": f.floor_number,
            "electric_contract_number": f.electric_contract_number,
        }
        for f in floors
    ]

    units_json = [
        {
            "id": u.id,
            "floor_id": u.floor_id,
            "unit_name": u.unit_name,
            "residents_count": u.residents_count,
            "is_vacant": bool(u.is_vacant),
            "has_tv": bool(u.has_tv),
            "electric_welfare": bool(u.electric_welfare),
            "electric_voucher": bool(u.electric_voucher),
            "water_welfare": bool(u.water_welfare),
        }
        for u in units
    ]

    return render_template(
        "calculator.html",
        floors=floors,
        units=units,
        floors_json=floors_json,
        units_json=units_json,
        total_units=total_units,
        occupied_units=occupied_units,
        vacant_units=vacant_units,
        total_residents=total_residents,
        electric_bill_url=get_setting("electric_bill_url"),
        water_bill_url=get_setting("water_bill_url"),
        water_customer_number=get_setting("water_customer_number"),
    )


def _electric_bill_in_use(bill_id):
    """전기 고지가 발행된 정산서에 포함되어 있는지."""
    return InvoiceCombinationItem.query.filter_by(electric_bill_id=bill_id).count()


@app.route("/calculate/electric", methods=["POST"])
@csrf_protect
def calculate_electric():
    """층 단위 전기요금을 세대 계량기 사용량 비율로 배분한다.

    배분 알고리즘(grossing-up, TV 배분, 할인 단가, 10원 올림)은 변경하지 않았다.
    영속화만 ``monthly_details`` JSON → ``electric_bill_months`` 테이블로 바뀌었다.
    """
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        floor_id = to_int(request.form.get("floor_id"), 0)
        if not floor_id:
            return json_error("층을 선택해주세요.")
        tv_distribution_mode = request.form.get("tv_distribution_mode", "INDIVIDUAL")

        monthly_details = []
        total_amount = dec(0)
        welfare_discount_input = dec(0)
        voucher_discount_input = dec(0)
        tv_fee_total = dec(0)

        month_count = to_int(request.form.get("month_count", "1"), 1)

        # FormData 에서 bill_month_ 로 시작하는 키를 찾아 동적 rowId 를 추출한다.
        bill_months = {}
        for key in request.form.keys():
            if key.startswith("bill_month_"):
                row_id = key.replace("bill_month_", "")
                bill_months[row_id] = {
                    "month": request.form.get("bill_month_{}".format(row_id)),
                    "amount": dec(request.form.get("bill_amount_{}".format(row_id), 0)),
                    "welfare": dec(request.form.get("bill_welfare_{}".format(row_id), 0)),
                    "voucher": dec(request.form.get("bill_voucher_{}".format(row_id), 0)),
                    "tv_fee": dec(request.form.get("bill_tv_fee_{}".format(row_id), 0)),
                }

        # 월별 데이터를 리스트로 변환하고 합계 계산
        seen_months = set()
        for row_id, month_data in bill_months.items():
            parsed = parse_month_input(month_data["month"])
            if parsed is None:
                # 고지월은 DATE 컬럼이 되었으므로 비워둘 수 없다.
                # UI 는 이미 required 로 막고 있으며, 임의로 정산월을 대입하면
                # '정산월 != 고지월' 이라는 도메인 개념이 훼손된다.
                return json_error("월별 고지 내역의 고지월을 모두 입력해주세요.")
            if parsed in seen_months:
                return json_error(
                    "고지월 {}이(가) 중복되었습니다.".format(parsed.strftime("%Y-%m"))
                )
            seen_months.add(parsed)
            month_data["parsed_month"] = parsed
            monthly_details.append(month_data)
            total_amount += month_data["amount"]
            welfare_discount_input += month_data["welfare"]
            voucher_discount_input += month_data["voucher"]
            tv_fee_total += month_data["tv_fee"]

        existing = ElectricBill.query.filter_by(
            billing_month=billing_month, floor_id=floor_id
        ).first()
        if existing and request.form.get("overwrite") != "true":
            return jsonify(
                {
                    "success": False,
                    "exists": True,
                    "already_exists": True,
                    "message": "해당 월의 전기요금이 이미 존재합니다.",
                }
            )
        if existing:
            in_use = _electric_bill_in_use(existing.id)
            if in_use:
                return json_error(
                    "이 전기요금은 이미 발행된 정산서 {}건에 포함되어 있어 "
                    "다시 계산할 수 없습니다. 해당 정산서를 먼저 삭제하세요.".format(in_use)
                )
            db.session.delete(existing)
            db.session.flush()

        bill = ElectricBill(
            billing_month=billing_month,
            floor_id=floor_id,
            total_amount=total_amount,
            welfare_discount=dec(0),
            voucher_discount=dec(0),
            tv_fee_total=tv_fee_total,
            tv_distribution_mode=tv_distribution_mode,
            billing_months_count=len(monthly_details),
        )
        db.session.add(bill)
        db.session.flush()

        for month_data in monthly_details:
            db.session.add(
                ElectricBillMonth(
                    electric_bill_id=bill.id,
                    billing_month=month_data["parsed_month"],
                    amount=month_data["amount"],
                    welfare_discount=month_data["welfare"],
                    voucher_discount=month_data["voucher"],
                    tv_fee=month_data["tv_fee"],
                )
            )

        floor = db.session.get(Floor, floor_id)
        if floor is None:
            db.session.rollback()
            return json_error("선택한 층을 찾을 수 없습니다.")

        units = [u for u in floor.units if not u.is_vacant]
        total_usage = dec(0)
        readings = []

        for unit in units:
            prev_reading = dec(request.form.get("prev_{}".format(unit.id), 0))
            curr_reading = dec(request.form.get("curr_{}".format(unit.id), 0))
            total_usage += curr_reading - prev_reading
            reading = ElectricReading(
                electric_bill_id=bill.id,
                unit_id=unit.id,
                previous_reading=prev_reading,
                current_reading=curr_reading,
            )
            db.session.add(reading)
            readings.append(reading)

        tv_fee = dec(get_setting("tv_fee") or "2500")

        if tv_distribution_mode == "EQUAL":
            tv_fee_per_unit = (tv_fee_total / len(units)) if units else dec(0)
        else:
            tv_fee_per_unit = tv_fee * month_count

        welfare_units = [u for u in units if u.electric_welfare]
        voucher_units = [u for u in units if u.electric_voucher]

        if welfare_discount_input > 0 and welfare_units:
            welfare_per_unit = welfare_discount_input / len(welfare_units)
            total_welfare_to_apply = welfare_discount_input
        elif welfare_units:
            welfare_per_unit = dec(get_setting("electric_welfare_amount")) * month_count
            total_welfare_to_apply = welfare_per_unit * len(welfare_units)
        else:
            welfare_per_unit = dec(0)
            total_welfare_to_apply = dec(0)

        if voucher_discount_input > 0 and voucher_units:
            voucher_per_unit = voucher_discount_input / len(voucher_units)
            total_voucher_to_apply = voucher_discount_input
        elif voucher_units:
            voucher_per_unit = dec(get_setting("electric_voucher_amount")) * month_count
            total_voucher_to_apply = voucher_per_unit * len(voucher_units)
        else:
            voucher_per_unit = dec(0)
            total_voucher_to_apply = dec(0)

        original_amount = total_amount + total_welfare_to_apply + total_voucher_to_apply

        for unit, reading in zip(units, readings):
            usage = reading.current_reading - reading.previous_reading

            base_amount = (
                (usage / total_usage) * original_amount
                if total_usage > 0
                else (original_amount / len(units) if units else dec(0))
            )

            unit_welfare = welfare_per_unit if unit.electric_welfare else dec(0)
            unit_voucher = voucher_per_unit if unit.electric_voucher else dec(0)

            if tv_distribution_mode == "EQUAL":
                unit_tv_fee = tv_fee_per_unit
            else:
                unit_tv_fee = tv_fee_per_unit if unit.has_tv else dec(0)

            final_amount = base_amount - unit_welfare - unit_voucher + unit_tv_fee
            if final_amount < 0:
                final_amount = dec(0)

            charged_amount = dec(round_up_to_10(final_amount))

            db.session.add(
                ElectricBillDetail(
                    electric_bill_id=bill.id,
                    unit_id=unit.id,
                    usage_amount=usage,
                    base_amount=base_amount,
                    welfare_discount=unit_welfare,
                    voucher_discount=unit_voucher,
                    tv_fee=unit_tv_fee,
                    final_amount=final_amount,
                    charged_amount=charged_amount,
                    unit_snapshot=create_unit_snapshot(unit),
                )
            )

        bill.welfare_discount = total_welfare_to_apply
        bill.voucher_discount = total_voucher_to_apply

        db.session.commit()
        return jsonify({"success": True, "message": "전기요금이 계산되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


def _water_bill_in_use(bill_id):
    return InvoiceCombinationItem.query.filter_by(water_bill_id=bill_id).count()


@app.route("/calculate/water", methods=["POST"])
@csrf_protect
def calculate_water():
    """주택 전체 수도요금을 거주 인원수 비율로 배분한다. 알고리즘 변경 없음."""
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        total_amount = dec(request.form.get("total_amount"))
        welfare_discount_input = dec(request.form.get("welfare_discount_total", "0"))

        excluded_units_json = request.form.get("excluded_units", "[]")
        try:
            excluded_unit_ids = set(map(int, json.loads(excluded_units_json)))
        except (ValueError, TypeError):
            excluded_unit_ids = set()

        existing = WaterBill.query.filter_by(billing_month=billing_month).first()
        if existing and request.form.get("overwrite") != "true":
            return jsonify(
                {
                    "success": False,
                    "exists": True,
                    "already_exists": True,
                    "message": "해당 월의 수도요금이 이미 존재합니다.",
                }
            )
        if existing:
            in_use = _water_bill_in_use(existing.id)
            if in_use:
                return json_error(
                    "이 수도요금은 이미 발행된 정산서 {}건에 포함되어 있어 "
                    "다시 계산할 수 없습니다. 해당 정산서를 먼저 삭제하세요.".format(in_use)
                )
            db.session.delete(existing)
            db.session.flush()

        bill = WaterBill(
            billing_month=billing_month,
            total_amount=total_amount,
            welfare_discount_total=dec(0),
        )
        db.session.add(bill)
        db.session.flush()

        all_units = Unit.query.filter_by(is_vacant=False).all()
        included_units = [u for u in all_units if u.id not in excluded_unit_ids]
        total_residents = sum(u.residents_count for u in included_units)
        welfare_units = [u for u in included_units if u.water_welfare]

        if welfare_discount_input > 0 and welfare_units:
            welfare_per_unit = welfare_discount_input / len(welfare_units)
            total_welfare_to_apply = welfare_discount_input
        elif welfare_units:
            welfare_per_unit = dec(get_setting("water_welfare_amount"))
            total_welfare_to_apply = welfare_per_unit * len(welfare_units)
        else:
            welfare_per_unit = dec(0)
            total_welfare_to_apply = dec(0)

        original_amount = total_amount + total_welfare_to_apply

        for unit in all_units:
            if unit.id in excluded_unit_ids:
                detail = WaterBillDetail(
                    water_bill_id=bill.id,
                    unit_id=unit.id,
                    base_amount=dec(0),
                    welfare_discount=dec(0),
                    final_amount=dec(0),
                    charged_amount=dec(0),
                    unit_snapshot=create_unit_snapshot(unit),
                    is_excluded=True,
                )
            else:
                if total_residents > 0:
                    base_amount = (
                        dec(unit.residents_count) / dec(total_residents)
                    ) * original_amount
                elif len(included_units) > 0:
                    base_amount = original_amount / len(included_units)
                else:
                    base_amount = dec(0)

                unit_welfare = welfare_per_unit if unit.water_welfare else dec(0)
                final_amount = base_amount - unit_welfare
                if final_amount < 0:
                    final_amount = dec(0)
                charged_amount = dec(round_up_to_10(final_amount))

                detail = WaterBillDetail(
                    water_bill_id=bill.id,
                    unit_id=unit.id,
                    base_amount=base_amount,
                    welfare_discount=unit_welfare,
                    final_amount=final_amount,
                    charged_amount=charged_amount,
                    unit_snapshot=create_unit_snapshot(unit),
                    is_excluded=False,
                )

            db.session.add(detail)

        bill.welfare_discount_total = total_welfare_to_apply

        db.session.commit()
        return jsonify({"success": True, "message": "수도요금이 계산되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/calculate/common", methods=["POST"])
@csrf_protect
def calculate_common():
    """공동 공과금을 인원 비례 또는 세대 균등으로 배분한다. 알고리즘 변경 없음."""
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        description = request.form.get("description")
        total_amount = dec(request.form.get("total_amount"))
        distribution_method = request.form.get("distribution_method", "BY_RESIDENTS")

        bill = CommonBill(
            billing_month=billing_month,
            description=description,
            total_amount=total_amount,
            distribution_method=distribution_method,
        )
        db.session.add(bill)
        db.session.flush()

        units = Unit.query.filter_by(is_vacant=False).all()

        if distribution_method == "BY_RESIDENTS":
            total_residents = sum(u.residents_count for u in units)
            for unit in units:
                amount = (
                    (dec(unit.residents_count) / dec(total_residents) * total_amount)
                    if total_residents > 0
                    else (total_amount / len(units) if units else dec(0))
                )
                charged_amount = dec(round_up_to_10(amount))
                db.session.add(
                    CommonBillDetail(
                        common_bill_id=bill.id,
                        unit_id=unit.id,
                        amount=amount,
                        charged_amount=charged_amount,
                        unit_snapshot=create_unit_snapshot(unit),
                    )
                )
        else:
            amount_per_unit = total_amount / len(units) if units else dec(0)
            for unit in units:
                charged_amount = dec(round_up_to_10(amount_per_unit))
                db.session.add(
                    CommonBillDetail(
                        common_bill_id=bill.id,
                        unit_id=unit.id,
                        amount=amount_per_unit,
                        charged_amount=charged_amount,
                        unit_snapshot=create_unit_snapshot(unit),
                    )
                )

        db.session.commit()
        return jsonify({"success": True, "message": "공동 공과금이 계산되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


# ======================================================
# Views / Delete
# ======================================================
@app.route("/view")
def view_bills():
    view_type = request.args.get("view", "month")
    selected_month = request.args.get("month")
    selected_floor = request.args.get("floor")
    selected_unit = request.args.get("unit")

    electric_bills = (
        ElectricBill.query.options(
            selectinload(ElectricBill.months),
            selectinload(ElectricBill.details).joinedload(ElectricBillDetail.unit),
        )
        .order_by(ElectricBill.billing_month.desc())
        .all()
    )
    water_bills = WaterBill.query.order_by(WaterBill.billing_month.desc()).all()
    common_bills = CommonBill.query.order_by(
        CommonBill.billing_month.desc(), CommonBill.id.desc()
    ).all()
    floors = Floor.query.order_by(Floor.floor_number).all()
    units = Unit.query.order_by(Unit.floor_id, Unit.unit_name).all()

    electric_bills_json = [
        {
            "id": b.id,
            "billing_month": b.billing_month.isoformat(),
            "floor_id": b.floor_id,
            "floor_name": b.floor_ref.name if b.floor_ref else "",
            "total_amount": b.total_amount,
            "welfare_discount": b.welfare_discount or 0,
            "voucher_discount": b.voucher_discount or 0,
            "tv_fee_total": b.tv_fee_total or 0,
            "billing_months_count": b.billing_months_count or 1,
            "monthly_details": b.monthly_details,
            "details": [
                {
                    "unit_name": d.unit.unit_name,
                    "usage_amount": float(d.usage_amount),
                    "base_amount": float(d.base_amount),
                    "welfare_discount": float(d.welfare_discount or 0),
                    "voucher_discount": float(d.voucher_discount or 0),
                    "tv_fee": float(d.tv_fee or 0),
                    "final_amount": float(d.final_amount),
                    "charged_amount": d.charged_amount,
                }
                for d in b.details
            ],
        }
        for b in electric_bills
    ]

    water_bills_json = [
        {
            "id": b.id,
            "billing_month": b.billing_month.isoformat(),
            "total_amount": b.total_amount,
            "welfare_discount_total": b.welfare_discount_total or 0,
            "unit_count": len(b.details),
        }
        for b in water_bills
    ]

    common_bills_json = [
        {
            "id": b.id,
            "billing_month": b.billing_month.isoformat(),
            "description": b.description or "",
            "total_amount": b.total_amount,
            "distribution_method": b.distribution_method,
            "unit_count": len(b.details),
        }
        for b in common_bills
    ]

    return render_template(
        "view.html",
        view_type=view_type,
        electric_bills=electric_bills,
        water_bills=water_bills,
        common_bills=common_bills,
        electric_bills_json=electric_bills_json,
        water_bills_json=water_bills_json,
        common_bills_json=common_bills_json,
        floors=floors,
        units=units,
        selected_month=selected_month,
        selected_floor=selected_floor,
        selected_unit=selected_unit,
    )


@app.route("/view/electric/<int:bill_id>")
def view_electric_detail(bill_id):
    bill = db.get_or_404(ElectricBill, bill_id)
    details = ElectricBillDetail.query.filter_by(electric_bill_id=bill_id).all()
    readings = ElectricReading.query.filter_by(electric_bill_id=bill_id).all()
    readings_map = {r.unit_id: r for r in readings}
    return render_template(
        "view_electric_detail.html", bill=bill, details=details, readings_map=readings_map
    )


@app.route("/view/water/<int:bill_id>")
def view_water_detail(bill_id):
    bill = db.get_or_404(WaterBill, bill_id)
    details = WaterBillDetail.query.filter_by(water_bill_id=bill_id).all()
    return render_template("view_water_detail.html", bill=bill, details=details)


@app.route("/view/common/<int:bill_id>")
def view_common_detail(bill_id):
    bill = db.get_or_404(CommonBill, bill_id)
    details = CommonBillDetail.query.filter_by(common_bill_id=bill_id).all()
    return render_template("view_common_detail.html", bill=bill, details=details)


_BILL_TYPES = {
    "electric": (ElectricBill, "electric_bill_id"),
    "water": (WaterBill, "water_bill_id"),
    "common": (CommonBill, "common_bill_id"),
}


@app.route("/bills/delete/<bill_type>/<int:bill_id>", methods=["POST"])
@csrf_protect
def delete_bill(bill_type, bill_id):
    try:
        if bill_type not in _BILL_TYPES:
            return json_error("잘못된 요청입니다.")
        model, fk_column = _BILL_TYPES[bill_type]
        bill = db.session.get(model, bill_id)
        if bill is None:
            return json_error("대상을 찾을 수 없습니다.", status=404)

        in_use = InvoiceCombinationItem.query.filter_by(**{fk_column: bill_id}).count()
        if in_use:
            return json_error(
                "이 항목은 발행된 정산서 {}건에 포함되어 있어 삭제할 수 없습니다. "
                "해당 정산서를 먼저 삭제하세요.".format(in_use)
            )

        db.session.delete(bill)
        db.session.commit()
        return jsonify({"success": True, "message": "삭제되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("참조 중인 데이터가 있어 삭제할 수 없습니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


# ======================================================
# Invoice
# ======================================================
@app.route("/invoice")
def invoice_combination():
    electric_bills = (
        ElectricBill.query.options(selectinload(ElectricBill.months))
        .order_by(ElectricBill.billing_month.desc())
        .all()
    )
    water_bills = WaterBill.query.order_by(WaterBill.billing_month.desc()).all()
    common_bills = CommonBill.query.order_by(
        CommonBill.billing_month.desc(), CommonBill.id.desc()
    ).all()
    combinations = InvoiceCombination.query.order_by(
        InvoiceCombination.created_at.desc()
    ).all()
    units = (
        Unit.query.filter_by(is_vacant=False).order_by(Unit.floor_id, Unit.unit_name).all()
    )
    floors = Floor.query.order_by(Floor.floor_number).all()

    units_json = [
        {
            "id": u.id,
            "floor_id": u.floor_id,
            "unit_name": u.unit_name,
            "memo": u.memo or "",
            "is_vacant": u.is_vacant,
        }
        for u in units
    ]

    floors_json = [
        {"id": f.id, "floor_number": f.floor_number, "name": f.name or ""} for f in floors
    ]

    return render_template(
        "invoice.html",
        electric_bills=electric_bills,
        water_bills=water_bills,
        common_bills=common_bills,
        combinations=combinations,
        units=units_json,
        floors=floors_json,
    )


_ITEM_FK_BY_TYPE = {
    "ELECTRIC": "electric_bill_id",
    "WATER": "water_bill_id",
    "COMMON": "common_bill_id",
}


@app.route("/invoice/create", methods=["POST"])
@csrf_protect
def create_invoice():
    """선택한 계산 결과를 조합해 세대별 청구서를 확정한다.

    변경점: 세대별 기타 항목이 ``final_invoices.additional_charges`` JSON 이 아니라
    ``final_invoice_charges`` 테이블에 저장되며, 프론트엔드가 보내는
    ``is_carryover`` 를 **버리지 않고 컬럼에 보존**한다.
    금액 계산식은 변경하지 않았다.
    """
    try:
        data = request.get_json(silent=True) or {}
        if not data.get("name"):
            return json_error("정산서 이름을 입력해주세요.")

        items = data.get("items", [])
        if not items:
            return json_error("최소 하나 이상의 항목을 선택해주세요.")

        default_memo = get_setting("invoice_default_memo")
        user_memo = data.get("memo", "")

        if default_memo and user_memo:
            combined_memo = "{}\n\n{}".format(default_memo, user_memo)
        elif default_memo:
            combined_memo = default_memo
        else:
            combined_memo = user_memo

        combination = InvoiceCombination(invoice_name=data["name"], memo=combined_memo)
        db.session.add(combination)
        db.session.flush()

        for item in items:
            item_type = item.get("type")
            if item_type not in _ITEM_FK_BY_TYPE:
                db.session.rollback()
                return json_error("알 수 없는 항목 유형입니다: {}".format(item_type))

            month = parse_month_input(item.get("month"))
            if month is None:
                db.session.rollback()
                return json_error("항목의 정산월을 해석할 수 없습니다.")

            item_data = {
                "combination_id": combination.id,
                "item_type": item_type,
                "billing_month": month,
                "item_description": item.get("description", ""),
                _ITEM_FK_BY_TYPE[item_type]: item["id"],
            }
            db.session.add(InvoiceCombinationItem(**item_data))

        unit_additional_data = data.get("unit_additional_data", {}) or {}

        units = Unit.query.filter_by(is_vacant=False).all()
        for unit in units:
            electric_total = dec(0)
            water_total = dec(0)
            common_total = dec(0)
            common_details_list = []

            for item in items:
                if item["type"] == "ELECTRIC":
                    d = ElectricBillDetail.query.filter_by(
                        electric_bill_id=item["id"], unit_id=unit.id
                    ).first()
                    if d:
                        electric_total += d.charged_amount
                elif item["type"] == "WATER":
                    d = WaterBillDetail.query.filter_by(
                        water_bill_id=item["id"], unit_id=unit.id
                    ).first()
                    if d:
                        water_total += d.charged_amount
                elif item["type"] == "COMMON":
                    d = CommonBillDetail.query.filter_by(
                        common_bill_id=item["id"], unit_id=unit.id
                    ).first()
                    if d:
                        common_total += d.charged_amount
                        common_details_list.append(
                            {
                                "description": item.get("description", "공동 공과금"),
                                "amount": d.charged_amount,
                            }
                        )

            unit_key = str(unit.id)
            unit_data = unit_additional_data.get(unit_key, {}) or {}
            charges_input = unit_data.get("charges", []) or []
            additional_total = dec(0)
            charge_rows = []

            for order, charge in enumerate(charges_input):
                charge_amount = dec(charge.get("amount", 0))
                additional_total += charge_amount
                charge_rows.append(
                    {
                        "description": (charge.get("description") or "").strip() or "(설명 없음)",
                        "amount": charge_amount,
                        # 프론트엔드가 보내는 명시적 플래그를 그대로 보존한다.
                        # 문자열 추론은 더 이상 사용하지 않는다.
                        "is_carryover": bool(charge.get("is_carryover", False)),
                        "sort_order": order,
                    }
                )

            total = electric_total + water_total + common_total + additional_total

            final_invoice = FinalInvoice(
                combination_id=combination.id,
                unit_id=unit.id,
                electric_amount=electric_total,
                water_amount=water_total,
                common_amount=common_total,
                common_details=common_details_list if common_details_list else None,
                total_amount=total,
                unit_memo=unit_data.get("memo", ""),
            )
            db.session.add(final_invoice)
            db.session.flush()

            for row in charge_rows:
                db.session.add(
                    FinalInvoiceCharge(final_invoice_id=final_invoice.id, **row)
                )

        db.session.commit()
        return jsonify(
            {"success": True, "message": "청구서가 생성되었습니다.", "id": combination.id}
        )
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


def _load_combination(combination_id):
    return (
        db.session.query(InvoiceCombination)
        .options(
            joinedload(InvoiceCombination.items)
            .joinedload(InvoiceCombinationItem.electric_bill_ref)
            .joinedload(ElectricBill.floor_ref),
            joinedload(InvoiceCombination.items)
            .joinedload(InvoiceCombinationItem.electric_bill_ref)
            .selectinload(ElectricBill.months),
        )
        .filter_by(id=combination_id)
        .first_or_404()
    )


def _load_invoices(combination_id):
    return (
        db.session.query(FinalInvoice)
        .options(
            joinedload(FinalInvoice.unit), selectinload(FinalInvoice.charges)
        )
        .filter_by(combination_id=combination_id)
        .order_by(FinalInvoice.unit_id)
        .all()
    )


@app.route("/invoice/view/<int:combination_id>")
def view_invoice(combination_id):
    return render_template(
        "invoice_view.html",
        combination=_load_combination(combination_id),
        invoices=_load_invoices(combination_id),
    )


@app.route("/invoice/print/<int:combination_id>")
def print_invoice(combination_id):
    return render_template(
        "invoice_print.html",
        combination=_load_combination(combination_id),
        invoices=_load_invoices(combination_id),
        invoice_footer=get_setting("invoice_footer"),
    )


@app.route("/invoice/delete/<int:combination_id>", methods=["POST"])
@csrf_protect
def delete_invoice(combination_id):
    try:
        combination = db.session.get(InvoiceCombination, combination_id)
        if combination is None:
            return json_error("정산서를 찾을 수 없습니다.", status=404)

        payment_count = Payment.query.filter_by(combination_id=combination_id).count()
        if payment_count:
            return json_error(
                "이 정산서에는 납부 내역 {}건이 등록되어 있어 삭제할 수 없습니다. "
                "납부 내역을 먼저 삭제하세요.".format(payment_count)
            )

        db.session.delete(combination)
        db.session.commit()
        return jsonify({"success": True, "message": "정산서가 삭제되었습니다."})
    except IntegrityError:
        db.session.rollback()
        return json_error("참조 중인 데이터가 있어 삭제할 수 없습니다.")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/get_previous_readings/<int:floor_id>/<billing_month>")
def get_previous_readings(floor_id, billing_month):
    try:
        current_month = parse_month_input(billing_month)
        if current_month is None:
            return json_error("정산월 형식이 올바르지 않습니다.")
        prev_bill = (
            ElectricBill.query.filter(
                ElectricBill.floor_id == floor_id,
                ElectricBill.billing_month < current_month,
            )
            .order_by(ElectricBill.billing_month.desc())
            .first()
        )

        readings = {}
        if prev_bill:
            for r in prev_bill.readings:
                readings[r.unit_id] = float(r.current_reading)
        return jsonify({"success": True, "readings": readings, "found": prev_bill is not None})
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


# ======================================================
# Payment Management (세대 중심 통합 관리)
# ======================================================
@app.route("/payments")
def payments():
    units = (
        Unit.query.filter_by(is_vacant=False).order_by(Unit.floor_id, Unit.unit_name).all()
    )
    combinations = InvoiceCombination.query.order_by(
        InvoiceCombination.created_at.desc()
    ).all()
    return render_template("payments.html", units=units, combinations=combinations)


@app.route("/payments/unit_history/<int:unit_id>")
def payment_unit_history(unit_id):
    """세대의 전체 정산 및 납부 이력 (이월 항목 제외 계산)."""
    try:
        unit = db.session.get(Unit, unit_id)
        if unit is None:
            return json_error("세대를 찾을 수 없습니다.", status=404)
        return jsonify(
            {
                "success": True,
                "unit": {
                    "id": unit.id,
                    "name": unit.unit_name,
                    "floor": unit.floor.name if unit.floor else "",
                },
                "history": balance_service.unit_history(unit_id),
            }
        )
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


@app.route("/payments/balance/<int:unit_id>")
def payment_balance(unit_id):
    """세대의 누적 미납/초과 금액 (이월 항목 제외)."""
    try:
        result = balance_service.unit_balance(unit_id)
        result["success"] = True
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


@app.route("/payments/add", methods=["POST"])
@csrf_protect
def add_payment():
    try:
        data = request.get_json(silent=True) or {}
        payment = Payment(
            combination_id=data["combination_id"],
            unit_id=data["unit_id"],
            payment_date=datetime.strptime(data["payment_date"], "%Y-%m-%d").date(),
            payment_amount=dec(data["payment_amount"]),
            payment_method=data.get("payment_method", "계좌이체"),
            memo=data.get("memo", ""),
        )
        db.session.add(payment)
        db.session.commit()
        return jsonify(
            {"success": True, "message": "납부 내역이 추가되었습니다.", "id": payment.id}
        )
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/payments/update/<int:payment_id>", methods=["POST"])
@csrf_protect
def update_payment(payment_id):
    try:
        payment = db.session.get(Payment, payment_id)
        if payment is None:
            return json_error("납부 내역을 찾을 수 없습니다.", status=404)
        data = request.get_json(silent=True) or {}

        payment.payment_date = datetime.strptime(data["payment_date"], "%Y-%m-%d").date()
        payment.payment_amount = dec(data["payment_amount"])
        payment.payment_method = data.get("payment_method", "계좌이체")
        payment.memo = data.get("memo", "")

        db.session.commit()
        return jsonify({"success": True, "message": "납부 내역이 수정되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/payments/delete/<int:payment_id>", methods=["POST"])
@csrf_protect
def delete_payment(payment_id):
    try:
        payment = db.session.get(Payment, payment_id)
        if payment is None:
            return json_error("납부 내역을 찾을 수 없습니다.", status=404)
        db.session.delete(payment)
        db.session.commit()
        return jsonify({"success": True, "message": "납부 내역이 삭제되었습니다."})
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/payments/all_units_balance")
def all_units_balance():
    """전체 세대의 누적 잔액 (정산서 작성 시 사용, 잔액 0인 세대는 생략)."""
    try:
        balances = balance_service.all_unit_balances()
        result = {
            str(unit_id): {
                "unit_name": info["unit_name"],
                "floor_name": info["floor_name"],
                "balance": info["balance"],
            }
            for unit_id, info in balances.items()
            if info["balance"] != 0
        }
        return jsonify({"success": True, "balances": result})
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


@app.route("/admin/validate_balances")
def validate_balances():
    """전체 세대의 잔액 정합성 검증 (관리자용)."""
    try:
        units = (
            Unit.query.filter_by(is_vacant=False)
            .order_by(Unit.floor_id, Unit.unit_name)
            .all()
        )
        balances = balance_service.all_unit_balances(units)
        report = [
            {
                "unit_id": unit.id,
                "unit_name": balances[unit.id]["unit_name"],
                "floor_name": balances[unit.id]["floor_name"],
                "total_billed": balances[unit.id]["total_billed"],
                "total_paid": balances[unit.id]["total_paid"],
                "balance": balances[unit.id]["balance"],
                "carryover_total": balances[unit.id]["carryover_total"],
                "invoice_count": balances[unit.id]["invoice_count"],
            }
            for unit in units
        ]
        return jsonify({"success": True, "report": report})
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"], host=app.config["HOST"], port=app.config["PORT"])
