"""Embedding backends.

Two options, deliberately:

* `HashingEmbedder` -- a deterministic, dependency-free hashed bag-of-words
  embedder. It always works: offline, with no model downloaded, and with
  identical output across runs (which makes retrieval testable). It is a
  lexical embedder, so it matches on shared vocabulary, not on paraphrase.
* `OllamaEmbedder` -- real semantic embeddings from a local Ollama model
  (default `nomic-embed-text`). Better retrieval, still fully on-device,
  but requires Ollama to be running.

Vectors are stored alongside the name of the embedder that produced them, so
switching embedders never mixes incompatible vector spaces: the store only
searches vectors whose embedder matches the active one.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

import httpx
import numpy as np

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
DEFAULT_DIM = 512

# Function words carry no retrieval signal and, in a lexical embedder, they
# dominate the vector: "what microphone do I have" would otherwise be mostly
# "what/do/i/have". Dropping them is what makes short questions match.
STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to for from by with
    about into over after before under again further is are was were be been being am do does
    did doing have has had having i me my we our you your he him his she her it its they them
    their what which who whom when where why how all any both each few more most other some
    such no nor not only own same so too very can will just should now would could shall may
    might must there here as s t don ll re ve d m o y ain aren couldn didn doesn hadn hasn
    haven isn ma mightn mustn needn shan shouldn wasn weren won wouldn""".split()
)


def tokenize(text: str) -> list[str]:
    """All word tokens, lowercased."""
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


def content_tokens(text: str) -> list[str]:
    """Tokens that carry meaning: stopwords and bare digits removed."""
    return [tok for tok in tokenize(text) if tok not in STOPWORDS and len(tok) > 1]


def _normalise(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return vector / norm


class Embedder(ABC):
    """Turns text into unit-length vectors."""

    name: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return a (len(texts), dim) float32 array of unit vectors."""

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class HashingEmbedder(Embedder):
    """Offline hashed bag-of-words with sublinear term frequency.

    Deterministic across processes: token hashing uses blake2b rather than
    Python's randomised `hash()`.
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim
        self.name = f"hashing-v1-{dim}"

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        # Second-to-last bit gives a stable +/- sign, reducing hash collisions.
        sign = 1.0 if (value >> 1) & 1 else -1.0
        return value % self.dim, sign

    #: Bigrams add a little word-order sensitivity, at half the weight of a
    #: unigram so they sharpen matches without drowning them out.
    BIGRAM_WEIGHT = 0.5

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = content_tokens(text)
            weights: dict[str, float] = {}
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                weights[token] = 1.0 + math.log(count)
            for first, second in zip(tokens, tokens[1:]):
                bigram = f"{first}_{second}"
                weights[bigram] = weights.get(bigram, 0.0) + self.BIGRAM_WEIGHT
            for token, weight in weights.items():
                index, sign = self._bucket(token)
                out[row, index] += sign * weight
            out[row] = _normalise(out[row])
        return out


class OllamaEmbedder(Embedder):
    """Semantic embeddings from a local Ollama model."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.name = f"ollama-{model}"
        self.dim = 0  # discovered on first call

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or DEFAULT_DIM), dtype=np.float32)
        response = httpx.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = payload.get("embeddings")
        if vectors is None and "embedding" in payload:  # older Ollama shape
            vectors = [payload["embedding"]]
        if not vectors:
            raise RuntimeError(f"Ollama returned no embeddings for model {self.model!r}")
        array = np.asarray(vectors, dtype=np.float32)
        self.dim = array.shape[1]
        return np.stack([_normalise(row) for row in array])

    def available(self) -> bool:
        try:
            self.embed(["ping"])
        except Exception:
            return False
        return True


def get_embedder(settings) -> Embedder:
    """Pick an embedder: Ollama if configured *and* reachable, else hashing."""
    if getattr(settings, "use_ollama_embeddings", False):
        candidate = OllamaEmbedder(
            base_url=settings.ollama_base_url, model=settings.ollama_embed_model
        )
        if candidate.available():
            return candidate
    return HashingEmbedder()
