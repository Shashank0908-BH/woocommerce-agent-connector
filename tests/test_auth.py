import os
import stat

import pytest

from woo_connector.auth import (
    AuthConfigError, Credentials, choose_mode, load_credentials, oauth1_params, save_credentials,
)

# Computed by WooCommerce's own PHP (WC_REST_Authentication steps, WooCommerce 11.1.2)
# with the same inputs, so this checks against the store, not against ourselves.
PHP_SIGNATURE = "jlQptVzrdgBwn/eHIUThEPm5U028QAZBX7NwmW7aXXk="


def test_signature_matches_woocommerce_php():
    signed = oauth1_params(
        "GET", "https://shop.example.test/wp-json/wc/v3/orders",
        {"search": "diya iyer+co/~", "per_page": "5", "status": "processing,on-hold"},
        Credentials("ck_test", "cs_test"), timestamp=1700000000, nonce="abc123",
    )
    assert signed["oauth_signature"] == PHP_SIGNATURE


def test_query_string_in_url_is_not_signed_twice():
    a = oauth1_params("GET", "http://s.test/wp-json/wc/v3/orders?x=1", {}, Credentials("ck_a", "cs_a"), timestamp=1, nonce="n")
    b = oauth1_params("GET", "http://s.test/wp-json/wc/v3/orders", {}, Credentials("ck_a", "cs_a"), timestamp=1, nonce="n")
    assert a["oauth_signature"] == b["oauth_signature"]


def test_every_request_gets_a_fresh_nonce():
    creds = Credentials("ck_a", "cs_a")
    nonces = {oauth1_params("GET", "http://s.test/x", {}, creds)["oauth_nonce"] for _ in range(500)}
    assert len(nonces) == 500


def test_mode_selection():
    assert choose_mode("https://s.test") == "basic"
    assert choose_mode("http://s.test") == "oauth1"
    with pytest.raises(AuthConfigError, match="plain http"):
        choose_mode("http://s.test", "basic")
    assert choose_mode("http://s.test", "basic", allow_insecure_basic=True) == "basic"
    with pytest.raises(AuthConfigError):
        choose_mode("ftp://s.test")


def test_credentials_reject_garbage_and_hide_secret():
    with pytest.raises(AuthConfigError):
        Credentials("key", "secret")
    creds = Credentials("ck_" + "a" * 40, "cs_" + "b" * 40)
    assert "b" * 10 not in repr(creds) and "a" * 10 not in repr(creds)


def test_credential_file_is_private_and_keyed_by_store(tmp_path):
    path = tmp_path / "creds.json"
    save_credentials("https://a.test/", Credentials("ck_a", "cs_a"), "read", path)
    save_credentials("https://b.test", Credentials("ck_b", "cs_b"), "read", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    creds, perms = load_credentials("https://a.test", path)
    assert creds.consumer_key == "ck_a" and perms == "read"
    assert load_credentials("https://c.test", path) is None


def test_world_readable_credential_file_is_refused(tmp_path):
    path = tmp_path / "creds.json"
    save_credentials("https://a.test", Credentials("ck_a", "cs_a"), "read", path)
    os.chmod(path, 0o644)
    with pytest.raises(AuthConfigError, match="chmod 600"):
        load_credentials("https://a.test", path)
