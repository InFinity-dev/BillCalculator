"""입금 없는 완납 표시와 숨겨진 입금으로 인한 정산서 삭제 막힘을 검증한다."""

from datetime import date
from html.parser import HTMLParser

import pytest

import balance
from extensions import db
from models import FinalInvoice, InvoiceCombination, Payment


class DeleteButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.disabled = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'button' and attributes.get('id') == 'deleteSelectedInvoice':
            self.disabled = 'disabled' in attributes


@pytest.mark.parametrize('billed,paid,has_invoice,expected', [
    (0, 0, True, 'no_charge'), (1000, 0, True, 'unpaid'),
    (1000, 1000, True, 'paid'), (1000, 1500, True, 'credit'),
    (-500, 0, True, 'refund'), (0, 0, False, 'unlinked'),
])
def test_payment_status(billed, paid, has_invoice, expected):
    assert balance.payment_status(billed, paid, has_invoice=has_invoice) == expected


def test_no_payment_and_zero_invoice_are_not_paid(app, client, csrf, units, combination):
    unit = units[0]
    invoice = FinalInvoice.query.filter_by(combination_id=combination.id, unit_id=unit.id).one()
    invoice.electric_amount = invoice.water_amount = invoice.common_amount = invoice.total_amount = 0
    db.session.commit()
    single = client.get('/payments/balance/{}'.format(unit.id)).get_json()
    history = client.get('/payments/unit_history/{}'.format(unit.id)).get_json()
    report = client.get('/admin/validate_balances').get_json()['report']
    assert single['status'] == 'no_charge'
    assert history['history'][0]['status'] == 'no_charge'
    assert history['history'][0]['payments'] == []
    assert history['summary']['status'] == 'no_charge'
    assert next(item for item in report if item['unit_id'] == unit.id)['status'] == 'no_charge'
    # 공실을 포함해 한 번도 청구받지 않은 세대도 완납으로 표시하지 않는다.
    assert client.get('/payments/balance/{}'.format(units[-1].id)).get_json()['status'] == 'no_charge'
    assert client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()['success']


def test_other_units_payments_visible_and_delete_then_invoice_delete(app, client, csrf, units, combination):
    payment = Payment(combination_id=combination.id, unit_id=units[1].id,
                      payment_date=date(2026, 4, 5), payment_amount=17000, memo='다른 세대의 입금')
    db.session.add(payment)
    db.session.commit()
    identifier = payment.id
    first_unit = client.get('/payments/unit_history/{}'.format(units[0].id)).get_json()
    assert first_unit['history'][0]['payments'] == []
    blocked = client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()
    assert blocked['success'] is False
    assert blocked['payment_count'] == 1
    assert blocked['payments_url'] == '/payments?combination_id={}'.format(combination.id)
    html = client.get(blocked['payments_url']).get_data(as_text=True)
    assert '다른 세대의 입금' in html
    assert 'data-payment-id="{}"'.format(identifier) in html
    button = DeleteButtonParser()
    button.feed(html)
    assert button.disabled is True
    deleted = client.post('/payments/delete/{}'.format(identifier), json={'_csrf_token': csrf}).get_json()
    assert deleted['success'] is True
    button = DeleteButtonParser()
    button.feed(client.get(blocked['payments_url']).get_data(as_text=True))
    assert button.disabled is False
    assert client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()['success']
    assert db.session.get(InvoiceCombination, combination.id) is None


def test_zero_amount_payment_still_visible_and_removable(app, client, csrf, units, combination):
    payment = Payment(combination_id=combination.id, unit_id=units[0].id,
                      payment_date=date(2026, 4, 5), payment_amount=0, memo='0원 테스트 입금')
    db.session.add(payment)
    db.session.commit()
    identifier = payment.id
    assert client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()['success'] is False
    html = client.get('/payments?combination_id={}'.format(combination.id)).get_data(as_text=True)
    assert '0원 테스트 입금' in html
    assert 'data-payment-id="{}"'.format(identifier) in html
    assert client.post('/payments/delete/{}'.format(identifier), json={'_csrf_token': csrf}).get_json()['success']
    assert client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()['success']


def test_unlinked_legacy_payment_not_hidden_and_balances_agree(app, client, csrf, units, combination):
    # 레거시 데이터처럼 세대별 청구서 연결 없이 정산서·세대만 참조하는 입금.
    vacant = units[-1]
    payment = Payment(combination_id=combination.id, unit_id=vacant.id,
                      payment_date=date(2026, 4, 5), payment_amount=2500, memo='<legacy payment>')
    db.session.add(payment)
    db.session.commit()
    identifier = payment.id
    history = client.get('/payments/unit_history/{}'.format(vacant.id)).get_json()
    assert len(history['history']) == 1
    assert history['history'][0]['status'] == 'unlinked'
    assert history['history'][0]['has_invoice'] is False
    assert history['history'][0]['payments'][0]['id'] == identifier
    assert history['summary']['status'] == 'unlinked'
    single = client.get('/payments/balance/{}'.format(vacant.id)).get_json()
    assert single['status'] == 'unlinked'
    assert sum(item['balance'] for item in history['history']) == single['balance'] == -2500
    html = client.get('/payments?combination_id={}'.format(combination.id)).get_data(as_text=True)
    assert '이 입금과 연결된 세대별 청구서가 없습니다.' in html
    assert '&lt;legacy payment&gt;' in html
    assert '<legacy payment>' not in html
    assert client.post('/payments/delete/{}'.format(identifier), json={'_csrf_token': csrf}).get_json()['success']
    assert client.get('/payments/unit_history/{}'.format(vacant.id)).get_json()['history'] == []
    assert client.post('/invoice/delete/{}'.format(combination.id), json={'_csrf_token': csrf}).get_json()['success']


def test_payment_delete_requires_csrf_and_post(app, client, units, combination):
    payment = Payment(combination_id=combination.id, unit_id=units[0].id,
                      payment_date=date(2026, 4, 5), payment_amount=500)
    db.session.add(payment)
    db.session.commit()
    identifier = payment.id
    assert client.get('/payments/delete/{}'.format(identifier)).status_code == 405
    assert client.post('/payments/delete/{}'.format(identifier), json={}).get_json()['success'] is False
    assert db.session.get(Payment, identifier) is not None
