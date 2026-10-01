import json
import ssl
import urllib.error
import urllib.request

import pytest

from woo_connector.connect import CallbackListener, authorize_url

INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE


def post(listener, payload, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(f"https://127.0.0.1:{listener.port}{listener.path}", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, context=INSECURE, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def keys(state, perms="read"):
    return {"key_id": 7, "user_id": state, "consumer_key": "ck_" + "a" * 40, "consumer_secret": "cs_" + "b" * 40,
            "key_permissions": perms}


def test_authorize_url_requires_https_callback():
    with pytest.raises(ValueError, match="https"):
        authorize_url("https://s.test", app_name="A", scope="read", state="s", return_url="http://x", callback_url="http://x")
    url = authorize_url("https://s.test/", app_name="Agent Studio", scope="read", state="s1",
                        return_url="https://cb.test/r", callback_url="https://cb.test/c")
    assert url.startswith("https://s.test/wc-auth/v1/authorize?") and "scope=read" in url and "user_id=s1" in url


def test_forged_and_malformed_callbacks_are_rejected_then_real_one_accepted():
    with CallbackListener() as lst:
        assert post(lst, keys("wrong-state"))[0] == 400
        assert post(lst, keys(lst.state, perms="read_write"))[0] == 400  # asked for read, got more
        assert post(lst, {}, raw=b"{not json")[0] == 400
        assert post(lst, {**keys(lst.state), "consumer_key": "nope"})[0] == 400
        assert post(lst, {}, raw=b"x" * 20000)[0] == 400
        assert lst.result is None and len(lst.rejections) == 4
        assert post(lst, keys(lst.state)) == (200, {"ok": True})
        assert post(lst, keys(lst.state))[0] == 400  # only one set of keys per attempt
        result = lst.wait(1)
    assert result.permissions == "read" and result.key_id == 7 and result.creds.consumer_key.startswith("ck_")


def test_each_attempt_has_its_own_state():
    with CallbackListener() as a, CallbackListener() as b:
        assert a.state != b.state and len(a.state) >= 32


def test_denied_return_is_detected():
    with CallbackListener() as lst:
        url = f"https://127.0.0.1:{lst.port}{lst.path}/return?success=0&user_id={lst.state}"
        with urllib.request.urlopen(url, context=INSECURE, timeout=5) as resp:
            assert resp.status == 200
        assert lst.denied and lst.wait(0.1) is None
