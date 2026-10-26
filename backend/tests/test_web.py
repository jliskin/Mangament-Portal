"""HTTP-level tests: auth, CSRF, every page renders, 1-click reconciliation, webhook."""
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import BankTransaction, User
from app.security import hash_password
from app.seed import demo_statement
from app.services import bank_feed


@pytest.fixture
def client(db):
    db.add(User(email="admin@example.com", full_name="Admin", role="admin", password_hash=hash_password("x" * 12)))
    db.add(User(email="viewer@example.com", full_name="Viewer", role="viewer", password_hash=hash_password("y" * 12)))
    db.commit()
    from app.main import create_app
    return TestClient(create_app())


def _csrf(c, url="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)


def login(c, email="admin@example.com", pw="x" * 12):
    r = c.post("/login", data={"email": email, "password": pw, "csrf": _csrf(c)}, follow_redirects=False)
    assert r.status_code == 303
    return re.search(r'"X-CSRF-Token": "([^"]+)"', c.get("/").text).group(1)


def test_requires_login_and_rejects_bad_password(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login", data={"email": "admin@example.com", "password": "wrong", "csrf": _csrf(client)})
    assert r.status_code == 401


def test_csrf_required_on_post(client):
    login(client)
    assert client.post("/bank/rematch").status_code == 403


@pytest.mark.parametrize("url", ["/", "/units", "/units/new", "/tenants", "/tenants/new", "/leases", "/leases/new",
                                 "/leases/1", "/charges", "/bank", "/bank/debits", "/expenses", "/expenses/new",
                                 "/reports", "/reports/aging.csv", "/reports/pnl.csv", "/settings", "/tenants/1"])
def test_pages_render(client, url):
    login(client)
    r = client.get(url)
    assert r.status_code == 200, r.text[:500]
    assert "Content-Security-Policy" in r.headers


def test_viewer_cannot_mutate(client):
    tok = login(client, "viewer@example.com", "y" * 12)
    assert client.get("/units/new").status_code == 403
    assert client.post("/bank/rematch", headers={"X-CSRF-Token": tok}).status_code == 403


def test_import_and_one_click_confirm(client, db):
    tok = login(client)
    r = client.post("/bank/import", data={"bank_account_id": "1", "csrf": tok},
                    files={"file": ("extracto.csv", demo_statement(), "text/csv")})
    assert r.status_code == 200 and "Importadas 10 filas" in r.text
    t = db.scalar(select(BankTransaction).where(BankTransaction.description.contains("CM-101")))
    lease_id = t.suggestions[0].lease_id
    r = client.post(f"/bank/txn/{t.id}/confirm", data={"lease_id": str(lease_id)},
                    headers={"X-CSRF-Token": tok, "HX-Request": "true"})
    assert r.status_code == 200 and "Conciliado" in r.text and r.headers["HX-Trigger"] == "reconciled"


def test_webhook_signature(client, db):
    body = json.dumps({"bank_account_id": 1, "transactions": [
        {"external_id": "abc-1", "date": "2026-10-03", "amount": "1630.00",
         "description": "SEPA CREDIT CM-101", "counterparty": "Jose Perez"}]}).encode()
    assert client.post("/api/bank-feed/webhook", content=body, headers={"x-signature": "t=1,v1=bad"}).status_code == 401
    sig = bank_feed.sign("test-webhook-secret", body)
    r = client.post("/api/bank-feed/webhook", content=body, headers={"x-signature": sig})
    assert r.json() == {"received": 1, "inserted": 1}
    r = client.post("/api/bank-feed/webhook", content=body, headers={"x-signature": bank_feed.sign("test-webhook-secret", body)})
    assert r.json()["inserted"] == 0   # idempotent by external id
