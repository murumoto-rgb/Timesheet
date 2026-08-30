import pytest
from pydantic import ValidationError

import main


@pytest.mark.parametrize("endpoint", [
    "http://localhost/internal", "https://localhost/internal", "https://127.0.0.1/",
    "https://169.254.169.254/metadata", "https://example.com/push", "https://fcm.googleapis.com.evil.test/",
    "https://user:pass@fcm.googleapis.com/token", "https://fcm.googleapis.com:443/token",
    "https://fcm.googleapis.com/token#fragment", "https://fcm.googleapis.com/token#",
    "https://fcm.googleapis.com\\@evil.test/token", "https://fcm.googleapis.com\n/token",
    "https://push.services.mozilla.com.evil.test/token",
])
def test_push_endpoint_rejects_non_provider_targets(endpoint):
    with pytest.raises(ValidationError): main.PushSub(endpoint=endpoint)


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/token", "https://updates.push.services.mozilla.com/wpush/v2/token",
    "https://web.push.apple.com/Qtoken",
])
def test_push_known_provider_endpoint_is_accepted(endpoint):
    assert main.PushSub(endpoint=endpoint).endpoint == endpoint


def test_stored_push_targets_are_revalidated_and_redirects_never_followed(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_vapid_jwt", lambda endpoint: "test-jwt")
    monkeypatch.setattr(main, "_vapid", lambda: {"app_key": "test-key"})
    class Redirect:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/internal"}
    def post(endpoint, **kwargs):
        calls.append(endpoint)
        assert kwargs["allow_redirects"] is False
        return Redirect()
    monkeypatch.setattr(main.requests, "post", post)
    assert main._send_push({"endpoint": "http://127.0.0.1/internal"}) is False
    assert calls == []
    endpoint = "https://fcm.googleapis.com/fcm/send/token"
    assert main._send_push({"endpoint": endpoint}) is False
    assert calls == [endpoint]
