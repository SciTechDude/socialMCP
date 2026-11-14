"""Optional local embeddings. Ollama by default; cloud is opt-in; 'none' is fine.

Local-first is the product promise, so the default provider talks to Ollama on
127.0.0.1 and nothing leaves the machine. If Ollama is not running, embedding
fails soft: contacts stay marked dirty and search falls back to FTS5 keyword
matching until the next successful pass.

Only stdlib HTTP is used, so there is no hard dependency on `requests`/`httpx`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

from ..config import EmbeddingConfig

log = logging.getLogger("socialmcp.embeddings")


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Local Ollama embeddings (default: nomic-embed-text, 768 dims)."""

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.dim = config.dim
        self.url = config.ollama_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            payload = {"model": self.config.model, "prompt": text or " "}
            data = _post_json(f"{self.url}/api/embeddings", payload, self.config.timeout_seconds)
            vector = data.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError(f"ollama returned no embedding for model {self.config.model!r}")
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"model {self.config.model!r} returned {len(vector)} dims but config says "
                    f"{self.dim}; set embedding.dim to {len(vector)} and reindex"
                )
            out.append([float(v) for v in vector])
        return out


class OpenAIEmbedder:
    """Opt-in cloud embeddings. Only used when explicitly configured.

    Note: contact names, companies and your clipboard notes would be sent to the
    provider. That is a deliberate, per-user decision — never the default.
    """

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.dim = config.dim
        self.api_key = os.environ.get(config.api_key_env, "")
        if not self.api_key:
            raise EmbeddingError(
                f"embedding.provider='openai' but ${config.api_key_env} is not set"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": [t or " " for t in texts],
            "dimensions": self.dim,
        }
        data = _post_json(
            "https://api.openai.com/v1/embeddings",
            payload,
            self.config.timeout_seconds,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [[float(v) for v in item["embedding"]] for item in items]


class EmbeddingError(RuntimeError):
    pass


def make_embedder(config: EmbeddingConfig) -> Embedder | None:
    provider = (config.provider or "none").lower()
    if provider in {"none", "off", "disabled"}:
        return None
    try:
        if provider == "ollama":
            return OllamaEmbedder(config)
        if provider == "openai":
            return OpenAIEmbedder(config)
    except EmbeddingError as exc:
        log.warning("embeddings disabled: %s", exc)
        return None
    log.warning("unknown embedding provider %r; embeddings disabled", provider)
    return None


def embedding_text(contact: dict[str, Any]) -> str:
    """Build the text that represents a contact in vector space.

    Field labels are included because they measurably help short-text embedding
    models: "Company: Stripe" retrieves better for "fintech" than a bare token.
    """
    parts = [f"Name: {contact.get('full_name') or 'Unknown'}"]
    if contact.get("position"):
        parts.append(f"Role: {contact['position']}")
    if contact.get("company"):
        parts.append(f"Company: {contact['company']}")
    if contact.get("headline"):
        parts.append(f"Headline: {contact['headline']}")
    if contact.get("location"):
        parts.append(f"Location: {contact['location']}")
    if contact.get("origin_note"):
        parts.append(f"How we met: {contact['origin_note']}")
    blob = (contact.get("search_blob") or "").strip()
    if blob:
        parts.append(f"Notes and messages: {blob[:2000]}")
    return "\n".join(parts)


def _post_json(
    url: str, payload: dict[str, Any], timeout: float, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise EmbeddingError(f"{url} unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"{url} returned non-JSON: {exc}") from exc
