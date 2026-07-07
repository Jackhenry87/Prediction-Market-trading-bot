"""Tests for Kalshi request signing and client basics. Run: pytest tests/"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import kalshi_client


@pytest.fixture()
def client(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_file = tmp_path / "test_key.pem"
    key_file.write_bytes(pem)
    return kalshi_client.KalshiClient("test-key-id", str(key_file), "demo")


def test_headers_present_and_signature_verifies(client):
    headers = client._headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()

    message = (
        headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/balance"
    ).encode()
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    # raises InvalidSignature on failure
    client.private_key.public_key().verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_env_selection(client):
    assert client.root == "https://demo-api.kalshi.co"
    with pytest.raises(kalshi_client.KalshiError):
        kalshi_client.KalshiClient("id", "nope.pem", "staging")


def test_private_key_pem_text_signs_like_the_file(tmp_path):
    """Hosts that store the key as secret text (Render, Actions) must get
    the same signing behavior as the .pem-file path."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    client = kalshi_client.KalshiClient(
        "test-key-id", None, "demo", private_key_pem=pem.decode())
    headers = client._headers("GET", "/trade-api/v2/portfolio/balance")
    message = (headers["KALSHI-ACCESS-TIMESTAMP"]
               + "GET" + "/trade-api/v2/portfolio/balance").encode()
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_order_body_v2_single_book_mapping(client, monkeypatch):
    captured = {}

    def fake_request(method, path, params=None, body=None):
        captured.update(method=method, path=path, body=dict(body))
        return {"order": {"order_id": "x", "status": "resting"}}

    monkeypatch.setattr(client, "_request", fake_request)

    # buy YES @ 37c -> bid at 0.3700
    client.create_limit_order("KXTEST-1", "yes", "buy", 10, 37)
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/events/orders"
    assert captured["body"]["side"] == "bid"
    assert captured["body"]["price"] == "0.3700"
    assert captured["body"]["count"] == "10"
    assert captured["body"]["client_order_id"]
    assert captured["body"]["time_in_force"] == "good_till_canceled"
    assert captured["body"]["self_trade_prevention_type"] == "taker_at_cross"

    # buy NO @ 37c -> ask at 0.6300 (complement)
    client.create_limit_order("KXTEST-1", "no", "buy", 10, 37)
    assert captured["body"]["side"] == "ask"
    assert captured["body"]["price"] == "0.6300"

    # sell YES @ 37c -> ask at 0.3700
    client.create_limit_order("KXTEST-1", "yes", "sell", 10, 37)
    assert captured["body"]["side"] == "ask"
    assert captured["body"]["price"] == "0.3700"
