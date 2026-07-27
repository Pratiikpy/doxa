"""Doxa configuration. Loads from environment / .env (never hard-codes secrets)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dep). Only sets vars not already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


class Settings:
    """Runtime settings. Secrets are read by name; values are never logged."""

    def __init__(self) -> None:
        self.llm_base_url: str = os.environ.get("LLM_BASE_URL", "https://router-api.0g.ai/v1")
        self.llm_model: str = os.environ.get("LLM_MODEL", "minimax-m3")
        # Ordered models to try when the configured one cannot serve. The router gates availability
        # PER MODEL (403 BALANCE_INSUFFICIENT), not per account: a sibling ASP's paid endpoint returned
        # 500 to an already-charged caller purely because its single model was gated while others on the
        # same key answered fine. Every name here was verified to answer on this account.
        self.llm_model_chain: list[str] = [
            m.strip() for m in os.environ.get(
                "LLM_MODEL_CHAIN",
                f"{os.environ.get('LLM_MODEL', 'minimax-m3')},glm-5.2,deepseek-v4-pro,qwen3.7-max,hy3",
            ).split(",") if m.strip()
        ]
        # secret: referenced by name only
        self._llm_api_key: str | None = os.environ.get("MINIMAX_API_KEY") or os.environ.get("LLM_API_KEY")
        self.signing_key_path: str = os.environ.get("DOXA_SIGNING_KEY", ".secrets/doxa_ed25519.key")
        # secret: the receipt signing identity, supplied as 64 hex chars. Set this in production —
        # without it the key is generated into the container filesystem and every redeploy or extra
        # replica signs under a different identity, so receipts stop being attributable to Doxa.
        self._signing_key_hex: str | None = os.environ.get("DOXA_SIGNING_KEY_HEX") or None
        self.artifact_dir: str = os.environ.get("DOXA_ARTIFACT_DIR", ".artifacts")
        self.artifact_ttl_seconds: int = int(os.environ.get("DOXA_ARTIFACT_TTL", "86400"))
        self.max_input_bytes: int = int(os.environ.get("DOXA_MAX_INPUT_BYTES", str(100 * 1024 * 1024)))
        # ---- x402 / OKX X Layer settlement (exact constants; override payTo + base url before submit)
        self.x402_version: int = 2
        self.x402_scheme: str = "exact"
        self.x402_network: str = os.environ.get("DOXA_NETWORK", "eip155:196")  # X Layer mainnet
        self.x402_asset: str = os.environ.get("DOXA_ASSET", "0x779ded0c9e1022225f8e0630b35a9b54be713736")  # USDT0
        self.x402_asset_name: str = os.environ.get("DOXA_ASSET_NAME", "USD₮0")
        self.x402_asset_version: str = os.environ.get("DOXA_ASSET_VERSION", "1")
        self.x402_asset_decimals: int = int(os.environ.get("DOXA_ASSET_DECIMALS", "6"))
        self.x402_max_timeout_seconds: int = int(os.environ.get("DOXA_X402_TIMEOUT", "300"))
        # payTo MUST be a real X Layer receive wallet before submitting for review.
        # Accept the same names the proven Aletheia/Reach deployments use.
        self.pay_to: str = (os.environ.get("DOXA_PAYTO")
                            or os.environ.get("X402_PAY_TO")
                            or os.environ.get("OKX_PAY_TO")
                            or "0x0000000000000000000000000000000000000000")
        # public https base URL the ASP is registered under (resource.url).
        self.public_base_url: str = os.environ.get("DOXA_PUBLIC_BASE_URL", "https://api.doxa.example")
        # x402 verification mode: 'signature' (local/dev cryptographic auth) | 'facilitator' (prod OKX SDK)
        self.x402_mode: str = os.environ.get("DOXA_X402_MODE", "signature")
        self.x402_facilitator_url: str | None = os.environ.get("DOXA_FACILITATOR_URL")
        # OKX facilitator (production settlement on X Layer)
        self.okx_facilitator_base_url: str = os.environ.get(
            "OKX_FACILITATOR_BASE_URL", "https://web3.okx.com")
        self.okx_sync_settle: bool = os.environ.get("OKX_SYNC_SETTLE", "true").lower() == "true"
        # secrets: referenced by name only, never logged.
        # Accept BOTH our names and the proven Aletheia/Reach names.
        self._okx_api_key: str | None = os.environ.get("OKX_API_KEY")
        self._okx_api_secret: str | None = (os.environ.get("OKX_API_SECRET")
                                           or os.environ.get("OKX_SECRET_KEY"))
        self._okx_api_passphrase: str | None = (os.environ.get("OKX_API_PASSPHRASE")
                                               or os.environ.get("OKX_PASSPHRASE"))
        # X402_ENABLED=1 (Aletheia convention) forces facilitator mode when creds are present.
        if os.environ.get("X402_ENABLED") == "1" and all(
                [self._okx_api_key, self._okx_api_secret, self._okx_api_passphrase]):
            self.x402_mode = "facilitator"

    @property
    def okx_api_key(self) -> str | None:
        return self._okx_api_key

    @property
    def okx_api_secret(self) -> str | None:
        return self._okx_api_secret

    @property
    def okx_api_passphrase(self) -> str | None:
        return self._okx_api_passphrase

    @property
    def facilitator_configured(self) -> bool:
        return bool(self._okx_api_key and self._okx_api_secret and self._okx_api_passphrase)

    @property
    def llm_api_key(self) -> str | None:
        return self._llm_api_key

    @property
    def llm_configured(self) -> bool:
        return bool(self._llm_api_key)

    def redacted(self) -> dict:
        """Safe-to-log view; secret presence only, never the value."""
        return {
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_configured": self.llm_configured,
            "artifact_dir": self.artifact_dir,
            "artifact_ttl_seconds": self.artifact_ttl_seconds,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
