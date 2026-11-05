"""공과금 정산 시스템 — Flask 애플리케이션.

DB 계층은 SQLite 기반이며 스키마는 ``models.py`` + ``migrations/`` 가 정의한다.
MySQL 의존성과 하드코딩된 자격증명은 제거되었다.

실행::

    flask db upgrade        # 스키마 생성/갱신
    flask seed-settings     # 설정 기본값 시딩 (멱등)
    python app.py

배분 계산은 Decimal 기반으로 처리하며, 사용자 입력은 저장 전에 검증한다.
"""

import json
import hashlib
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
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import balance as balance_service
from backup_format import (
    BackupFormatError,
    current_counts,
    export_payload,
    replace_all,
    validate_payload,
    validate_sqlite_backup,
)
from calculation_service import (
    AllocationError,
    allocate_common,
    allocate_electric,
    allocate_water,
    round_up_to_10 as allocation_round_up_to_10,
)
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


def parse_amount(value, label, *, allow_negative=False, default=None):
    """사용자 금액 입력을 검증한다. 생략 가능한 필드는 default 를 지정한다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValueError("{}을(를) 입력해주세요.".format(label))
        return Decimal(str(default))
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError("{}이(가) 올바른 숫자가 아닙니다.".format(label))
    if not amount.is_finite():
        raise ValueError("{}이(가) 올바른 숫자가 아닙니다.".format(label))
    if amount < 0 and not allow_negative:
        raise ValueError("{}은(는) 음수일 수 없습니다.".format(label))
    return amount


def parse_json_bool(value, label):
    if not isinstance(value, bool):
        raise ValueError("{}은(는) true 또는 false여야 합니다.".format(label))
    return value


def to_int(val, default=0):
    try:
        s = (val if val is not None else "").strip()
    except (AttributeError, TypeError):
        s = str(val or "")
    if s == "":
        return default
    try:
        number = Decimal(s.replace(",", ""))
        if not number.is_finite() or number != number.to_integral_value():
            return default
        return int(number)
    except (InvalidOperation, TypeError, ValueError):
        return default


def parse_nonnegative_int(value, label):
    number = to_int(value, None)
    if number is None or number < 0:
        raise ValueError("{}은(는) 0 이상의 정수여야 합니다.".format(label))
    return number


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
    return allocation_round_up_to_10(amount)


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


def _serialize_allocation_preview(allocation):
    """순수 배분 결과를 화면에서 공통으로 표시할 수 있는 형태로 바꾼다."""
    details = []
    for row in allocation["details"]:
        unit = row["unit"]
        details.append({
            "unit_id": unit.id,
            "unit_name": unit.unit_name,
            "floor_name": unit.floor.name if unit.floor else "",
            "usage_amount": row.get("usage_amount"),
            "base_amount": row.get("base_amount", row.get("amount", 0)),
            "welfare_discount": row.get("welfare_discount", 0),
            "voucher_discount": row.get("voucher_discount", 0),
            "tv_fee": row.get("tv_fee", 0),
            "is_excluded": row.get("is_excluded", False),
            "final_amount": row["final_amount"],
            "charged_amount": row["charged_amount"],
        })
    return {
        "details": details,
        "warnings": allocation["warnings"],
        "final_total": allocation["final_total"],
        "charged_total": allocation["charged_total"],
        "rounding_difference": allocation["rounding_difference"],
    }


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
    """설정과 회계 이력을 포함한 전체 JSON 백업을 내보낸다."""
    return jsonify(export_payload())


def _create_safety_backup(reason):
    """위험한 변경 직전 현재 SQLite DB의 검증된 복원 지점을 만든다."""
    from config import resolve_db_path
    from db_backup import backup_database

    database_path = resolve_db_path()
    if database_path is None:
        raise ValueError("현재 DB 연결 방식에서는 자동 파일 백업을 지원하지 않습니다.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = database_path.parent / "backups" / "{}_{}.db".format(reason, stamp)
    return backup_database(output=output)


def _apply_full_restore(validated, backup_reason):
    """검증된 전체 백업을 자동 백업·정합성 검사와 함께 적용한다."""
    backup_path = _create_safety_backup(backup_reason)
    replace_all(validated["decoded"])

    from db_validate import run_all_checks
    failed = [check for check in run_all_checks() if not check.ok]
    if failed:
        raise BackupFormatError(
            "복원 후 정합성 검사 실패: {}".format(", ".join(check.name for check in failed))
        )
    db.session.commit()
    db.session.expire_all()
    return backup_path


_BACKUP_REASON_LABELS = {
    "before_full_restore": "전체 백업 복원 전",
    "before_automatic_restore": "자동 백업으로 되돌리기 전",
    "before_floor_delete": "층 삭제 전",
    "before_unit_delete": "세대 삭제 전",
    "before_electric_overwrite": "전기요금 덮어쓰기 전",
    "before_water_overwrite": "수도요금 덮어쓰기 전",
    "before_electric_delete": "전기요금 삭제 전",
    "before_water_delete": "수도요금 삭제 전",
    "before_common_delete": "공동 공과금 삭제 전",
    "before_invoice_delete": "정산서 삭제 전",
    "before_payment_delete": "입금 내역 삭제 전",
}


@app.route("/settings/automatic-backups")
def automatic_backups():
    """설정 화면에서 선택할 수 있는 최근 자동 복원 지점을 반환한다."""
    from db_backup import list_automatic_backups

    try:
        return jsonify({
            "success": True,
            "backups": [
                {
                    "filename": item["filename"],
                    "reason": item["reason"],
                    "reason_label": _BACKUP_REASON_LABELS.get(item["reason"], "위험 작업 전"),
                    "created_at": item["created_at"].isoformat(timespec="seconds"),
                    "size": item["size"],
                }
                for item in list_automatic_backups()
            ],
        })
    except Exception as exc:  # noqa: BLE001
        return json_error("자동 백업 목록을 읽지 못했습니다: {}".format(exc))


@app.route("/settings/automatic-backups/preview", methods=["POST"])
@csrf_protect
def preview_automatic_backup():
    """선택한 자동 백업을 적용하지 않고 검증한다."""
    from db_backup import resolve_automatic_backup

    try:
        data = request.get_json(silent=True) or {}
        path = resolve_automatic_backup(data.get("filename"))
        validated = validate_sqlite_backup(path)
        return jsonify({
            "success": True,
            "message": "자동 백업 검사가 완료되었습니다.",
            "current_counts": current_counts(),
            "backup_counts": validated["counts"],
            "backup_created_at": validated["created_at"],
        })
    except Exception as exc:  # noqa: BLE001
        return json_error(str(exc))


@app.route("/settings/automatic-backups/restore", methods=["POST"])
@csrf_protect
def restore_automatic_backup():
    """자동 백업의 전체 데이터로 현재 상태를 교체한다."""
    from db_backup import resolve_automatic_backup

    data = request.get_json(silent=True) or {}
    if data.get("confirm_full_replace") is not True:
        return json_error("전체 교체 확인이 필요합니다.")
    new_backup = None
    try:
        path = resolve_automatic_backup(data.get("filename"))
        validated = validate_sqlite_backup(path)
        new_backup = _apply_full_restore(validated, "before_automatic_restore")
        return jsonify({
            "success": True,
            "message": "선택한 자동 복원 지점으로 되돌렸습니다.",
            "restored_counts": validated["counts"],
            "automatic_backup": str(new_backup),
        })
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return json_error(
            "자동 백업 복원 실패: {}".format(exc),
            automatic_backup=str(new_backup) if new_backup else None,
        )


@app.route("/settings/import/preview", methods=["POST"])
@csrf_protect
def preview_import_settings():
    """전체 복원 파일을 실제 DB와 분리된 임시 DB에서 검증한다."""
    try:
        payload = request.get_json(silent=True)
        validated = validate_payload(payload)
        return jsonify({
            "success": True,
            "message": "전체 백업 검증이 완료되었습니다.",
            "current_counts": current_counts(),
            "backup_counts": validated["counts"],
            "backup_created_at": validated["created_at"],
        })
    except BackupFormatError as exc:
        return json_error(str(exc))


@app.route("/settings/import", methods=["POST"])
@csrf_protect
def import_settings():
    """현재 데이터 전체를 검증된 백업 파일의 내용으로 교체한다."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("JSON 본문을 해석할 수 없습니다.")
    if data.get("confirm_full_replace") is not True:
        return json_error("전체 교체 확인이 필요합니다.")

    backup_path = None
    try:
        validated = validate_payload(data)
        backup_path = _apply_full_restore(validated, "before_full_restore")
        return jsonify({
            "success": True,
            "message": "전체 백업으로 복원했습니다.",
            "restored_counts": validated["counts"],
            "automatic_backup": str(backup_path),
        })
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(
            "전체 복원 실패: {}".format(e),
            automatic_backup=str(backup_path) if backup_path else None,
        )


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

        _create_safety_backup("before_floor_delete")
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
            residents_count=parse_nonnegative_int(
                request.form.get("residents_count", "1"), "거주 인원"
            ),
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
        unit.residents_count = parse_nonnegative_int(
            request.form.get("residents_count", unit.residents_count), "거주 인원"
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

        _create_safety_backup("before_unit_delete")
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
    """층 단위 전기요금을 미리보기와 저장에 동일한 엔진으로 배분한다."""
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        floor_id = to_int(request.form.get("floor_id"), 0)
        if not floor_id:
            return json_error("층을 선택해주세요.")
        tv_distribution_mode = request.form.get("tv_distribution_mode", "INDIVIDUAL")
        if tv_distribution_mode not in ("INDIVIDUAL", "EQUAL"):
            return json_error("TV 수신료 분배 방식이 올바르지 않습니다.")

        monthly_details = []
        total_amount = dec(0)
        welfare_discount_input = dec(0)
        voucher_discount_input = dec(0)
        tv_fee_total = dec(0)

        # FormData 에서 bill_month_ 로 시작하는 키를 찾아 동적 rowId 를 추출한다.
        bill_months = {}
        for key in request.form.keys():
            if key.startswith("bill_month_"):
                row_id = key.replace("bill_month_", "")
                bill_months[row_id] = {
                    "month": request.form.get("bill_month_{}".format(row_id)),
                    "amount": parse_amount(request.form.get("bill_amount_{}".format(row_id)), "고지 금액"),
                    "welfare": parse_amount(request.form.get("bill_welfare_{}".format(row_id)), "복지 할인", default=0),
                    "voucher": parse_amount(request.form.get("bill_voucher_{}".format(row_id)), "바우처 할인", default=0),
                    "tv_fee": parse_amount(request.form.get("bill_tv_fee_{}".format(row_id)), "TV 수신료", default=0),
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
        if not monthly_details:
            return json_error("월별 고지 내역을 하나 이상 입력해주세요.")
        month_count = len(monthly_details)

        floor = db.session.get(Floor, floor_id)
        if floor is None:
            return json_error("선택한 층을 찾을 수 없습니다.")

        units = [u for u in floor.units if not u.is_vacant]
        if not units:
            raise AllocationError("전기요금을 배분할 재실 세대가 없습니다.")
        reading_values = {}
        for unit in units:
            prev_reading = parse_amount(request.form.get("prev_{}".format(unit.id)), "이전 검침값")
            curr_reading = parse_amount(request.form.get("curr_{}".format(unit.id)), "현재 검침값")
            reading_values[unit.id] = (prev_reading, curr_reading)

        allocation = allocate_electric(
            units,
            reading_values,
            total_amount=total_amount,
            welfare_discount_input=welfare_discount_input,
            voucher_discount_input=voucher_discount_input,
            tv_fee_total=tv_fee_total,
            tv_distribution_mode=tv_distribution_mode,
            month_count=month_count,
            configured_tv_fee=get_setting("tv_fee") or "2500",
            configured_welfare_discount=get_setting("electric_welfare_amount"),
            configured_voucher_discount=get_setting("electric_voucher_amount"),
        )
        existing = ElectricBill.query.filter_by(
            billing_month=billing_month, floor_id=floor_id
        ).first()
        if request.form.get("preview") == "true":
            return jsonify({
                "success": True,
                "preview": _serialize_allocation_preview(allocation),
                "existing": bool(existing),
                "message": "저장 전 배분 결과입니다.",
            })
        if existing and request.form.get("overwrite") != "true":
            return jsonify({
                "success": False, "exists": True, "already_exists": True,
                "message": "해당 월의 전기요금이 이미 존재합니다.",
            })
        if existing:
            in_use = _electric_bill_in_use(existing.id)
            if in_use:
                return json_error(
                    "이 전기요금은 이미 발행된 정산서 {}건에 포함되어 있어 다시 계산할 수 없습니다. "
                    "해당 정산서를 먼저 삭제하세요.".format(in_use)
                )
            _create_safety_backup("before_electric_overwrite")
            db.session.delete(existing)
            db.session.flush()

        bill = ElectricBill(
            billing_month=billing_month, floor_id=floor_id, total_amount=total_amount,
            welfare_discount=allocation["welfare_discount_total"],
            voucher_discount=allocation["voucher_discount_total"],
            tv_fee_total=tv_fee_total, tv_distribution_mode=tv_distribution_mode,
            billing_months_count=len(monthly_details),
        )
        db.session.add(bill)
        db.session.flush()
        for month_data in monthly_details:
            db.session.add(ElectricBillMonth(
                electric_bill_id=bill.id, billing_month=month_data["parsed_month"],
                amount=month_data["amount"], welfare_discount=month_data["welfare"],
                voucher_discount=month_data["voucher"], tv_fee=month_data["tv_fee"],
            ))
        for unit in units:
            previous, current = reading_values[unit.id]
            db.session.add(ElectricReading(
                electric_bill_id=bill.id, unit_id=unit.id,
                previous_reading=previous, current_reading=current,
            ))
        for row in allocation["details"]:
            unit = row["unit"]
            db.session.add(
                ElectricBillDetail(
                    electric_bill_id=bill.id,
                    unit_id=unit.id,
                    usage_amount=row["usage_amount"], base_amount=row["base_amount"],
                    welfare_discount=row["welfare_discount"],
                    voucher_discount=row["voucher_discount"], tv_fee=row["tv_fee"],
                    final_amount=row["final_amount"], charged_amount=row["charged_amount"],
                    unit_snapshot=create_unit_snapshot(unit),
                )
            )
        db.session.commit()
        return jsonify({
            "success": True, "message": "전기요금이 계산되었습니다.",
            "warnings": allocation["warnings"],
        })
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


def _water_bill_in_use(bill_id):
    return InvoiceCombinationItem.query.filter_by(water_bill_id=bill_id).count()


@app.route("/calculate/water", methods=["POST"])
@csrf_protect
def calculate_water():
    """주택 전체 수도요금을 미리보기와 저장에 동일한 엔진으로 배분한다."""
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        total_amount = parse_amount(request.form.get("total_amount"), "수도요금")
        welfare_discount_input = parse_amount(
            request.form.get("welfare_discount_total"), "수도 복지 할인", default=0
        )

        excluded_units_json = request.form.get("excluded_units", "[]")
        try:
            excluded_values = json.loads(excluded_units_json)
        except (ValueError, TypeError):
            return json_error("제외 세대 목록이 올바르지 않습니다.")
        if not isinstance(excluded_values, list) or any(
            type(value) is not int or value <= 0 for value in excluded_values
        ):
            return json_error("제외 세대 목록이 올바르지 않습니다.")
        excluded_unit_ids = set(excluded_values)

        all_units = Unit.query.filter_by(is_vacant=False).all()
        if not all_units:
            raise AllocationError("수도요금을 배분할 재실 세대가 없습니다.")
        unknown_exclusions = excluded_unit_ids - {u.id for u in all_units}
        if unknown_exclusions:
            raise ValueError("제외 세대 목록에 존재하지 않는 재실 세대가 있습니다.")
        included_units = [u for u in all_units if u.id not in excluded_unit_ids]
        allocation = allocate_water(
            all_units, included_units, total_amount=total_amount,
            welfare_discount_input=welfare_discount_input,
            configured_welfare_discount=get_setting("water_welfare_amount"),
        )
        existing = WaterBill.query.filter_by(billing_month=billing_month).first()
        if request.form.get("preview") == "true":
            return jsonify({
                "success": True,
                "preview": _serialize_allocation_preview(allocation),
                "existing": bool(existing),
                "message": "저장 전 배분 결과입니다.",
            })
        if existing and request.form.get("overwrite") != "true":
            return jsonify({
                "success": False, "exists": True, "already_exists": True,
                "message": "해당 월의 수도요금이 이미 존재합니다.",
            })
        if existing:
            in_use = _water_bill_in_use(existing.id)
            if in_use:
                return json_error(
                    "이 수도요금은 이미 발행된 정산서 {}건에 포함되어 있어 다시 계산할 수 없습니다. "
                    "해당 정산서를 먼저 삭제하세요.".format(in_use)
                )
            _create_safety_backup("before_water_overwrite")
            db.session.delete(existing)
            db.session.flush()

        bill = WaterBill(
            billing_month=billing_month, total_amount=total_amount,
            welfare_discount_total=allocation["welfare_discount_total"],
        )
        db.session.add(bill)
        db.session.flush()
        for row in allocation["details"]:
            unit = row["unit"]
            db.session.add(WaterBillDetail(
                water_bill_id=bill.id, unit_id=unit.id,
                base_amount=row["base_amount"],
                welfare_discount=row["welfare_discount"],
                final_amount=row["final_amount"], charged_amount=row["charged_amount"],
                unit_snapshot=create_unit_snapshot(unit), is_excluded=row["is_excluded"],
            ))

        db.session.commit()
        return jsonify({
            "success": True, "message": "수도요금이 계산되었습니다.",
            "warnings": allocation["warnings"],
        })
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return json_error(str(e))


@app.route("/calculate/common", methods=["POST"])
@csrf_protect
def calculate_common():
    """공동 공과금을 검증하고 중복 요청을 방지해 배분한다."""
    try:
        billing_month = parse_month_input(request.form.get("billing_month"))
        if billing_month is None:
            return json_error("정산월을 올바르게 입력해주세요.")

        if "total_amount" in request.form:
            distribution_method = request.form.get("distribution_method", "BY_RESIDENTS")
            bill_inputs = [(
                (request.form.get("description") or "").strip(),
                parse_amount(request.form.get("total_amount"), "공동 공과금"),
            )]
        else:
            distribution_method = {"RESIDENTS": "BY_RESIDENTS", "UNIT": "BY_UNITS"}.get(
                request.form.get("distribution_mode")
            )
            bill_inputs = [
                (label, parse_amount(request.form.get(field), label, default=0))
                for label, field in (
                    ("인터넷", "internet_amount"),
                    ("관리비", "management_amount"),
                    ("기타", "other_amount"),
                )
            ]
            bill_inputs = [(label, amount) for label, amount in bill_inputs if amount > 0]
            if not bill_inputs:
                return json_error("공동 공과금 금액을 하나 이상 입력해주세요.")

        if distribution_method not in ("BY_RESIDENTS", "BY_UNITS"):
            return json_error("분배 방식이 올바르지 않습니다.")
        if any(amount <= 0 for _, amount in bill_inputs):
            return json_error("공동 공과금은 0원보다 커야 합니다.")

        units = Unit.query.filter_by(is_vacant=False).all()
        if not units:
            return json_error("배분할 재실 세대가 없습니다.")

        request_token = (request.form.get("request_token") or "").strip()[:80]
        token_keys = ["{}:{}".format(request_token, index) for index in range(len(bill_inputs))] if request_token else []
        if token_keys:
            completed = CommonBill.query.filter(CommonBill.request_token.in_(token_keys)).all()
            if len(completed) == len(token_keys):
                return jsonify({
                    "success": True,
                    "message": "이미 처리된 같은 저장 요청입니다. 중복 저장하지 않았습니다.",
                    "idempotent": True,
                })

        allocations = [
            allocate_common(units, total_amount=amount, distribution_method=distribution_method)
            for _, amount in bill_inputs
        ]
        duplicates = []
        for description, _ in bill_inputs:
            count = CommonBill.query.filter_by(
                billing_month=billing_month, description=description
            ).count()
            if count:
                duplicates.append({"description": description or "(설명 없음)", "count": count})

        if request.form.get("preview") == "true":
            return jsonify({
                "success": True,
                "message": "저장 전 배분 결과입니다.",
                "duplicates": duplicates,
                "items": [
                    {
                        "description": description or "(설명 없음)",
                        "total_amount": amount,
                        "preview": _serialize_allocation_preview(allocation),
                    }
                    for (description, amount), allocation in zip(bill_inputs, allocations)
                ],
            })
        if duplicates and request.form.get("duplicate_action") != "add":
            return json_error(
                "같은 정산월과 항목명의 공동 공과금이 이미 있습니다. 미리보기에서 추가 저장을 확인해주세요.",
                duplicate_requires_confirmation=True,
                duplicates=duplicates,
            )

        for index, ((description, total_amount), allocation) in enumerate(zip(bill_inputs, allocations)):
            bill = CommonBill(
                billing_month=billing_month,
                description=description,
                total_amount=total_amount,
                distribution_method=distribution_method,
                request_token=token_keys[index] if token_keys else None,
            )
            db.session.add(bill)
            db.session.flush()
            for row in allocation["details"]:
                unit = row["unit"]
                db.session.add(
                    CommonBillDetail(
                        common_bill_id=bill.id,
                        unit_id=unit.id,
                        amount=row["amount"],
                        charged_amount=row["charged_amount"],
                        unit_snapshot=create_unit_snapshot(unit),
                    )
                )

        db.session.commit()
        warnings = [warning for allocation in allocations for warning in allocation["warnings"]]
        return jsonify({
            "success": True,
            "message": "공동 공과금 {}건이 계산되었습니다.".format(len(bill_inputs)),
            "warnings": warnings,
        })
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

        _create_safety_backup("before_{}_delete".format(bill_type))
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
        .filter(~ElectricBill.invoice_items.any())
        .order_by(ElectricBill.billing_month.desc())
        .all()
    )
    water_bills = WaterBill.query.filter(~WaterBill.invoice_items.any()).order_by(
        WaterBill.billing_month.desc()
    ).all()
    common_bills = CommonBill.query.order_by(
        CommonBill.billing_month.desc(), CommonBill.id.desc()
    ).filter(~CommonBill.invoice_items.any()).all()
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

_BILL_MODEL_BY_TYPE = {
    "ELECTRIC": ElectricBill,
    "WATER": WaterBill,
    "COMMON": CommonBill,
}


def _normalize_invoice_items(items):
    if not isinstance(items, list) or not items:
        raise ValueError("최소 하나 이상의 항목을 선택해주세요.")
    selected_refs = set()
    normalized = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in _ITEM_FK_BY_TYPE:
            raise ValueError("알 수 없는 항목 유형입니다.")
        item_type = item["type"]
        try:
            bill_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("항목 ID가 올바르지 않습니다.")
        bill = db.session.get(_BILL_MODEL_BY_TYPE[item_type], bill_id)
        if bill is None:
            raise ValueError("선택한 고지 항목을 찾을 수 없습니다.")
        month = parse_month_input(item.get("month"))
        if month is None or month != bill.billing_month:
            raise ValueError("항목의 정산월이 원본 고지월과 일치하지 않습니다.")
        ref = (item_type, bill_id)
        if ref in selected_refs:
            raise ValueError("같은 고지 항목을 한 정산서에 두 번 넣을 수 없습니다.")
        selected_refs.add(ref)
        if InvoiceCombinationItem.query.filter_by(
            **{_ITEM_FK_BY_TYPE[item_type]: bill_id}
        ).first():
            raise ValueError("이미 다른 정산서에 포함된 고지 항목입니다.")
        normalized.append({
            "type": item_type,
            "id": bill_id,
            "month": month,
            "description": (item.get("description") or "").strip(),
        })
    return normalized


def _invoice_units(normalized_items):
    unit_ids = {u.id for u in Unit.query.filter_by(is_vacant=False).all()}
    for item in normalized_items:
        detail_model, bill_fk = {
            "ELECTRIC": (ElectricBillDetail, ElectricBillDetail.electric_bill_id),
            "WATER": (WaterBillDetail, WaterBillDetail.water_bill_id),
            "COMMON": (CommonBillDetail, CommonBillDetail.common_bill_id),
        }[item["type"]]
        unit_ids.update(
            unit_id for (unit_id,) in db.session.query(detail_model.unit_id)
            .filter(bill_fk == item["id"]).all()
        )
    units = Unit.query.filter(Unit.id.in_(unit_ids)).options(joinedload(Unit.floor)).all()
    if not units:
        raise ValueError("정산서를 발행할 세대가 없습니다.")
    return sorted(units, key=lambda unit: (unit.floor.floor_number, unit.unit_name, unit.id))


def _calculate_invoice_rows(normalized_items, unit_additional_data):
    if not isinstance(unit_additional_data, dict):
        raise ValueError("세대별 기타 항목 형식이 올바르지 않습니다.")
    rows = []
    for unit in _invoice_units(normalized_items):
        electric_total = dec(0)
        water_total = dec(0)
        common_total = dec(0)
        common_details = []
        for item in normalized_items:
            if item["type"] == "ELECTRIC":
                detail = ElectricBillDetail.query.filter_by(
                    electric_bill_id=item["id"], unit_id=unit.id
                ).first()
                if detail:
                    electric_total += detail.charged_amount
            elif item["type"] == "WATER":
                detail = WaterBillDetail.query.filter_by(
                    water_bill_id=item["id"], unit_id=unit.id
                ).first()
                if detail:
                    water_total += detail.charged_amount
            else:
                detail = CommonBillDetail.query.filter_by(
                    common_bill_id=item["id"], unit_id=unit.id
                ).first()
                if detail:
                    common_total += detail.charged_amount
                    common_details.append({
                        "description": item["description"] or "공동 공과금",
                        "amount": detail.charged_amount,
                    })

        unit_data = unit_additional_data.get(str(unit.id), {}) or {}
        if not isinstance(unit_data, dict):
            raise ValueError("{}의 기타 항목 형식이 올바르지 않습니다.".format(unit.unit_name))
        charges_input = unit_data.get("charges", []) or []
        if not isinstance(charges_input, list):
            raise ValueError("{}의 기타 항목 목록이 올바르지 않습니다.".format(unit.unit_name))
        charges = []
        additional_total = dec(0)
        for order, charge in enumerate(charges_input):
            if not isinstance(charge, dict):
                raise ValueError("{}의 기타 항목이 올바르지 않습니다.".format(unit.unit_name))
            amount = parse_amount(charge.get("amount"), "기타 항목 금액", allow_negative=True)
            additional_total += amount
            charges.append({
                "description": (charge.get("description") or "").strip() or "(설명 없음)",
                "amount": amount,
                "is_carryover": parse_json_bool(charge.get("is_carryover", False), "이월 여부"),
                "sort_order": order,
            })
        rows.append({
            "unit": unit,
            "electric_amount": electric_total,
            "water_amount": water_total,
            "common_amount": common_total,
            "common_details": common_details,
            "charges": charges,
            "additional_amount": additional_total,
            "total_amount": electric_total + water_total + common_total + additional_total,
            "unit_memo": unit_data.get("memo", ""),
        })
    return rows


def _invoice_payload_digest(data):
    canonical = {
        "name": data.get("name"),
        "memo": data.get("memo", ""),
        "items": data.get("items", []),
        "unit_additional_data": data.get("unit_additional_data", {}) or {},
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invoice_source_fingerprint(normalized_items):
    sources = []
    for item in normalized_items:
        bill = db.session.get(_BILL_MODEL_BY_TYPE[item["type"]], item["id"])
        sources.append({
            "type": item["type"], "id": item["id"],
            "updated_at": bill.updated_at.isoformat() if bill.updated_at else None,
            "detail_count": len(bill.details),
            "charged_total": sum(detail.charged_amount for detail in bill.details),
        })
    sources.append({"invoice_default_memo": get_setting("invoice_default_memo")})
    encoded = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invoice_preview_response(data):
    if not data.get("name"):
        raise ValueError("정산서 이름을 입력해주세요.")
    normalized = _normalize_invoice_items(data.get("items", []))
    rows = _calculate_invoice_rows(normalized, data.get("unit_additional_data", {}) or {})
    token = URLSafeTimedSerializer(app.secret_key, salt="invoice-preview-v1").dumps({
        "payload": _invoice_payload_digest(data),
        "sources": _invoice_source_fingerprint(normalized),
    })
    return normalized, rows, token


def _unit_floor_display(unit):
    number = unit.floor.floor_number
    label = "B{}층".format(-number) if number < 0 else "{}층".format(number)
    return "{} ({})".format(label, unit.floor.name) if unit.floor.name else label


@app.route("/invoice/preview", methods=["POST"])
@csrf_protect
def preview_invoice():
    try:
        data = request.get_json(silent=True) or {}
        normalized, rows, token = _invoice_preview_response(data)
        return jsonify({
            "success": True,
            "preview_token": token,
            "items": [
                {
                    "type": item["type"], "id": item["id"],
                    "month": item["month"].strftime("%Y-%m"),
                    "description": item["description"],
                }
                for item in normalized
            ],
            "units": [
                {
                    "unit_id": row["unit"].id,
                    "unit_name": row["unit"].unit_name,
                    "floor_name": _unit_floor_display(row["unit"]),
                    "electric_amount": row["electric_amount"],
                    "water_amount": row["water_amount"],
                    "common_amount": row["common_amount"],
                    "additional_amount": row["additional_amount"],
                    "total_amount": row["total_amount"],
                }
                for row in rows
            ],
            "grand_total": sum(row["total_amount"] for row in rows),
        })
    except Exception as exc:  # noqa: BLE001
        return json_error(str(exc))


@app.route("/invoice/create", methods=["POST"])
@csrf_protect
def create_invoice():
    """서명된 최종 미리보기와 동일한 내용으로 세대별 청구서를 확정한다."""
    try:
        data = request.get_json(silent=True) or {}
        preview_token = data.get("preview_token")
        if not preview_token:
            return json_error("저장 전에 최종 금액 미리보기를 확인해주세요.")
        normalized_items, rows, _fresh_token = _invoice_preview_response(data)
        try:
            signed = URLSafeTimedSerializer(app.secret_key, salt="invoice-preview-v1").loads(
                preview_token, max_age=600
            )
        except SignatureExpired:
            return json_error("최종 확인이 만료되었습니다. 금액을 다시 확인해주세요.")
        except BadSignature:
            return json_error("최종 확인 정보가 올바르지 않습니다. 다시 확인해주세요.")
        if signed.get("payload") != _invoice_payload_digest(data):
            return json_error("최종 확인 후 입력 내용이 변경되었습니다. 다시 확인해주세요.")
        if signed.get("sources") != _invoice_source_fingerprint(normalized_items):
            return json_error("확인 이후 원본 계산 결과가 변경되었습니다. 다시 확인해주세요.")

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

        for item in normalized_items:
            item_type = item["type"]
            item_data = {
                "combination_id": combination.id,
                "item_type": item_type,
                "billing_month": item["month"],
                "item_description": item.get("description", ""),
                _ITEM_FK_BY_TYPE[item_type]: item["id"],
            }
            db.session.add(InvoiceCombinationItem(**item_data))

        for row in rows:
            unit = row["unit"]
            final_invoice = FinalInvoice(
                combination_id=combination.id,
                unit_id=unit.id,
                electric_amount=row["electric_amount"],
                water_amount=row["water_amount"],
                common_amount=row["common_amount"],
                common_details=row["common_details"] or None,
                total_amount=row["total_amount"],
                unit_memo=row["unit_memo"],
                unit_name_snapshot=unit.unit_name,
                floor_number_snapshot=unit.floor.floor_number,
                floor_name_snapshot=unit.floor.name or "",
                unit_profile_memo_snapshot=unit.memo or "",
                snapshot_source="invoice_issue",
            )
            db.session.add(final_invoice)
            db.session.flush()
            for charge in row["charges"]:
                db.session.add(
                    FinalInvoiceCharge(final_invoice_id=final_invoice.id, **charge)
                )

        db.session.commit()
        return jsonify(
            {"success": True, "message": "청구서가 생성되었습니다.", "id": combination.id}
        )
    except IntegrityError:
        db.session.rollback()
        return json_error("선택한 계산 원본이 이미 다른 정산서에 사용되었습니다. 목록을 새로고침해주세요.")
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
                "정산서별 납부 내역에서 전체 세대의 입금을 확인하고 먼저 삭제하세요.".format(payment_count),
                payment_count=payment_count,
                payments_url=url_for("payments", combination_id=combination_id),
            )

        _create_safety_backup("before_invoice_delete")
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
        Unit.query.order_by(Unit.floor_id, Unit.unit_name).all()
    )
    combinations = InvoiceCombination.query.order_by(
        InvoiceCombination.created_at.desc()
    ).all()
    selected_id = request.args.get("combination_id", type=int)
    selected_combination = None
    combination_payments = []
    unlinked_payment_ids = set()
    payment_invoice_snapshots = {}
    if selected_id is not None:
        selected_combination = InvoiceCombination.query.filter_by(id=selected_id).first_or_404()
        combination_payments = Payment.query.options(
            joinedload(Payment.unit).joinedload(Unit.floor)
        ).filter_by(combination_id=selected_id).order_by(Payment.payment_date, Payment.id).all()
        invoice_units = {identifier for (identifier,) in db.session.query(FinalInvoice.unit_id)
                         .filter_by(combination_id=selected_id).all()}
        payment_invoice_snapshots = {
            invoice.unit_id: {
                "unit_name": invoice.unit_name_snapshot,
                "floor_name": invoice.floor_label_snapshot,
            }
            for invoice in FinalInvoice.query.filter_by(combination_id=selected_id).all()
        }
        unlinked_payment_ids = {payment.id for payment in combination_payments if payment.unit_id not in invoice_units}
    return render_template("payments.html", units=units, combinations=combinations,
                           selected_combination=selected_combination, combination_payments=combination_payments,
                           unlinked_payment_ids=unlinked_payment_ids,
                           payment_invoice_snapshots=payment_invoice_snapshots)


