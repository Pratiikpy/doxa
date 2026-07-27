"""x402 v2 for OKX.AI — the #1 review gate.

Builds the exact challenge the marketplace validates (PAYMENT-REQUIRED header,
base64 JSON, x402Version 2, network eip155:196, USDT0 asset, 6-decimal amount,
EIP-3009 `extra`), and verifies the X-PAYMENT header before replaying the call.

Two verify modes:
  - 'signature' (local/dev/test): the payer signs the challenge nonce; we verify
    the Ed25519 signature. Proves a real payment *authorization* without a chain.
  - 'facilitator' (production): delegate verify/settle to the OKX seller SDK /
    x402 facilitator on X Layer mainnet. (HTTP hook.)
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN

from config import Settings

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_HEADER = "X-PAYMENT"
PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"

# in-memory nonce store (single-use challenges). Production: Redis/Valkey.
_ISSUED: dict[str, dict] = {}


def fee_to_min_units(fee: str | float, decimals: int) -> str:
    """'0.01' + 6 decimals -> '10000' (string, integer min units)."""
    q = Decimal(str(fee)) * (Decimal(10) ** decimals)
    return str(int(q.quantize(Decimal(1), rounding=ROUND_DOWN)))


def build_challenge(endpoint: str, fee_usdt: str, settings: Settings, description: str,
                    input_contract: dict | None = None) -> tuple[str, dict]:
    """Return (base64_header_value, challenge_dict) for a paid endpoint."""
    nonce = uuid.uuid4().hex
    amount = fee_to_min_units(fee_usdt, settings.x402_asset_decimals)
    resource_url = f"{settings.public_base_url.rstrip('/')}/a2mcp/{endpoint}"
    challenge = {
        "x402Version": settings.x402_version,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
            # What the call needs. A buying agent reads the challenge to work out how to invoke the
            # service; without this it has nothing to go on, sends an empty body, pays, and gets an
            # error. That is exactly what happened on the first real A2A purchase of this service.
            **({"inputSchema": input_contract} if input_contract else {}),
        },
        "accepts": [
            {
                "scheme": settings.x402_scheme,          # "exact"
                "network": settings.x402_network,        # "eip155:196" (X Layer mainnet)
                "asset": settings.x402_asset,            # USDT0 on X Layer
                "amount": amount,                        # min units, 6 decimals
                "payTo": settings.pay_to,                # real X Layer receive wallet
                "maxTimeoutSeconds": settings.x402_max_timeout_seconds,
                # decimals MUST live inside `extra`: USDT0 is not in OKX's token list, and a
                # top-level `decimals` is dropped by OKX's canonical re-serialization.
                # (Confirmed by the live, x402-check-passing Aletheia/Reach challenges.)
                "extra": {"name": settings.x402_asset_name,
                          "version": settings.x402_asset_version,
                          "decimals": settings.x402_asset_decimals},
            }
        ],
        "nonce": nonce,
    }
    _ISSUED[nonce] = {"endpoint": endpoint, "amount": amount, "url": resource_url, "ts": time.time()}
    header_val = base64.b64encode(canonical(challenge)).decode("ascii")
    return header_val, challenge


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_challenge(header_value: str) -> dict:
    """Reviewer-side: PAYMENT-REQUIRED header base64 must decode to valid JSON."""
    return json.loads(base64.b64decode(header_value))


class PaymentResult:
    """The outcome of checking a payment, and — when it failed — *why*, in a form a caller can act on.

    `malformed` separates two failures a caller must handle differently. A header we cannot decode is
    the caller's bug: retrying is pointless until they fix how they build it, so it deserves a 400
    naming the field. A well-formed authorization we decline (spent nonce, short amount) is a payment
    problem: the caller should pay again, so it gets a 402 with a fresh challenge.
    """

    def __init__(self, ok: bool, detail: str, response_header: str | None = None,
                 code: str = "", field: str = "", malformed: bool = False):
        self.ok = ok
        self.detail = detail
        self.response_header = response_header
        self.code = code or ("" if ok else "payment_rejected")
        self.field = field
        self.malformed = malformed


def verify_payment(x_payment_header: str, settings: Settings,
                   endpoint: str = "", fee_usdt: str = "") -> PaymentResult:
    """Verify a payment authorization and, in facilitator mode, settle it on X Layer.

    Two payload shapes arrive here.

    A real x402 v2 authorization, produced by the OKX agentic wallet and sent in `PAYMENT-SIGNATURE`,
    looks like `{x402Version, resource, accepted, payload:{authorization, signature}}`. Its `nonce`
    lives in `payload.authorization` and is an EIP-3009 nonce belonging to the token contract — not
    the challenge nonce this server issued, so it cannot be looked up in `_ISSUED`.

    The dev shape, used by the offline tests, carries the challenge nonce at the top level.

    The distinction that matters for money: `paymentRequirements` sent to the facilitator is built
    from *this server's* price for *this endpoint*, never from the `accepted` block in the request.
    Echoing the caller's terms back would let anyone declare they owed a fraction of a cent and have
    the facilitator dutifully agree.
    """
    header_name = "PAYMENT-SIGNATURE"
    raw = (x_payment_header or "").strip()
    if not raw:
        return PaymentResult(False, f"the {header_name} header was empty.",
                             code="malformed_payment_signature", field=header_name, malformed=True)
    try:
        decoded = base64.b64decode(raw, validate=False)
    except Exception as e:  # noqa: BLE001
        return PaymentResult(
            False,
            f"the {header_name} header is not valid base64 ({e}). It must be the base64 encoding of "
            f"the x402 v2 payment payload returned by your wallet.",
            code="malformed_payment_signature", field=header_name, malformed=True)
    try:
        payload = json.loads(decoded)
    except Exception as e:  # noqa: BLE001
        return PaymentResult(
            False,
            f"the {header_name} header decoded, but not to JSON ({e}). Expected a JSON object with "
            f"'x402Version', 'accepted' and 'payload'.",
            code="malformed_payment_signature", field=header_name, malformed=True)
    if not isinstance(payload, dict):
        return PaymentResult(
            False,
            f"the {header_name} header decoded to {type(payload).__name__}, not a JSON object.",
            code="malformed_payment_signature", field=header_name, malformed=True)

    inner = payload.get("payload")
    is_x402_v2 = isinstance(inner, dict) and "authorization" in inner
    if is_x402_v2:
        return _verify_x402_v2(payload, settings, endpoint, fee_usdt)

    # --- dev / signature mode -------------------------------------------------------------------
    nonce = payload.get("nonce")
    if not nonce or nonce not in _ISSUED:
        return PaymentResult(False, "unknown or expired challenge nonce")
    issued = _ISSUED[nonce]

    if payload.get("scheme") != settings.x402_scheme:
        return PaymentResult(False, "scheme mismatch")
    if payload.get("network") != settings.x402_network:
        return PaymentResult(False, "network mismatch (must be X Layer eip155:196)")
    if str(payload.get("amount")) != issued["amount"]:
        return PaymentResult(False, "amount mismatch vs challenge")
    if settings.x402_mode == "facilitator":
        return PaymentResult(False, "this endpoint settles on X Layer; send a signed x402 v2 "
                                    "authorization in the PAYMENT-SIGNATURE header")

    ok, detail = _verify_signature(payload, nonce)
    if not ok:
        return PaymentResult(False, detail)

    _ISSUED.pop(nonce, None)  # single-use → prevents replay of the same payment
    resp = base64.b64encode(canonical({"success": True, "nonce": nonce,
                                       "settledAt": time.time()})).decode("ascii")
    return PaymentResult(True, "verified", resp)


def _verify_x402_v2(payload: dict, settings: Settings, endpoint: str,
                    fee_usdt: str) -> PaymentResult:
    """Check a real authorization against our own terms, then settle it."""
    accepted = payload.get("accepted") or {}
    auth = (payload.get("payload") or {}).get("authorization") or {}
    resource = payload.get("resource") or {}

    # Our price for this endpoint, in minor units. Server-side truth.
    required_amount = fee_to_min_units(fee_usdt or "0", settings.x402_asset_decimals)
    expected_url = f"{settings.public_base_url.rstrip('/')}/a2mcp/{endpoint}" if endpoint else ""

    # The authorization must be for this resource. Without this check an authorization bought for
    # the cheapest endpoint would unlock the most expensive one.
    if expected_url and resource.get("url") and resource["url"] != expected_url:
        return PaymentResult(False, f"this authorization is for {resource['url']}, not {expected_url}")
    if str(accepted.get("network") or "") != settings.x402_network:
        return PaymentResult(False, "network mismatch (must be X Layer eip155:196)")
    if (accepted.get("asset") or "").lower() != settings.x402_asset.lower():
        return PaymentResult(False, "asset mismatch")
    if (accepted.get("payTo") or "").lower() != settings.pay_to.lower():
        return PaymentResult(False, "payTo mismatch")
    try:
        if int(auth.get("value") or 0) < int(required_amount):
            return PaymentResult(False, f"authorised {auth.get('value')} minor units, "
                                        f"{required_amount} required")
    except (TypeError, ValueError):
        return PaymentResult(False, "unreadable authorised value")

    # An EIP-3009 nonce is single-use at the token contract, but a replay within the validity window
    # would still cost us the work twice before the chain rejects it. Refuse it here as well.
    auth_nonce = auth.get("nonce")
    if auth_nonce:
        if auth_nonce in _SPENT:
            return PaymentResult(False, "this authorization has already been used")

    requirements = {
        "scheme": settings.x402_scheme,
        "network": settings.x402_network,
        "asset": settings.x402_asset,
        "amount": required_amount,
        "payTo": settings.pay_to,
        "maxTimeoutSeconds": settings.x402_max_timeout_seconds,
        "resource": expected_url or settings.public_base_url,
        "extra": {"name": settings.x402_asset_name,
                  "version": settings.x402_asset_version,
                  "decimals": settings.x402_asset_decimals},
    }

    if settings.x402_mode != "facilitator":
        return PaymentResult(False, "x402 settlement is not enabled on this deployment")

    ok, detail, tx = _settle_via_facilitator(payload, requirements, settings)
    if not ok:
        return PaymentResult(False, detail)
    if auth_nonce:
        _SPENT.add(auth_nonce)
    resp = base64.b64encode(canonical({"success": True, "network": settings.x402_network,
                                       "transaction": tx,
                                       "settledAt": time.time()})).decode("ascii")
    return PaymentResult(True, detail, resp)


def _settle_via_facilitator(payment_payload: dict, requirements: dict,
                            settings: Settings) -> tuple[bool, str, str]:
    """Verify then settle through OKX. Fail closed: no settlement, no service."""
    if not settings.facilitator_configured:
        return False, ("settlement is unavailable: OKX facilitator credentials are not configured",
                       "")[0], ""
    try:
        from okx_facilitator import FacilitatorError, OkxFacilitator
    except Exception as e:  # noqa: BLE001
        return False, f"facilitator client unavailable: {e}", ""

    fac = OkxFacilitator(settings.okx_api_key, settings.okx_api_secret,
                         settings.okx_api_passphrase,
                         base_url=settings.x402_facilitator_url or settings.okx_facilitator_base_url,
                         sync_settle=settings.okx_sync_settle)
    try:
        v = fac.verify(payment_payload, requirements)
    except FacilitatorError as e:
        return False, f"facilitator verify failed: {e}", ""
    if not (v.get("isValid") is True or v.get("valid") is True):
        return False, f"facilitator rejected the authorization: {v.get('invalidReason') or v}", ""

    try:
        s = fac.settle(payment_payload, requirements)
    except FacilitatorError as e:
        return False, f"facilitator settle failed: {e}", ""
    tx = str(s.get("transaction") or s.get("txHash") or "")
    settled = s.get("success") is True or bool(tx)
    if not settled:
        return False, f"settlement did not complete: {str(s)[:180]}", ""
    return True, f"settled on {settings.x402_network}" + (f" tx {tx}" if tx else ""), tx


# EIP-3009 nonces already spent through this process. The token contract is the real defence; this
# stops a replay inside the validity window from costing us the work before the chain rejects it.
_SPENT: set[str] = set()


def _verify_signature(payload: dict, nonce: str) -> tuple[bool, str]:
    """Dev/test: payer proves authorization by signing the nonce (Ed25519)."""
    pub = payload.get("payer_pubkey")
    sig = payload.get("signature")
    if not pub or not sig:
        return False, "missing payer_pubkey/signature"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(sig), nonce.encode())
        return True, "signature ok"
    except Exception as e:
        return False, f"bad signature: {e}"


def _verify_via_facilitator(payload: dict, settings: Settings) -> tuple[bool, str]:
    """Production verify+settle through the official OKX x402 facilitator on X Layer."""
    if not settings.facilitator_configured:
        return False, ("facilitator mode requires OKX_API_KEY / OKX_API_SECRET / "
                       "OKX_API_PASSPHRASE (not configured)")
    try:
        from okx_facilitator import OkxFacilitator, FacilitatorError
    except Exception as e:  # noqa
        return False, f"facilitator client unavailable: {e}"

    issued = _ISSUED.get(payload.get("nonce"), {})
    payment_requirements = {
        "scheme": settings.x402_scheme,
        "network": settings.x402_network,
        "asset": settings.x402_asset,
        "amount": issued.get("amount") or str(payload.get("amount")),
        "payTo": settings.pay_to,
        "maxTimeoutSeconds": settings.x402_max_timeout_seconds,
        "resource": issued.get("url") or settings.public_base_url,
        "extra": {"name": settings.x402_asset_name, "version": settings.x402_asset_version},
    }
    try:
        fac = OkxFacilitator(
            settings.okx_api_key, settings.okx_api_secret, settings.okx_api_passphrase,
            base_url=settings.x402_facilitator_url or settings.okx_facilitator_base_url,
            sync_settle=settings.okx_sync_settle,
        )
        v = fac.verify(payload, payment_requirements)
        if not (v.get("isValid") is True or v.get("valid") is True or v.get("success") is True):
            return False, f"facilitator verify rejected: {str(v)[:160]}"
        s = fac.settle(payload, payment_requirements)
        ok = (s.get("success") is True or s.get("settled") is True
              or bool(s.get("txHash") or s.get("transaction")))
        return (True, f"settled {s.get('txHash') or ''}".strip()) if ok else \
               (False, f"facilitator settle failed: {str(s)[:160]}")
    except FacilitatorError as e:
        return False, f"facilitator error: {e}"
    except Exception as e:  # noqa
        return False, f"facilitator unexpected error: {type(e).__name__}: {e}"


# ---- test helper: forge a valid dev payment for a given challenge (used by self-probe/tests)
def make_dev_payment(challenge: dict) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    nonce = challenge["nonce"]
    accept = challenge["accepts"][0]
    payload = {
        "x402Version": challenge["x402Version"],
        "scheme": accept["scheme"],
        "network": accept["network"],
        "asset": accept["asset"],
        "amount": accept["amount"],
        "nonce": nonce,
        "payer_pubkey": sk.public_key().public_bytes_raw().hex(),
        "signature": sk.sign(nonce.encode()).hex(),
    }
    return base64.b64encode(canonical(payload)).decode("ascii")
