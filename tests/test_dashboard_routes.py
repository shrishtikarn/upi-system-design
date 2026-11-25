"""Dashboard routes: the gateway's /dashboard + /dashboard/api/* and the account
service's /admin/* endpoints, with internal services simulated."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from starlette.exceptions import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

import gateway.main as gw  # noqa: E402

calls: list[tuple[str, str, str, bytes]] = []
down: set[str] = set()


def internal(request: httpx.Request) -> httpx.Response:
    calls.append((request.url.host, request.method, str(request.url.path) + ("?" + request.url.query.decode() if request.url.query else ""), request.content))
    if request.url.host in down:
        raise httpx.ConnectError("refused", request=request)
    return httpx.Response(200, json={"ok": True, "from": request.url.host})


@pytest.fixture(autouse=True)
def fresh():
    calls.clear()
    down.clear()
    gw.state.http = httpx.AsyncClient(transport=httpx.MockTransport(internal))
    for node in gw.NODES:
        node.status = "UP"
    yield


def run(coro):
    return asyncio.run(coro)


def post_request(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/dashboard/api/chaos", "raw_path": b"/dashboard/api/chaos",
             "query_string": b"", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
             "server": ("gateway", 8080), "scheme": "http", "root_path": ""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_dashboard_page_is_served():
    resp = run(gw.dashboard())
    assert resp.status_code == 200 and b"UPI Payment System" in resp.body
    assert resp.headers["cache-control"] == "no-store"


def test_summary_and_transactions_are_forwarded_to_account_service():
    assert run(gw.dashboard_summary()).status_code == 200
    run(gw.dashboard_transactions(limit=500))
    assert calls[0][:3] == ("account-service", "GET", "/admin/summary")
    assert calls[1][2] == "/admin/transactions/recent?limit=100"  # clamped


def test_events_validates_the_transaction_id():
    assert run(gw.dashboard_transaction_events("../../etc")).status_code == 400
    txn = str(uuid.uuid4())
    assert run(gw.dashboard_transaction_events(txn)).status_code == 200
    assert calls[-1][2] == f"/admin/transactions/{txn}/events"


def test_chaos_only_accepts_known_modes_and_reaches_the_bank():
    assert run(gw.dashboard_set_chaos(post_request(b'{"mode":"explode"}'))).status_code == 400
    assert run(gw.dashboard_set_chaos(post_request(b"not json"))).status_code == 400
    resp = run(gw.dashboard_set_chaos(post_request(b'{"mode":"down"}')))
    assert resp.status_code == 200
    host, method, path, body = calls[-1]
    assert (host, method, path) == ("bank-mock", "POST", "/admin/chaos") and json.loads(body) == {"mode": "down"}


def test_pay_page_is_served():
    resp = run(gw.pay_page())
    assert resp.status_code == 200 and b"UPI Pay Console" in resp.body
    assert resp.headers["cache-control"] == "no-store"


def test_users_are_forwarded_with_escaped_query_and_clamped_limit():
    assert run(gw.dashboard_users(q="alice & bob", limit=5000)).status_code == 200
    assert calls[-1][:3] == ("account-service", "GET", "/admin/users?q=alice%20%26%20bob&limit=1100")


def test_unreachable_service_returns_503():
    down.add("account-service")
    resp = run(gw.dashboard_summary())
    assert resp.status_code == 503 and json.loads(resp.body)["error"] == "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------- account service /admin/*
class FakePool:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.txn = {"txn_id": uuid.uuid4(), "payer_vpa": "alice@okaxis", "payee_vpa": "bob@oksbi", "amount_paise": 15000,
                    "note": None, "status": "SUCCESS", "failure_reason": None, "bank_ref": "NPCI1", "attempts": 1,
                    "request_id": "abc", "created_at": now, "updated_at": now, "completed_at": now}
        self.events = [
            {"id": 1, "from_status": None, "to_status": "PENDING", "actor": "payment-service-1", "request_id": "abc",
             "detail": '{"source": "api"}', "created_at": now},
            {"id": 2, "from_status": "PROCESSING", "to_status": "SUCCESS", "actor": "worker-1", "request_id": "abc",
             "detail": '{"bank_ref": "NPCI1"}', "created_at": now},
        ]

    async def fetch(self, query, *args):
        import account_service.sql as s
        if query == s.STATUS_SUMMARY:
            return [{"status": "SUCCESS", "n": 3, "amount_paise": 300}, {"status": "FAILED", "n": 1, "amount_paise": 113}]
        if query == s.RECENT_TXNS:
            assert args == (5,)
            return [self.txn]
        if query == s.TXN_EVENTS:
            return self.events
        if query == s.USER_DIRECTORY:
            assert args == ("alice", 10)
            return [{"vpa": "alice@okaxis", "full_name": "Alice Sharma", "bank_name": "Axis Bank",
                     "balance_paise": 5000000, "is_active": True}]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        import account_service.sql as s
        if query == s.MONEY_SUMMARY:
            return {"total_paise": 100_000_000, "accounts": 3}
        if query == s.TXN_BY_ID:
            return self.txn if args[0] == self.txn["txn_id"] else None
        raise AssertionError(query)


@pytest.fixture()
def account():
    import account_service.main as acc
    acc.state.pool = FakePool()
    return acc


def test_admin_summary_totals(account):
    body = run(account.summary())
    assert body["total_transactions"] == 4 and body["by_status"]["FAILED"]["count"] == 1
    assert body["money_in_system"] == "1000000.00"


def test_admin_user_directory(account):
    body = run(account.user_directory(q="  ALICE ", limit=10))
    assert body["users"] == [{"vpa": "alice@okaxis", "name": "Alice Sharma", "bank": "Axis Bank", "active": True,
                              "balance": "50000.00", "balance_paise": 5000000}]


def test_admin_recent_and_events(account):
    recent = run(account.recent_transactions(limit=5))
    assert recent["transactions"][0]["amount"] == "150.00"
    txn_id = account.state.pool.txn["txn_id"]
    trail = run(account.transaction_events(txn_id))
    assert [e["to"] for e in trail["events"]] == ["PENDING", "SUCCESS"]
    assert trail["events"][1]["detail"] == {"bank_ref": "NPCI1"}
    with pytest.raises(HTTPException) as err:
        run(account.transaction_events(uuid.uuid4()))
    assert err.value.status_code == 404