@app.route("/payments/unit_history/<int:unit_id>")
def payment_unit_history(unit_id):
    """세대의 전체 정산 및 납부 이력 (이월 항목 제외 계산)."""
    try:
        unit = db.session.get(Unit, unit_id)
        if unit is None:
            return json_error("세대를 찾을 수 없습니다.", status=404)
        history = balance_service.unit_history(unit_id)
        summary = balance_service.unit_balance(unit_id)
        summary["status"] = balance_service.payment_status(
            summary["total_billed"], summary["total_paid"],
            has_invoice=all(item["has_invoice"] for item in history),
        )
        return jsonify(
            {
                "success": True,
                "unit": {
                    "id": unit.id,
                    "name": unit.unit_name,
                    "floor": unit.floor.name if unit.floor else "",
                },
                "history": history,
                "summary": summary,
            }
        )
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


@app.route("/payments/balance/<int:unit_id>")
def payment_balance(unit_id):
    """세대의 누적 미납/초과 금액 (이월 항목 제외)."""
    try:
        result = balance_service.unit_balance(unit_id)
        result["status"] = balance_service.payment_status(
            result["total_billed"], result["total_paid"],
            has_invoice=not balance_service.unlinked_payment_unit_ids(unit_id),
        )
        result["success"] = True
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


