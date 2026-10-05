"""Smoke tests for empty screens, populated detail pages and their local assets."""
from html.parser import HTMLParser

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
        if asset and asset.startswith('/static/'):
            self.assets.add(asset)


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
