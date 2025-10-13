from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
from decimal import Decimal
import math
import secrets
from functools import wraps
import mysql.connector
from mysql.connector import Error
from sqlalchemy.exc import IntegrityError

# =========================
# Safe numeric helpers & JSON provider (Decimal-safe)
# =========================
from decimal import Decimal, InvalidOperation

try:
    from flask.json.provider import DefaultJSONProvider
except Exception:
    DefaultJSONProvider = None


def dec(val, q=None):
    if isinstance(val, Decimal):
        x = val
    elif isinstance(val, (int, float)):
        x = Decimal(str(val))
    else:
        try:
            s = (val if val is not None else "").strip()
        except Exception:
            s = str(val or "")
        if s == "":
            x = Decimal("0")
        else:
            try:
                x = Decimal(s.replace(",", ""))
            except Exception:
                x = Decimal("0")
    if q:
        try:
            x = x.quantize(q)
        except Exception:
            pass
    return x


def to_int(val, default=0):
    try:
        s = (val if val is not None else "").strip()
    except Exception:
        s = str(val or "")
    if s == "":
        return default
    try:
        return int(float(s.replace(",", "")))
    except Exception:
        return default


def to_jsonable(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (list, tuple)):
        return [to_jsonable(x) for x in o]
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in o.items()}
    return o


# ======================================================
# Flask / DB bootstrap
# ======================================================
app = Flask(__name__)

# Install Decimal-safe JSON provider
try:
    if 'app' in globals():
        if DefaultJSONProvider is not None:
            class DecimalJSONProvider(DefaultJSONProvider):
                def default(self, o):
                    if isinstance(o, Decimal):
                        return float(o)
                    return super().default(o)


            app.json = DecimalJSONProvider(app)
        else:
            from flask.json import JSONEncoder as _JSONEncoder


            class _DecimalEncoder(_JSONEncoder):
                def default(self, o):
                    if isinstance(o, Decimal):
                        return float(o)
                    return super().default(o)


            app.json_encoder = _DecimalEncoder
except Exception:
    pass

app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://power_user:mslee0702@localhost/bill_calculator'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 3600}
db = SQLAlchemy(app)


def init_database():
    """Create DB if it doesn't exist to avoid first-run failure."""
    try:
        connection = mysql.connector.connect(host='localhost', user='power_user', password='mslee0702')
        cursor = connection.cursor()
        cursor.execute("CREATE DATABASE IF NOT EXISTS bill_calculator CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        connection.commit()
        cursor.close()
        connection.close()
    except Error as e:
        print(f"[bootstrap] Database initialization error: {e}")


# ======================================================
# Models
# ======================================================
class Floor(db.Model):
    __tablename__ = 'floors'
    id = db.Column(db.Integer, primary_key=True)
    floor_number = db.Column(db.Integer, nullable=False, unique=True)
    name = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    units = db.relationship('Unit', backref='floor', lazy=True, cascade='all, delete-orphan')


class Unit(db.Model):
    __tablename__ = 'units'
    id = db.Column(db.Integer, primary_key=True)
    floor_id = db.Column(db.Integer, db.ForeignKey('floors.id'), nullable=False)
    unit_name = db.Column(db.String(50), nullable=False)
    memo = db.Column(db.Text)
    electric_welfare = db.Column(db.Boolean, default=False)
    electric_voucher = db.Column(db.Boolean, default=False)
    has_tv = db.Column(db.Boolean, default=True)
    water_welfare = db.Column(db.Boolean, default=False)
    residents_count = db.Column(db.Integer, default=1)
    is_vacant = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(50), unique=True, nullable=False)
    setting_value = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ElectricBill(db.Model):
    __tablename__ = 'electric_bills'
    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False)
    floor_id = db.Column(db.Integer, db.ForeignKey('floors.id'), nullable=False)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    welfare_discount = db.Column(db.Numeric(10, 2), default=0)
    voucher_discount = db.Column(db.Numeric(10, 2), default=0)
    tv_fee_total = db.Column(db.Numeric(10, 2), default=0)
    tv_distribution_mode = db.Column(db.String(20), default='INDIVIDUAL')
    tv_units_count = db.Column(db.Integer, default=0)
    billing_months_count = db.Column(db.Integer, default=1)
    monthly_details = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    floor_ref = db.relationship('Floor', backref='electric_bills')
    readings = db.relationship('ElectricReading', backref='electric_bill', cascade='all, delete-orphan')
    details = db.relationship('ElectricBillDetail', backref='electric_bill', cascade='all, delete-orphan')


