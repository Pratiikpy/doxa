"""Universal Artifact Contract — the single envelope every Doxa node returns.

Design goals (the moat): every output is structured, testable, reproducible,
traceable and independently verifiable. This module defines the typed shapes;
`runtime.py` produces and signs them.
"""
from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


SCHEMA_REQUEST = "doxa.request.v1"
SCHEMA_RESULT = "doxa.result.v1"


class VerificationLevel(str, enum.Enum):
    """Honest ladder — we never call everything 'verified'."""

    L1_PRODUCED = "L1_PRODUCED"          # node completed
    L2_VALIDATED = "L2_VALIDATED"        # schema + deterministic checks passed
    L3_REPRODUCED = "L3_REPRODUCED"      # rerun produced equivalent output
    L4_INDEPENDENT = "L4_INDEPENDENTLY_VERIFIED"  # a different engine corroborated it


class ErrorCode(str, enum.Enum):
    INVALID_INPUT = "INVALID_INPUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    FETCH_FAILED = "FETCH_FAILED"
    ENGINE_FAILED = "ENGINE_FAILED"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    QUALITY_FAILED = "QUALITY_FAILED"
    TIMEOUT = "TIMEOUT"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"


class ToolInfo(BaseModel):
    name: str
    version: str = "unknown"
    container_digest: str | None = None


class ArtifactRef(BaseModel):
    name: str
    mime_type: str = "application/octet-stream"
    bytes: int = 0
    sha256: str
    uri: str | None = None
    expires_at: str | None = None
    # An artifact the caller cannot actually obtain is a service that took payment and delivered
    # nothing. `uri` is an internal content address (artifact://<digest>) and is NOT fetchable, so
    # every ref also carries a resolvable HTTPS URL, and small artifacts are inlined so a single
    # paid call is self-contained — an agent gets the bytes without a second round trip.
    download_url: str | None = None
    content_b64: str | None = None
    content_inlined: bool = False


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    detail: str | None = None


class Validation(BaseModel):
    level: VerificationLevel = VerificationLevel.L1_PRODUCED
    status: str = "validated"  # validated | quality_failed | unverified
    tests: list[ValidationCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    independent_verifier: str | None = None
    differential_agreement: bool | None = None


class Lineage(BaseModel):
    parent_hashes: list[str] = Field(default_factory=list)
    workflow_run: str | None = None


class Provenance(BaseModel):
    # Truth vs provenance are kept separate on purpose.
    c2pa_manifest: str | None = None
    in_toto_attestation: dict | None = None
    sigstore_bundle: str | None = None
    opentimestamps: str | None = None
    prov_graph: dict | None = None


class ReplayCapsule(BaseModel):
    """Everything a *different* agent needs to reproduce this artifact."""

    environment: str = "python"
    command: str | None = None
    random_seed: int | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)
    parameters_sha256: str | None = None
    input_refs: list[str] = Field(default_factory=list)


class Metrics(BaseModel):
    duration_ms: int = 0
    queue_ms: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    cost_usdt: float = 0.0


class Receipt(BaseModel):
    manifest_sha256: str
    algo: str = "ed25519"
    signature: str  # hex
    public_key: str  # hex
    signed_at: str


class ArtifactError(BaseModel):
    code: ErrorCode
    message: str


class ArtifactEnvelope(BaseModel):
    schema_version: str = SCHEMA_RESULT
    ok: bool = True
    status: str = "completed"  # completed | accepted | failed
    request_id: str
    job_id: str
    endpoint: str
    result: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    input_hashes: list[str] = Field(default_factory=list)
    output_hash: str | None = None
    tool: ToolInfo | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    validation: Validation = Field(default_factory=Validation)
    lineage: Lineage = Field(default_factory=Lineage)
    provenance: Provenance = Field(default_factory=Provenance)
    replay: ReplayCapsule = Field(default_factory=ReplayCapsule)
    metrics: Metrics = Field(default_factory=Metrics)
    receipt: Receipt | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ArtifactError | None = None


class ArtifactRequest(BaseModel):
    schema_version: str = SCHEMA_REQUEST
    endpoint: str
    idempotency_key: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = None
