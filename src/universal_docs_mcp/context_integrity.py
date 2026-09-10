"""Bind selected UTF-8 bytes to the retriever's declared document identity.

This detects inconsistent/tampered envelopes. It is deliberately not a
signature or an independent authentication of a publisher or trusted process.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SOURCE_AUTHENTICATION = "trusted_retriever_not_publisher_authenticated"


def context_integrity(context: str, receipt: Mapping[str, Any]) -> dict[str, str]:
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    projection = {
        "target": receipt["target"],
        "source": receipt["source"],
        "context_sha256": context_hash,
    }
    encoded = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "context_sha256": context_hash,
        "binding_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_authentication": SOURCE_AUTHENTICATION,
    }