class ElectricReading(db.Model):
    __tablename__ = 'electric_readings'
    id = db.Column(db.Integer, primary_key=True)
    electric_bill_id = db.Column(db.Integer, db.ForeignKey('electric_bills.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    previous_reading = db.Column(db.Numeric(10, 2), nullable=False)
    current_reading = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    unit = db.relationship('Unit', backref='electric_readings')


class ElectricBillDetail(db.Model):
    __tablename__ = 'electric_bill_details'
    id = db.Column(db.Integer, primary_key=True)
    electric_bill_id = db.Column(db.Integer, db.ForeignKey('electric_bills.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    usage_amount = db.Column(db.Numeric(10, 2), nullable=False)
    base_amount = db.Column(db.Numeric(10, 2), nullable=False)
    welfare_discount = db.Column(db.Numeric(10, 2), default=0)
    voucher_discount = db.Column(db.Numeric(10, 2), default=0)
    tv_fee = db.Column(db.Numeric(10, 2), default=0)
    final_amount = db.Column(db.Numeric(10, 2), nullable=False)
    charged_amount = db.Column(db.Numeric(10, 2), nullable=False)
    unit_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    unit = db.relationship('Unit', backref='electric_bill_details')


class WaterBill(db.Model):
    __tablename__ = 'water_bills'
    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False, unique=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    welfare_discount_total = db.Column(db.Numeric(10, 2), default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    details = db.relationship('WaterBillDetail', backref='water_bill', cascade='all, delete-orphan')


class WaterBillDetail(db.Model):
    __tablename__ = 'water_bill_details'
    id = db.Column(db.Integer, primary_key=True)
    water_bill_id = db.Column(db.Integer, db.ForeignKey('water_bills.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    base_amount = db.Column(db.Numeric(10, 2), nullable=False)
    welfare_discount = db.Column(db.Numeric(10, 2), default=0)
    final_amount = db.Column(db.Numeric(10, 2), nullable=False)
    charged_amount = db.Column(db.Numeric(10, 2), nullable=False)
    unit_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    unit = db.relationship('Unit', backref='water_bill_details')


class CommonBill(db.Model):
    __tablename__ = 'common_bills'
    id = db.Column(db.Integer, primary_key=True)
    billing_month = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255))
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    distribution_method = db.Column(db.Enum('BY_RESIDENTS', 'BY_UNITS'), default='BY_RESIDENTS')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    details = db.relationship('CommonBillDetail', backref='common_bill', cascade='all, delete-orphan')


class CommonBillDetail(db.Model):
    __tablename__ = 'common_bill_details'
    id = db.Column(db.Integer, primary_key=True)
    common_bill_id = db.Column(db.Integer, db.ForeignKey('common_bills.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    charged_amount = db.Column(db.Numeric(10, 2), nullable=False)
    unit_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    unit = db.relationship('Unit', backref='common_bill_details')


class InvoiceCombination(db.Model):
    __tablename__ = 'invoice_combinations'
    id = db.Column(db.Integer, primary_key=True)
    invoice_name = db.Column(db.String(255), nullable=False)
    memo = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    items = db.relationship('InvoiceCombinationItem', backref='combination', cascade='all, delete-orphan')
    invoices = db.relationship('FinalInvoice', backref='combination', cascade='all, delete-orphan')


class InvoiceCombinationItem(db.Model):
    __tablename__ = 'invoice_combination_items'
    id = db.Column(db.Integer, primary_key=True)
    combination_id = db.Column(db.Integer, db.ForeignKey('invoice_combinations.id'), nullable=False)
    item_type = db.Column(db.Enum('ELECTRIC', 'WATER', 'COMMON'), nullable=False)
    item_id = db.Column(db.Integer, nullable=False)
    billing_month = db.Column(db.Date, nullable=False)
    item_description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FinalInvoice(db.Model):
    __tablename__ = 'final_invoices'
    id = db.Column(db.Integer, primary_key=True)
    combination_id = db.Column(db.Integer, db.ForeignKey('invoice_combinations.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    electric_amount = db.Column(db.Numeric(10, 2), default=0)
    water_amount = db.Column(db.Numeric(10, 2), default=0)
    common_amount = db.Column(db.Numeric(10, 2), default=0)
    common_details = db.Column(db.JSON)
    additional_charges = db.Column(db.JSON)  # 세대별 특별 추가금
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    memo = db.Column(db.Text)
    unit_memo = db.Column(db.Text)  # 세대별 개별 메모
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    unit = db.relationship('Unit', backref='final_invoices')


# ======================================================
# CSRF
# ======================================================
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


app.jinja_env.globals['csrf_token'] = generate_csrf_token


def csrf_protect(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == "POST":
            token = session.get('_csrf_token', None)
            req_token = request.form.get('_csrf_token') or (request.get_json(silent=True) or {}).get('_csrf_token')
            if not token or token != req_token:
                return jsonify({'success': False, 'message': 'CSRF 토큰이 유효하지 않습니다.'}), 403
        return f(*args, **kwargs)

    return decorated_function


# ======================================================
# Utils
# ======================================================
def round_up_to_10(amount):
    return math.ceil(float(amount) / 10) * 10


def get_setting(key, default=None):
    s = Setting.query.filter_by(setting_key=key).first()
    return s.setting_value if s else default


def set_setting(key, value):
    s = Setting.query.filter_by(setting_key=key).first()
    if s:
        s.setting_value = str(value)
    else:
        s = Setting(setting_key=key, setting_value=str(value))
        db.session.add(s)


def create_unit_snapshot(unit):
    return {
        'unit_name': unit.unit_name,
        'electric_welfare': unit.electric_welfare,
        'electric_voucher': unit.electric_voucher,
        'has_tv': unit.has_tv,
        'water_welfare': unit.water_welfare,
        'residents_count': unit.residents_count,
        'is_vacant': unit.is_vacant
    }


def first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


# ======================================================
# Routes - Core pages
# ======================================================
@app.route('/')
def index():
    floors_count = Floor.query.count()
    units_count = Unit.query.count()
    vacant_count = Unit.query.filter_by(is_vacant=True).count()
    occupied_count = units_count - vacant_count
    return render_template('index.html',
                           floors_count=floors_count,
                           units_count=units_count,
                           vacant_count=vacant_count,
                           occupied_count=occupied_count)


@app.route('/settings', endpoint='settings')
def settings_page():
    floors = Floor.query.order_by(Floor.floor_number).all()
    ctx = {
        'floors': floors,
        'tv_fee': get_setting('tv_fee', '2500'),
        'electric_welfare_amount': get_setting('electric_welfare_amount', '0'),
        'electric_voucher_amount': get_setting('electric_voucher_amount', '0'),
        'water_welfare_amount': get_setting('water_welfare_amount', '0'),
        'invoice_default_memo': get_setting('invoice_default_memo', ''),
    }
    return render_template('settings.html', **ctx)


@app.route('/settings/save', methods=['POST'])
@csrf_protect
def save_settings():
    try:
        set_setting('tv_fee', request.form.get('tv_fee', '2500'))
        set_setting('electric_welfare_amount', request.form.get('electric_welfare_amount', '0'))
        set_setting('electric_voucher_amount', request.form.get('electric_voucher_amount', '0'))
        set_setting('water_welfare_amount', request.form.get('water_welfare_amount', '0'))
        set_setting('invoice_default_memo', request.form.get('invoice_default_memo', ''))
        db.session.commit()
        flash('설정이 저장되었습니다.', 'success')
        return redirect(url_for('settings'))
    except Exception as e:
        db.session.rollback()
        flash(f'설정 저장 실패: {e}', 'error')
        return redirect(url_for('settings'))


@app.route('/settings/export')
def export_settings():
    floors = Floor.query.order_by(Floor.floor_number).all()
    payload = {
        'settings': {
            'tv_fee': get_setting('tv_fee', '2500'),
            'electric_welfare_amount': get_setting('electric_welfare_amount', '0'),
            'electric_voucher_amount': get_setting('electric_voucher_amount', '0'),
            'water_welfare_amount': get_setting('water_welfare_amount', '0'),
            'invoice_default_memo': get_setting('invoice_default_memo', ''),
        },
        'floors': []
    }
    for f in floors:
        payload['floors'].append({
            'floor_number': f.floor_number,
            'name': f.name,
            'units': [{
                'unit_name': u.unit_name,
                'memo': u.memo,
                'electric_welfare': bool(u.electric_welfare),
                'electric_voucher': bool(u.electric_voucher),
                'has_tv': bool(u.has_tv),
                'water_welfare': bool(u.water_welfare),
                'residents_count': u.residents_count,
                'is_vacant': bool(u.is_vacant)
            } for u in f.units]
        })
    return jsonify(payload)


@app.route('/settings/import', methods=['POST'])
@csrf_protect
def import_settings():
    try:
        data = request.get_json(force=True)
        for f in Floor.query.all():
            db.session.delete(f)
        db.session.flush()

        s = data.get('settings', {})
        set_setting('tv_fee', s.get('tv_fee', '2500'))
        set_setting('electric_welfare_amount', s.get('electric_welfare_amount', '0'))
        set_setting('electric_voucher_amount', s.get('electric_voucher_amount', '0'))
        set_setting('water_welfare_amount', s.get('water_welfare_amount', '0'))
        set_setting('invoice_default_memo', s.get('invoice_default_memo', ''))

        for f in data.get('floors', []):
            floor = Floor(
                floor_number=int(f.get('floor_number')),
                name=f.get('name')
            )
            db.session.add(floor)
            db.session.flush()
            for u in f.get('units', []):
                unit = Unit(
                    floor_id=floor.id,
                    unit_name=u.get('unit_name'),
                    memo=u.get('memo', ''),
                    electric_welfare=bool(u.get('electric_welfare', False)),
                    electric_voucher=bool(u.get('electric_voucher', False)),
                    has_tv=bool(u.get('has_tv', True)),
                    water_welfare=bool(u.get('water_welfare', False)),
                    residents_count=int(u.get('residents_count', 1)),
                    is_vacant=bool(u.get('is_vacant', False)),
                )
                db.session.add(unit)

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Import 실패: {e}'})


@app.route('/floors/add', methods=['POST'])
@csrf_protect
def add_floor():
    try:
        floor_number_str = request.form.get('floor_number', '').strip()
        if not floor_number_str:
            return jsonify({'success': False, 'message': '층 번호를 입력해주세요.'})

        floor_number = to_int(floor_number_str, None)
        if floor_number is None:
            return jsonify({'success': False, 'message': '층 번호는 정수로 입력해주세요.'})

        name = request.form.get('name') or (f"B{abs(floor_number)}층" if floor_number < 0 else f"{floor_number}층")
        if Floor.query.filter_by(floor_number=floor_number).first():
            return jsonify({'success': False, 'message': '이미 같은 층 번호가 존재합니다.'})
        floor = Floor(floor_number=floor_number, name=name)
        db.session.add(floor)
        db.session.commit()
        return jsonify({'success': True, 'message': '층이 추가되었습니다.'})
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'message': '이미 같은 층 번호가 존재합니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'층 추가 실패: {e}'})


@app.route('/floors/<int:floor_id>/update', methods=['POST'])
@csrf_protect
def update_floor(floor_id):
    try:
        floor = Floor.query.get_or_404(floor_id)
        new_name = (request.form.get('name') or '').strip()
        new_number_raw = (request.form.get('floor_number') or '').strip()

        if new_number_raw:
            new_number = to_int(new_number_raw, None)
            if new_number is None:
                return jsonify({'success': False, 'message': '층 번호는 정수로 입력하세요.'})
            exists = Floor.query.filter(Floor.floor_number == new_number, Floor.id != floor.id).first()
            if exists:
                return jsonify({'success': False, 'message': '이미 같은 층 번호가 존재합니다.'})
            floor.floor_number = new_number
            if not new_name:
                new_name = f"B{abs(new_number)}층" if new_number < 0 else f"{new_number}층"

        if new_name:
            floor.name = new_name

        db.session.commit()
        return jsonify({'success': True, 'message': '층 정보가 수정되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/floors/<int:floor_id>/delete', methods=['POST'])
@csrf_protect
def delete_floor(floor_id):
    try:
        floor = Floor.query.get_or_404(floor_id)
        db.session.delete(floor)
        db.session.commit()
        return jsonify({'success': True, 'message': '층이 삭제되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/units/add', methods=['POST'])
@csrf_protect
def add_unit():
    try:
        floor_id = to_int(request.form.get('floor_id'), 0)
        if not floor_id:
            return jsonify({'success': False, 'message': '층을 선택해주세요.'})

        unit = Unit(
            floor_id=floor_id,
            unit_name=request.form.get('unit_name'),
            memo=request.form.get('memo', ''),
            electric_welfare=request.form.get('electric_welfare') == 'true',
            electric_voucher=request.form.get('electric_voucher') == 'true',
            has_tv=request.form.get('has_tv') == 'true',
            water_welfare=request.form.get('water_welfare') == 'true',
            residents_count=to_int(request.form.get('residents_count', '1'), 1),
            is_vacant=request.form.get('is_vacant') == 'true'
        )
        db.session.add(unit)
        db.session.commit()
        return jsonify({'success': True, 'message': '세대가 추가되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/units/<int:unit_id>/update', methods=['POST'])
@csrf_protect
def update_unit(unit_id):
    try:
        unit = Unit.query.get_or_404(unit_id)
        unit.unit_name = request.form.get('unit_name', unit.unit_name)
        unit.memo = request.form.get('memo', '')
        unit.electric_welfare = request.form.get('electric_welfare') == 'true'
        unit.electric_voucher = request.form.get('electric_voucher') == 'true'
        unit.has_tv = request.form.get('has_tv') == 'true'
        unit.water_welfare = request.form.get('water_welfare') == 'true'
        unit.residents_count = to_int(request.form.get('residents_count'), unit.residents_count or 1)
        unit.is_vacant = request.form.get('is_vacant') == 'true'
        db.session.commit()
        return jsonify({'success': True, 'message': '세대 정보가 수정되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/units/<int:unit_id>/delete', methods=['POST'])
@csrf_protect
def delete_unit(unit_id):
    try:
        unit = Unit.query.get_or_404(unit_id)
        db.session.delete(unit)
        db.session.commit()
        return jsonify({'success': True, 'message': '세대가 삭제되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


# ======================================================
# Calculator
# ======================================================
@app.route('/calculator')
def calculator():
    floors = Floor.query.order_by(Floor.floor_number).all()
    units = Unit.query.order_by(Unit.floor_id, Unit.unit_name).all()
    total_units = len(units)
    occupied_units = sum(1 for u in units if not u.is_vacant)
    vacant_units = total_units - occupied_units
    total_residents = sum(u.residents_count for u in units if not u.is_vacant)

    floors_json = [{'id': f.id, 'name': f.name, 'floor_number': f.floor_number} for f in floors]
    units_json = [{
        'id': u.id,
        'floor_id': u.floor_id,
        'unit_name': u.unit_name,
        'residents_count': u.residents_count,
        'is_vacant': bool(u.is_vacant),
        'has_tv': bool(u.has_tv),
        'electric_welfare': bool(u.electric_welfare),
        'electric_voucher': bool(u.electric_voucher),
        'water_welfare': bool(u.water_welfare),
    } for u in units]

    return render_template('calculator.html',
                           floors=floors, units=units,
                           floors_json=floors_json, units_json=units_json,
                           total_units=total_units, occupied_units=occupied_units,
                           vacant_units=vacant_units, total_residents=total_residents)


@app.route('/calculate/electric', methods=['POST'])
@csrf_protect
def calculate_electric():
    try:
        billing_month = datetime.strptime(request.form.get('billing_month'), '%Y-%m').date().replace(day=1)
        floor_id = to_int(request.form.get('floor_id'), 0)
        if not floor_id:
            return jsonify({'success': False, 'message': '층을 선택해주세요.'})
        tv_distribution_mode = request.form.get('tv_distribution_mode', 'INDIVIDUAL')

        monthly_details = []
        total_amount = dec(0)
        welfare_discount_input = dec(0)  # 고지서의 복지할인 총액
        voucher_discount_input = dec(0)  # 고지서의 바우처할인 총액
        tv_fee_total = dec(0)

        month_count = to_int(request.form.get('month_count', '1'), 1)
        for i in range(month_count):
            month_data = {
                'month': request.form.get(f'month_{i}'),
                'amount': float(dec(request.form.get(f'amount_{i}', 0))),
                'welfare': float(dec(request.form.get(f'welfare_{i}', 0))),
                'voucher': float(dec(request.form.get(f'voucher_{i}', 0))),
                'tv_fee': float(dec(request.form.get(f'tv_fee_{i}', 0)))
            }
            monthly_details.append(month_data)
            total_amount += dec(month_data['amount'])
            welfare_discount_input += dec(month_data['welfare'])
            voucher_discount_input += dec(month_data['voucher'])
            tv_fee_total += dec(month_data['tv_fee'])

        existing = ElectricBill.query.filter_by(billing_month=billing_month, floor_id=floor_id).first()
        if existing and request.form.get('overwrite') != 'true':
            return jsonify({'success': False, 'exists': True, 'message': '해당 월의 전기요금이 이미 존재합니다.'})
        if existing:
            db.session.delete(existing)
            db.session.flush()

        bill = ElectricBill(
            billing_month=billing_month,
            floor_id=floor_id,
            total_amount=total_amount,
            welfare_discount=dec(0),  # 나중에 실제 적용값으로 업데이트
            voucher_discount=dec(0),  # 나중에 실제 적용값으로 업데이트
            tv_fee_total=tv_fee_total,
            tv_distribution_mode=tv_distribution_mode,
            tv_units_count=0,
            billing_months_count=month_count,
            monthly_details=monthly_details
        )
        db.session.add(bill)
        db.session.flush()

        floor = Floor.query.get(floor_id)
        units = [u for u in floor.units if not u.is_vacant]
        total_usage = dec(0)
        readings = []

        for unit in units:
            prev_reading = dec(request.form.get(f'prev_{unit.id}', 0))
            curr_reading = dec(request.form.get(f'curr_{unit.id}', 0))
            total_usage += (curr_reading - prev_reading)
            reading = ElectricReading(electric_bill_id=bill.id, unit_id=unit.id,
                                      previous_reading=prev_reading, current_reading=curr_reading)
            db.session.add(reading)
            readings.append(reading)

        tv_fee = dec(get_setting('tv_fee', '2500') or '2500')

        if tv_distribution_mode == 'EQUAL':
            tv_fee_per_unit = (tv_fee_total / len(units)) if units else dec(0)
        else:
            tv_fee_per_unit = tv_fee * month_count

        # 복지/바우처 대상 세대 확인
        welfare_units = [u for u in units if u.electric_welfare]
        voucher_units = [u for u in units if u.electric_voucher]

        # 🔧 1단계: 실제로 적용될 세대당 할인액 계산
        if welfare_discount_input > 0 and welfare_units:
            # 입력값이 있으면 해당 세대들에게 균등 분배
            welfare_per_unit = welfare_discount_input / len(welfare_units)
            total_welfare_to_apply = welfare_discount_input
        elif welfare_units:
            # 입력값이 없으면 설정값 사용
            welfare_per_unit = dec(get_setting('electric_welfare_amount', '0')) * month_count
            total_welfare_to_apply = welfare_per_unit * len(welfare_units)
        else:
            welfare_per_unit = dec(0)
            total_welfare_to_apply = dec(0)

        if voucher_discount_input > 0 and voucher_units:
            # 입력값이 있으면 해당 세대들에게 균등 분배
            voucher_per_unit = voucher_discount_input / len(voucher_units)
            total_voucher_to_apply = voucher_discount_input
        elif voucher_units:
            # 입력값이 없으면 설정값 사용
            voucher_per_unit = dec(get_setting('electric_voucher_amount', '0')) * month_count
            total_voucher_to_apply = voucher_per_unit * len(voucher_units)
        else:
            voucher_per_unit = dec(0)
            total_voucher_to_apply = dec(0)

        # 🔧 2단계: 할인 전 원래 금액 계산 (실제 적용될 할인액 사용)
        # 고지액 = 원래금액 - 실제적용할인
        # 따라서: 원래금액 = 고지액 + 실제적용할인
        original_amount = total_amount + total_welfare_to_apply + total_voucher_to_apply

        # 🔧 3단계: 사용량 비례 분배 및 세대별 할인 적용
        for unit, reading in zip(units, readings):
            usage = reading.current_reading - reading.previous_reading

            # 할인 전 원래 금액을 사용량 비례로 분배
            base_amount = (usage / total_usage) * original_amount if total_usage > 0 else (
                original_amount / len(units) if units else dec(0))

            # 해당 세대의 복지/바우처 할인액
            unit_welfare = welfare_per_unit if unit.electric_welfare else dec(0)
            unit_voucher = voucher_per_unit if unit.electric_voucher else dec(0)

            # TV 수신료
            if tv_distribution_mode == 'EQUAL':
                unit_tv_fee = tv_fee_per_unit
            else:
                unit_tv_fee = tv_fee_per_unit if unit.has_tv else dec(0)

            # 최종 금액 = 기본 분배액 - 복지할인 - 바우처할인 + TV수신료
            final_amount = base_amount - unit_welfare - unit_voucher + unit_tv_fee
            if final_amount < 0:
                final_amount = dec(0)

            charged_amount = dec(round_up_to_10(final_amount))

            detail = ElectricBillDetail(
                electric_bill_id=bill.id, unit_id=unit.id,
                usage_amount=usage, base_amount=base_amount,
                welfare_discount=unit_welfare, voucher_discount=unit_voucher,
                tv_fee=unit_tv_fee, final_amount=final_amount, charged_amount=charged_amount,
                unit_snapshot=create_unit_snapshot(unit)
            )
            db.session.add(detail)

        # 🔧 4단계: bill에 실제 적용된 총 할인액 저장
        bill.welfare_discount = total_welfare_to_apply
        bill.voucher_discount = total_voucher_to_apply

        db.session.commit()
        return jsonify({'success': True, 'message': '전기요금이 계산되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/calculate/water', methods=['POST'])
@csrf_protect
def calculate_water():
    try:
        billing_month = datetime.strptime(request.form.get('billing_month'), '%Y-%m').date().replace(day=1)
        total_amount = dec(request.form.get('total_amount'))
        welfare_discount_input = dec(request.form.get('welfare_discount_total', '0'))

        existing = WaterBill.query.filter_by(billing_month=billing_month).first()
        if existing and request.form.get('overwrite') != 'true':
            return jsonify({'success': False, 'exists': True, 'message': '해당 월의 수도요금이 이미 존재합니다.'})
        if existing:
            db.session.delete(existing)
            db.session.flush()

        bill = WaterBill(
            billing_month=billing_month,
            total_amount=total_amount,
            welfare_discount_total=dec(0)  # 나중에 실제 적용값으로 업데이트
        )
        db.session.add(bill)
        db.session.flush()

        units = Unit.query.filter_by(is_vacant=False).all()
        total_residents = sum(u.residents_count for u in units)

        # 복지 대상 세대 확인
        welfare_units = [u for u in units if u.water_welfare]

        # 🔧 1단계: 실제로 적용될 세대당 복지 할인액 계산
        if welfare_discount_input > 0 and welfare_units:
            # 입력된 복지 할인을 복지 대상 세대에게 균등 분배
            welfare_per_unit = welfare_discount_input / len(welfare_units)
            total_welfare_to_apply = welfare_discount_input
        elif welfare_units:
            # 입력값이 없으면 설정값 사용
            welfare_per_unit = dec(get_setting('water_welfare_amount', '0'))
            total_welfare_to_apply = welfare_per_unit * len(welfare_units)
        else:
            welfare_per_unit = dec(0)
            total_welfare_to_apply = dec(0)

        # 🔧 2단계: 할인 전 원래 금액 계산 (실제 적용될 할인액 사용)
        # 고지액 = 원래금액 - 실제적용할인
        # 따라서: 원래금액 = 고지액 + 실제적용할인
        original_amount = total_amount + total_welfare_to_apply

        # 🔧 3단계: 인원수 비례 분배 및 세대별 할인 적용
        for unit in units:
            # 할인 전 원래 금액을 인원수 비례로 분배
            base_amount = (dec(unit.residents_count) / dec(
                total_residents) * original_amount) if total_residents > 0 else (
                original_amount / len(units) if units else dec(0))

            # 해당 세대의 복지 할인액
            unit_welfare = welfare_per_unit if unit.water_welfare else dec(0)

            # 최종 금액 = 기본 분배액 - 복지할인
            final_amount = base_amount - unit_welfare
            if final_amount < 0:
                final_amount = dec(0)
            charged_amount = dec(round_up_to_10(final_amount))

            detail = WaterBillDetail(
                water_bill_id=bill.id, unit_id=unit.id, base_amount=base_amount,
                welfare_discount=unit_welfare, final_amount=final_amount, charged_amount=charged_amount,
                unit_snapshot=create_unit_snapshot(unit)
            )
            db.session.add(detail)

        # 🔧 4단계: bill에 실제 적용된 총 할인액 저장
        bill.welfare_discount_total = total_welfare_to_apply

        db.session.commit()
        return jsonify({'success': True, 'message': '수도요금이 계산되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/calculate/common', methods=['POST'])
@csrf_protect
def calculate_common():
    try:
        billing_month = datetime.strptime(request.form.get('billing_month'), '%Y-%m').date().replace(day=1)
        description = request.form.get('description')
        total_amount = dec(request.form.get('total_amount'))
        distribution_method = request.form.get('distribution_method', 'BY_RESIDENTS')

        bill = CommonBill(billing_month=billing_month, description=description, total_amount=total_amount,
                          distribution_method=distribution_method)
        db.session.add(bill)
        db.session.flush()

        units = Unit.query.filter_by(is_vacant=False).all()

        if distribution_method == 'BY_RESIDENTS':
            total_residents = sum(u.residents_count for u in units)
            for unit in units:
                amount = (dec(unit.residents_count) / dec(total_residents) * total_amount) if total_residents > 0 else (
                    total_amount / len(units) if units else dec(0))
                charged_amount = dec(round_up_to_10(amount))
                db.session.add(CommonBillDetail(common_bill_id=bill.id, unit_id=unit.id, amount=amount,
                                                charged_amount=charged_amount,
                                                unit_snapshot=create_unit_snapshot(unit)))
        else:
            amount_per_unit = total_amount / len(units) if units else dec(0)
            for unit in units:
                charged_amount = dec(round_up_to_10(amount_per_unit))
                db.session.add(CommonBillDetail(common_bill_id=bill.id, unit_id=unit.id, amount=amount_per_unit,
                                                charged_amount=charged_amount,
                                                unit_snapshot=create_unit_snapshot(unit)))

        db.session.commit()
        return jsonify({'success': True, 'message': '공동 공과금이 계산되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


# ======================================================
# Views / Delete
# ======================================================
@app.route('/view')
def view_bills():
    view_type = request.args.get('view', 'month')
    selected_month = request.args.get('month')
    selected_floor = request.args.get('floor')
    selected_unit = request.args.get('unit')

    electric_bills = ElectricBill.query.order_by(ElectricBill.billing_month.desc()).all()
    water_bills = WaterBill.query.order_by(WaterBill.billing_month.desc()).all()
    common_bills = CommonBill.query.order_by(CommonBill.billing_month.desc(), CommonBill.id.desc()).all()
    floors = Floor.query.order_by(Floor.floor_number).all()
    units = Unit.query.order_by(Unit.floor_id, Unit.unit_name).all()

    electric_bills_json = [{
        'id': b.id,
        'billing_month': b.billing_month.isoformat(),
        'floor_name': b.floor_ref.name if b.floor_ref else '',
        'total_amount': float(b.total_amount),
        'welfare_discount': float(b.welfare_discount or 0),
        'voucher_discount': float(b.voucher_discount or 0),
        'tv_fee_total': float(b.tv_fee_total or 0),
        'billing_months_count': b.billing_months_count or 1,
        'monthly_details': b.monthly_details or []
    } for b in electric_bills]

    water_bills_json = [{
        'id': b.id,
        'billing_month': b.billing_month.isoformat(),
        'total_amount': float(b.total_amount),
        'welfare_discount_total': float(b.welfare_discount_total or 0)
    } for b in water_bills]

    common_bills_json = [{
        'id': b.id,
        'billing_month': b.billing_month.isoformat(),
        'description': b.description or '',
        'total_amount': float(b.total_amount),
        'distribution_method': b.distribution_method
    } for b in common_bills]

    return render_template('view.html',
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
                           selected_unit=selected_unit)


@app.route('/view/electric/<int:bill_id>')
def view_electric_detail(bill_id):
    bill = ElectricBill.query.get_or_404(bill_id)
    details = ElectricBillDetail.query.filter_by(electric_bill_id=bill_id).all()
    readings = ElectricReading.query.filter_by(electric_bill_id=bill_id).all()
    readings_map = {r.unit_id: r for r in readings}
    return render_template('view_electric_detail.html',
                           bill=bill,
                           details=details,
                           readings_map=readings_map)


@app.route('/view/water/<int:bill_id>')
def view_water_detail(bill_id):
    bill = WaterBill.query.get_or_404(bill_id)
    details = WaterBillDetail.query.filter_by(water_bill_id=bill_id).all()
    return render_template('view_water_detail.html', bill=bill, details=details)


@app.route('/view/common/<int:bill_id>')
def view_common_detail(bill_id):
    bill = CommonBill.query.get_or_404(bill_id)
    details = CommonBillDetail.query.filter_by(common_bill_id=bill_id).all()
    return render_template('view_common_detail.html', bill=bill, details=details)


@app.route('/bills/delete/<bill_type>/<int:bill_id>', methods=['POST'])
@csrf_protect
def delete_bill(bill_type, bill_id):
    try:
        if bill_type == 'electric':
            bill = ElectricBill.query.get_or_404(bill_id)
        elif bill_type == 'water':
            bill = WaterBill.query.get_or_404(bill_id)
        elif bill_type == 'common':
            bill = CommonBill.query.get_or_404(bill_id)
        else:
            return jsonify({'success': False, 'message': '잘못된 요청입니다.'})
        db.session.delete(bill)
        db.session.commit()
        return jsonify({'success': True, 'message': '삭제되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


# ======================================================
# Invoice
# ======================================================
@app.route('/invoice')
def invoice_combination():
    electric_bills = ElectricBill.query.order_by(ElectricBill.billing_month.desc()).all()
    water_bills = WaterBill.query.order_by(WaterBill.billing_month.desc()).all()
    common_bills = CommonBill.query.order_by(CommonBill.billing_month.desc(), CommonBill.id.desc()).all()
    combinations = InvoiceCombination.query.order_by(InvoiceCombination.created_at.desc()).all()
    units = Unit.query.filter_by(is_vacant=False).order_by(Unit.floor_id, Unit.unit_name).all()

    units_json = [{
        'id': u.id,
        'unit_name': u.unit_name,
        'is_vacant': u.is_vacant
    } for u in units]

    return render_template('invoice.html',
                           electric_bills=electric_bills,
                           water_bills=water_bills,
                           common_bills=common_bills,
                           combinations=combinations,
                           units=units_json)


@app.route('/invoice/create', methods=['POST'])
@csrf_protect
def create_invoice():
    try:
        data = request.get_json() or {}

        # 고정 메모를 메모 앞에 추가
        default_memo = get_setting('invoice_default_memo', '')
        user_memo = data.get('memo', '')

        if default_memo and user_memo:
            combined_memo = f"{default_memo}\n\n{user_memo}"
        elif default_memo:
            combined_memo = default_memo
        else:
            combined_memo = user_memo

        combination = InvoiceCombination(invoice_name=data['name'], memo=combined_memo)
        db.session.add(combination)
        db.session.flush()

        for item in data.get('items', []):
            month = datetime.strptime(item['month'], '%Y-%m-%d').date()
            db.session.add(InvoiceCombinationItem(
                combination_id=combination.id,
                item_type=item['type'],
                item_id=item['id'],
                billing_month=month,
                item_description=item.get('description', '')
            ))

        # 세대별 추가금 및 메모 정보 받기
        unit_additional_data = data.get('unit_additional_data', {})

        units = Unit.query.filter_by(is_vacant=False).all()
        for unit in units:
            electric_total = dec(0)
            water_total = dec(0)
            common_total = dec(0)
            common_details_list = []

            for item in data.get('items', []):
                if item['type'] == 'ELECTRIC':
                    d = ElectricBillDetail.query.filter_by(electric_bill_id=item['id'], unit_id=unit.id).first()
                    if d: electric_total += d.charged_amount
                elif item['type'] == 'WATER':
                    d = WaterBillDetail.query.filter_by(water_bill_id=item['id'], unit_id=unit.id).first()
                    if d: water_total += d.charged_amount
                elif item['type'] == 'COMMON':
                    d = CommonBillDetail.query.filter_by(common_bill_id=item['id'], unit_id=unit.id).first()
                    if d:
                        common_total += d.charged_amount
                        common_details_list.append({
                            'description': item.get('description', '공동 공과금'),
                            'amount': float(d.charged_amount)
                        })

            # 세대별 추가금 처리
            unit_key = str(unit.id)
            additional_charges = []
            additional_total = dec(0)

            if unit_key in unit_additional_data:
                unit_data = unit_additional_data[unit_key]
                for charge in unit_data.get('charges', []):
                    charge_amount = dec(charge.get('amount', 0))
                    additional_charges.append({
                        'description': charge.get('description', ''),
                        'amount': float(charge_amount)
                    })
                    additional_total += charge_amount

            # 최종 금액 계산 (기존 금액 + 추가금)
            total = electric_total + water_total + common_total + additional_total

            # 세대별 메모
            unit_memo = ''
            if unit_key in unit_additional_data:
                unit_memo = unit_additional_data[unit_key].get('memo', '')

            db.session.add(FinalInvoice(
                combination_id=combination.id,
                unit_id=unit.id,
                electric_amount=electric_total,
                water_amount=water_total,
                common_amount=common_total,
                common_details=common_details_list if common_details_list else None,
                additional_charges=additional_charges if additional_charges else None,
                total_amount=total,
                memo=combined_memo,
                unit_memo=unit_memo
            ))

        db.session.commit()
        return jsonify({'success': True, 'message': '청구서가 생성되었습니다.', 'id': combination.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/invoice/view/<int:combination_id>')
def view_invoice(combination_id):
    combination = InvoiceCombination.query.get_or_404(combination_id)
    invoices = FinalInvoice.query.filter_by(combination_id=combination_id).all()
    return render_template('invoice_view.html', combination=combination, invoices=invoices)


@app.route('/invoice/print/<int:combination_id>')
def print_invoice(combination_id):
    combination = InvoiceCombination.query.get_or_404(combination_id)
    invoices = FinalInvoice.query.filter_by(combination_id=combination_id).all()
    return render_template('invoice_print.html', combination=combination, invoices=invoices)


@app.route('/invoice/delete/<int:combination_id>', methods=['POST'])
@csrf_protect
def delete_invoice(combination_id):
    try:
        combination = InvoiceCombination.query.get_or_404(combination_id)
        db.session.delete(combination)
        db.session.commit()
        return jsonify({'success': True, 'message': '정산서가 삭제되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})


@app.route('/get_previous_readings/<int:floor_id>/<billing_month>')
def get_previous_readings(floor_id, billing_month):
    try:
        current_month = datetime.strptime(billing_month, '%Y-%m').date().replace(day=1)
        prev_bill = ElectricBill.query.filter(
            ElectricBill.floor_id == floor_id,
            ElectricBill.billing_month < current_month
        ).order_by(ElectricBill.billing_month.desc()).first()

        readings = {}
        if prev_bill:
            for r in prev_bill.readings:
                readings[r.unit_id] = float(r.current_reading)
        return jsonify({'success': True, 'readings': readings})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ======================================================
# Bootstrap / Defaults
# ======================================================
if __name__ == '__main__':
    init_database()
    with app.app_context():
        try:
            db.create_all()
            defaults = {
                'tv_fee': '2500',
                'electric_welfare_amount': '0',
                'electric_voucher_amount': '0',
                'water_welfare_amount': '0',
                'invoice_default_memo': '',
            }
            for k, v in defaults.items():
                if not Setting.query.filter_by(setting_key=k).first():
                    db.session.add(Setting(setting_key=k, setting_value=v))
            db.session.commit()
        except Exception as e:
            print(f"[bootstrap] Database initialization error: {e}")

    app.run(debug=True, host='0.0.0.0', port=5000)