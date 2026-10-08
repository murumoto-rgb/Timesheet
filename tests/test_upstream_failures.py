"""Provider failures stay diagnosable without leaking secrets or losing tokens."""
import pytest
import requests
from fastapi import HTTPException
import main

@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    monkeypatch.setattr(main, "token_url", lambda: "https://synthetic.invalid/token")


def broken_request(*args, **kwargs):
    raise requests.Timeout("private-token-and-url")


def test_query_transport_error_is_safe_and_correlated(monkeypatch, caplog):
    monkeypatch.setattr(main, "get_access_token", lambda: ("secret", "realm"))
    monkeypatch.setattr(main.requests, "get", broken_request)
    with pytest.raises(HTTPException) as exc:
        main.qbo_query("SELECT * FROM TimeActivity")
    assert exc.value.status_code == 502
    assert exc.value.detail["supportId"] in caplog.text
    assert "query_transport" in caplog.text
    assert "private-token-and-url" not in caplog.text


def test_temporary_refresh_failure_preserves_connection(monkeypatch):
    tokens = {"access_token": "old", "refresh_token": "keep", "access_expires_at": 0, "realm_id": "synthetic"}
    monkeypatch.setattr(main, "_load_tokens", lambda: dict(tokens))
    monkeypatch.setattr(main, "_save_tokens", lambda _: pytest.fail("failure must not overwrite tokens"))
    monkeypatch.setattr(main.requests, "post", broken_request)
    with pytest.raises(HTTPException) as exc:
        main.get_access_token()
    assert exc.value.status_code == 502
    assert tokens["refresh_token"] == "keep"


@pytest.mark.parametrize("token", [False, True])
def test_bad_provider_json_is_safe(monkeypatch, caplog, token):
    class BadResponse:
        status_code = 200
        def json(self):
            raise ValueError("private-response")
    monkeypatch.setattr(main, "get_access_token", lambda: ("secret", "realm"))
    monkeypatch.setattr(main.requests, "post" if token else "get", lambda *a, **kw: BadResponse())
    with pytest.raises(HTTPException) as exc:
        if token:
            main._token_request({"grant_type": "refresh_token"})
        else:
            main.qbo_query("SELECT * FROM TimeActivity")
    assert exc.value.status_code == 502
    assert "private-response" not in caplog.text


def test_reconciliation_unexpected_failure_is_safe_and_correlated(monkeypatch, caplog):
    monkeypatch.setattr(main, "_write_scope", lambda: "test|realm")
    def fail(*a, **kw): raise RuntimeError("private-report-data")
    monkeypatch.setattr(main, "qbo_query_all", fail)
    with pytest.raises(HTTPException) as exc:
        main.reconciliation("2026-10-06", "2026-10-06")
    assert exc.value.status_code == 500
    assert exc.value.detail["supportId"] in caplog.text
    assert "stage=query" in caplog.text
    assert "private-report-data" not in caplog.text


@pytest.mark.parametrize("body", [{}, {"QueryResponse": None}])
def test_missing_query_response_fails_closed(monkeypatch, body):
    class Response:
        status_code = 200
        def json(self): return body
    monkeypatch.setattr(main, "get_access_token", lambda: ("secret", "realm"))
    monkeypatch.setattr(main.requests, "get", lambda *a, **kw: Response())
    with pytest.raises(HTTPException) as exc: main.qbo_query("SELECT * FROM TimeActivity")
    assert exc.value.status_code == 502


@pytest.mark.parametrize("rows", [None, {}, "private", ["private"]])
def test_bad_entity_rows_fail_closed(monkeypatch, rows):
    monkeypatch.setattr(main, "qbo_query", lambda _: {"TimeActivity": rows})
    with pytest.raises(HTTPException) as exc: main.qbo_query_all("TimeActivity")
    assert exc.value.status_code == 502


def test_authorization_exchange_requires_refresh_token(monkeypatch):
    class Response:
        status_code = 200
        def json(self): return {"access_token": "synthetic", "expires_in": 3600}
    monkeypatch.setattr(main.requests, "post", lambda *a, **kw: Response())
    with pytest.raises(HTTPException) as exc:
        main._token_request({"grant_type": "authorization_code"})
    assert exc.value.status_code == 502


@pytest.mark.parametrize("status", [408, 429, 503])
def test_transient_token_response_requests_retry(monkeypatch, status):
    class Response:
        status_code = status
        headers = {"intuit_tid": "synthetic-support"}
    monkeypatch.setattr(main.requests, "post", lambda *a, **kw: Response())
    with pytest.raises(HTTPException) as exc: main._token_request({"grant_type": "refresh_token"})
    assert exc.value.status_code == status
    assert "Retry" in exc.value.detail["message"]


def test_qbo_validation_detail_is_not_logged(caplog):
    class Response:
        status_code = 400
        headers = {"intuit_tid": "synthetic-support"}
        def json(self): return {"Fault": {"Error": [{"code": "6000", "Detail": "private-validation-detail"}]}}
    error = main._qbo_error(Response())
    assert error.detail["message"] == "private-validation-detail"
    assert "private-validation-detail" not in caplog.text
    assert "6000" in caplog.text
