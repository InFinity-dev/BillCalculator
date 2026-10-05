"""세대별 잔액 계산 — 단일 정의.

기존에는 동일한 계산이 네 곳에 복붙되어 있었고
(``payment_unit_history`` / ``payment_balance`` / ``all_units_balance`` / ``validate_balances``),
float 과 Decimal 처리가 서로 달랐다.
(codebase-analysis 06-technical-debt.md H-3)

또한 이월 여부를 ``description`` 문자열에 '미납/초과납부/환급/이월' 이 들어있는지로
판정하고 있었다. 이제 ``FinalInvoiceCharge.is_carryover`` 컬럼만 본다.
(같은 문서 C-6)

보존되는 불변식
--------------
::

    billed(unit)  = Σ_정산 [ electric + water + common + Σ(is_carryover=0 인 기타 항목) ]
    paid(unit)    = Σ payments
    balance(unit) = billed − paid

이월 금액은 ``FinalInvoice.total_amount`` 에는 포함되지만(청구서에 찍힘)
위 ``billed`` 에는 포함되지 않는다(이중계상 방지).
판정 수단만 문자열 → 컬럼으로 바뀌었을 뿐 **결과 숫자는 기존과 동일**하다.
"""

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from extensions import db
from models import FinalInvoice, InvoiceCombination, Payment, Unit


def _billed_for_invoice(invoice):
    """정산 1건의 실제 고지액 (이월 항목 제외)."""
    return (
        invoice.electric_amount
        + invoice.water_amount
        + invoice.common_amount
        + invoice.billable_charges_total
    )


def payment_status(billed, paid, *, has_invoice=True):
    """잔액 0과 실제 완납을 구분한다. 청구 연결이 없는 입금은 먼저 확인한다."""
    if not has_invoice:
        return "unlinked"
    remaining = billed - paid
    if remaining > 0:
        return "unpaid"
    if remaining < 0:
        return "credit" if paid > 0 else "refund"
    return "paid" if paid > 0 else "no_charge"


def unlinked_payment_unit_ids(unit_id=None):
    query = db.session.query(Payment.unit_id).outerjoin(
        FinalInvoice,
        (FinalInvoice.combination_id == Payment.combination_id)
        & (FinalInvoice.unit_id == Payment.unit_id),
    ).filter(FinalInvoice.id.is_(None))
    if unit_id is not None:
        query = query.filter(Payment.unit_id == unit_id)
    return {identifier for (identifier,) in query.distinct().all()}


def _paid_totals_by_unit(unit_ids=None):
    """{unit_id: 납부 합계}. 세대별 개별 쿼리를 피하기 위한 일괄 집계."""
    query = db.session.query(
        Payment.unit_id, func.coalesce(func.sum(Payment.payment_amount), 0)
    ).group_by(Payment.unit_id)
    if unit_ids is not None:
        query = query.filter(Payment.unit_id.in_(list(unit_ids)))
    return {unit_id: int(total or 0) for unit_id, total in query.all()}


def _invoices_by_unit(unit_ids=None):
    """{unit_id: [FinalInvoice]}. charges 를 함께 로드해 N+1 을 막는다."""
    query = FinalInvoice.query.options(selectinload(FinalInvoice.charges))
    if unit_ids is not None:
        query = query.filter(FinalInvoice.unit_id.in_(list(unit_ids)))
    grouped = {}
    for invoice in query.all():
        grouped.setdefault(invoice.unit_id, []).append(invoice)
    return grouped


def unit_balance(unit_id):
    """세대 1개의 누적 잔액.

    Returns
    -------
    dict: ``{'total_billed': int, 'total_paid': int, 'balance': int}``
    """
    invoices = (
        FinalInvoice.query.options(selectinload(FinalInvoice.charges))
        .filter_by(unit_id=unit_id)
        .all()
    )
    total_billed = sum(_billed_for_invoice(inv) for inv in invoices)
    total_paid = int(
        db.session.query(func.coalesce(func.sum(Payment.payment_amount), 0))
        .filter(Payment.unit_id == unit_id)
        .scalar()
        or 0
    )
    return {
        "total_billed": int(total_billed),
        "total_paid": total_paid,
        "balance": int(total_billed) - total_paid,
    }


def all_unit_balances(units=None):
    """여러 세대의 잔액을 한 번에 계산한다.

    Parameters
    ----------
    units : list[Unit] | None
        None 이면 공실을 포함한 전체 세대. 과거 미납액은 공실 전환 후에도 남는다.

    Returns
    -------
    dict: ``{unit_id: {'unit_name', 'floor_name', 'total_billed', 'total_paid', 'balance'}}``
    """
    if units is None:
        units = Unit.query.all()
    unit_ids = [u.id for u in units]

    invoices_by_unit = _invoices_by_unit(unit_ids)
    paid_by_unit = _paid_totals_by_unit(unit_ids)

    result = {}
    for unit in units:
        invoices = invoices_by_unit.get(unit.id, [])
        total_billed = int(sum(_billed_for_invoice(inv) for inv in invoices))
        total_paid = paid_by_unit.get(unit.id, 0)
        result[unit.id] = {
            "unit_name": unit.unit_name,
            "floor_name": unit.floor.name if unit.floor else "",
            "total_billed": total_billed,
            "total_paid": total_paid,
            "balance": total_billed - total_paid,
            "invoice_count": len(invoices),
            "carryover_total": int(
                sum(inv.carryover_charges_total for inv in invoices)
            ),
        }
    return result


def unit_history(unit_id):
    """세대의 정산별 고지/납부 이력.

    Returns
    -------
    list[dict] — 정산 생성 순.
    """
    invoices = (
        FinalInvoice.query
        .options(selectinload(FinalInvoice.charges))
        .filter(FinalInvoice.unit_id == unit_id)
        .all()
    )
    invoices_by_combination = {invoice.combination_id: invoice for invoice in invoices}
    payments_by_combination = {}
    for payment in Payment.query.filter_by(unit_id=unit_id).order_by(Payment.payment_date, Payment.id).all():
        payments_by_combination.setdefault(payment.combination_id, []).append(payment)
    identifiers = set(invoices_by_combination) | set(payments_by_combination)
    combinations = InvoiceCombination.query.filter(InvoiceCombination.id.in_(identifiers)).order_by(
        InvoiceCombination.created_at, InvoiceCombination.id
    ).all()
    history = []
    for combination in combinations:
        invoice = invoices_by_combination.get(combination.id)
        payments = payments_by_combination.get(combination.id, [])
        total_paid = sum(p.payment_amount for p in payments)
        billed = int(_billed_for_invoice(invoice)) if invoice else 0

        history.append(
            {
                "combination_id": combination.id,
                "invoice_name": combination.invoice_name,
                "created_at": combination.created_at.strftime("%Y-%m-%d"),
                "billed_amount": billed,
                "paid_amount": int(total_paid),
                "balance": billed - int(total_paid),
                "has_invoice": invoice is not None,
                "status": payment_status(billed, total_paid, has_invoice=invoice is not None),
                "payments": [
                    {
                        "id": p.id,
                        "payment_date": p.payment_date.strftime("%Y-%m-%d"),
                        "payment_amount": p.payment_amount,
                        "payment_method": p.payment_method,
                        "memo": p.memo or "",
                    }
                    for p in payments
                ],
            }
        )
    return history
