"""Smoke tests for empty screens, populated detail pages and their local assets."""
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from models import CommonBill
from tests.golden import dataset


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.heading_count = 0

    def handle_starttag(self, tag, attrs):
        names = [key for key, _ in attrs]
        assert len(names) == len(set(names)), 'Duplicate HTML attributes'
        attrs = dict(attrs)
        if tag == 'h1':
            self.heading_count += 1
        asset = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' else None
        if asset:
            path = urlsplit(asset).path
            if path.startswith('/static/'):
                self.assets.add(path)


def assert_page(client, route):
    response = client.get(route)
    assert response.status_code == 200, route
    parsed = AssetParser()
    parsed.feed(response.get_data(as_text=True))
    assert parsed.heading_count > 0, route
    assert parsed.assets, route
    for asset in parsed.assets:
        assert client.get(asset).status_code == 200, asset


@pytest.mark.parametrize('route', ['/', '/settings', '/calculator', '/view', '/invoice', '/payments'])
def test_empty_pages_and_assets(client, route):
    assert_page(client, route)


def test_populated_pages_and_invoice_variants(app, client, csrf):
    records = dataset.build(client, csrf)
    routes = ['/', '/settings', '/calculator', '/view', '/invoice', '/payments']
    routes.extend([
        '/view/electric/{}'.format(records['electric_single'].id),
        '/view/electric/{}'.format(records['electric_bundle'].id),
        '/view/water/{}'.format(records['water'].id),
        '/view/common/{}'.format(CommonBill.query.first().id),
    ])
    for invoice in records['invoices']:
        routes.extend(['/invoice/view/{}'.format(invoice.id), '/invoice/print/{}'.format(invoice.id)])
    for route in routes:
        assert_page(client, route)


def test_invoice_balance_action_uses_explicit_button_and_payment_allows_credit(client):
    invoice_html = client.get('/invoice').get_data(as_text=True)
    assert 'onclick="applyBalance(${unit.id}, ${balance.balance}, this)"' in invoice_html
    assert 'function applyBalance(unitId, balance, button)' in invoice_html
    assert 'const button = event.target' not in invoice_html

    payment_html = client.get('/payments').get_data(as_text=True)
    assert 'id="paymentAmount"' in payment_html
    assert '이번 정산서 잔액 입력' in payment_html
    assert '정산서 금액' in payment_html
    assert '이월 제외 신규 요금' in payment_html
    assert '전액 입력은 이번 정산서의 남은 금액만 채웁니다.' in payment_html
    assert 'modalOutstanding = payableAmount - otherPaid' in payment_html
    assert 'modalUnitOutstanding = currentUnitBalance + Number(editingAmount || 0)' in payment_html
    assert '이번 정산서 완납' in payment_html
    assert '세대 누적 완납' in payment_html