def _payment_warnings(invoice, payment_date, payment_amount, exclude_payment_id=None):
    paid_query = db.session.query(db.func.coalesce(db.func.sum(Payment.payment_amount), 0)).filter(
        Payment.combination_id == invoice.combination_id,
        Payment.unit_id == invoice.unit_id,
    )
    if exclude_payment_id is not None:
        paid_query = paid_query.filter(Payment.id != exclude_payment_id)
    already_paid = int(paid_query.scalar() or 0)
    remaining = balance_service.invoice_billed_amount(invoice) - already_paid
    warnings = []
    if payment_date > date.today():
        warnings.append("입금일이 오늘보다 미래입니다.")
    if payment_amount > max(remaining, 0):
        warnings.append(
            "입금액이 현재 미납액 {:,}원보다 {:,}원 많습니다.".format(
                max(remaining, 0), int(payment_amount - max(remaining, 0))
            )
        )
    return warnings, remaining


@app.route("/payments/add", methods=["POST"])
@csrf_protect
def add_payment():
    try:
        data = request.get_json(silent=True) or {}
        invoice = FinalInvoice.query.options(selectinload(FinalInvoice.charges)).filter_by(
            combination_id=data["combination_id"], unit_id=data["unit_id"]
        ).first()
        if not invoice:
            return json_error("해당 세대의 정산서가 없어 입금을 등록할 수 없습니다.")
        payment_date = datetime.strptime(data["payment_date"], "%Y-%m-%d").date()
        payment_amount = parse_amount(data.get("payment_amount"), "입금액")
        warnings, remaining = _payment_warnings(
            invoice, payment_date, payment_amount
        )
        if warnings and data.get("acknowledge_warnings") is not True:
            return json_error(
                "입금 내용을 한 번 더 확인해주세요.",
                requires_confirmation=True, warnings=warnings, remaining=remaining,
            )
        payment = Payment(
            combination_id=data["combination_id"],
            unit_id=data["unit_id"],
            payment_date=payment_date,
            payment_amount=payment_amount,
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

        invoice = FinalInvoice.query.options(selectinload(FinalInvoice.charges)).filter_by(
            combination_id=payment.combination_id, unit_id=payment.unit_id
        ).first()
        if invoice is None:
            return json_error("연결된 세대별 정산서를 찾을 수 없습니다.")
        payment_date = datetime.strptime(data["payment_date"], "%Y-%m-%d").date()
        payment_amount = parse_amount(data.get("payment_amount"), "입금액")
        warnings, remaining = _payment_warnings(
            invoice, payment_date, payment_amount, exclude_payment_id=payment.id
        )
        if warnings and data.get("acknowledge_warnings") is not True:
            return json_error(
                "입금 내용을 한 번 더 확인해주세요.",
                requires_confirmation=True, warnings=warnings, remaining=remaining,
            )
        payment.payment_date = payment_date
        payment.payment_amount = payment_amount
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
        _create_safety_backup("before_payment_delete")
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
            Unit.query.order_by(Unit.floor_id, Unit.unit_name)
            .all()
        )
        balances = balance_service.all_unit_balances(units)
        unlinked_units = balance_service.unlinked_payment_unit_ids()
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
                "status": balance_service.payment_status(
                    balances[unit.id]["total_billed"], balances[unit.id]["total_paid"],
                    has_invoice=unit.id not in unlinked_units,
                ),
            }
            for unit in units
        ]
        return jsonify({"success": True, "report": report})
    except Exception as e:  # noqa: BLE001
        return json_error(str(e))


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"], host=app.config["HOST"], port=app.config["PORT"])
