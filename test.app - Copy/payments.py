
import os
import hmac
import hashlib
import requests

PAYMONGO_API = "https://api.paymongo.com/v1"
SECRET_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")

# Set False when PayMongo isn't configured yet — app falls back to the
# old instant-"paid" placeholder behavior instead of crashing.
PAYMONGO_ENABLED = bool(SECRET_KEY)

SERVICE_FEE_CENTAVOS = 0  # add a convenience fee here if you ever want one


def _auth():
    return (SECRET_KEY, "")   # PayMongo uses basic auth: secret key as username


def create_checkout_session(line_items, batch_id, description,
                            success_url, cancel_url):
    """
    line_items: [{"name": ..., "amount": centavos(int), "quantity": 1}, ...]
    Returns the PayMongo checkout URL the user should be redirected to.
    Raises RuntimeError with a readable message on failure.
    """
    for item in line_items:
        item.setdefault("currency", "PHP")

    attributes = {
        "send_email_receipt": False,
        "show_description": True,
        "show_line_items": True,
        "description": description,
        "line_items": line_items,
        "payment_method_types": ["gcash", "paymaya", "card"],
        "reference_number": batch_id,          # <-- we look the batch up by this
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"batch_id": batch_id},    # also in metadata as backup
    }
    resp = requests.post(
        f"{PAYMONGO_API}/checkout_sessions",
        auth=_auth(),
        json={"data": {"attributes": attributes}},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"PayMongo rejected checkout: {resp.status_code} {resp.text}")

    data = resp.json()["data"]
    return {
        "checkout_id": data["id"],
        "checkout_url": data["attributes"]["checkout_url"],
        "payment_intent_id": data["attributes"].get("payment_intent_id", ""),
    }


def verify_webhook(raw_body: bytes, signature_header: str) -> bool:
    """
    PayMongo-Signature format:  t=<ts>,te1=<hex>,li1=<hex> (or te=...,li=...)
    Signed payload is:  f"{timestamp}.{raw_body.decode()}"
    """
    if not WEBHOOK_SECRET:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        timestamp = parts["t"]
        expected = hmac.new(
            WEBHOOK_SECRET.encode(),
            f"{timestamp}.{raw_body.decode()}".encode(),
            hashlib.sha256,
        ).hexdigest()
        # test signature first, then live — accept either
        for key in ("te1", "te2", "te", "li1", "li2", "li"):
            if parts.get(key) and hmac.compare_digest(parts[key], expected):
                return True
        return False
    except Exception:
        return False