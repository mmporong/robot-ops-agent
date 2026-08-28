from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from typing import Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_./:-]+")


class Embedder(Protocol):
    @property
    def identifier(self) -> str: ...

    def embed(self, text: str) -> Sequence[float]: ...


class HashEmbedder:
    """Deterministic dependency-free embedder for tests and offline smoke checks.

    It is intentionally not a semantic model and must not be used as retrieval
    quality evidence.
    """

    def __init__(self, dimension: int = 128) -> None:
        if dimension <= 0:
            raise ValueError("dimension은 1 이상이어야 합니다")
        self.dimension = dimension

    @property
    def identifier(self) -> str:
        return f"hash-v1:{self.dimension}"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            return [value / norm for value in vector]
        return vector

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    @property
    def query_identifier(self) -> str:
        return self.identifier

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class LlamaCppEmbedder:
    """Local-only client for llama.cpp's OpenAI-compatible embeddings API."""

    MAX_RESPONSE_BYTES = 16 * 1024 * 1024
    MAX_BATCH_SIZE = 256
    MODEL_RE = re.compile(r"^[0-9A-Za-z가-힣_.:/+-]{1,160}$")

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        timeout_seconds: float = 60.0,
        batch_size: int = 32,
        api_key: str = "no-key",
        query_instruction: str | None = None,
    ) -> None:
        self.endpoint = self._validate_endpoint(endpoint)
        if not self.MODEL_RE.fullmatch(model) or "|" in model:
            raise ValueError(
                "embedding model은 1~160자의 안전한 모델 식별자여야 합니다"
            )
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout은 0보다 커야 합니다")
        if not 1 <= batch_size <= self.MAX_BATCH_SIZE:
            raise ValueError(
                f"embedding batch size는 1~{self.MAX_BATCH_SIZE}여야 합니다"
            )
        if not api_key or len(api_key) > 1_024 or "\n" in api_key or "\r" in api_key:
            raise ValueError("embedding API key 형식이 올바르지 않습니다")
        if query_instruction is not None:
            query_instruction = query_instruction.strip()
            if not query_instruction or len(query_instruction) > 500 or "\x00" in query_instruction:
                raise ValueError("embedding query instruction은 1~500자여야 합니다")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.api_key = api_key
        self.query_instruction = query_instruction
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http":
            raise ValueError("embedding endpoint는 로컬 http 주소만 허용합니다")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("embedding endpoint에 인증정보·query·fragment를 넣을 수 없습니다")
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError(
                "embedding endpoint는 127.0.0.0/8 또는 ::1 주소여야 합니다"
            ) from error
        if not address.is_loopback:
            raise ValueError("embedding endpoint는 루프백 주소만 허용합니다")
        if parsed.path.rstrip("/") != "/v1/embeddings":
            raise ValueError("embedding endpoint 경로는 /v1/embeddings여야 합니다")
        if parsed.port is None:
            raise ValueError("embedding endpoint에 포트를 명시해야 합니다")
        return endpoint.rstrip("/")

    @property
    def identifier(self) -> str:
        return f"llama.cpp-openai-v1:{self.model}"

    @property
    def query_identifier(self) -> str:
        if self.query_instruction is None:
            return self.identifier
        digest = hashlib.sha256(self.query_instruction.encode("utf-8")).hexdigest()[:12]
        return f"{self.identifier}:query-{digest}"

    def embed(self, text: str) -> list[float]:
        return self.embed_many((text,))[0]

    def embed_query(self, text: str) -> list[float]:
        if self.query_instruction is None:
            return self.embed(text)
        prompt = f"Instruct: {self.query_instruction}\nQuery: {text}"
        return self.embed(prompt)

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("embedding input은 문자열이어야 합니다")

        vectors: list[list[float]] = []
        expected_dimension: int | None = None
        for offset in range(0, len(texts), self.batch_size):
            batch = list(texts[offset : offset + self.batch_size])
            response = self._request_batch(batch)
            for vector in response:
                if expected_dimension is None:
                    expected_dimension = len(vector)
                elif len(vector) != expected_dimension:
                    raise ValueError("embedding 응답의 벡터 차원이 일치하지 않습니다")
                vectors.append(vector)
        return vectors

    def _request_batch(self, texts: Sequence[str]) -> list[list[float]]:
        body = json.dumps(
            {
                "input": list(texts),
                "model": self.model,
                "encoding_format": "float",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise RuntimeError(f"embedding 서버 HTTP 오류: {error.code}") from error
        except URLError as error:
            raise RuntimeError("로컬 embedding 서버에 연결하지 못했습니다") from error
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise ValueError("embedding 응답이 허용 크기를 초과했습니다")

        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("embedding 서버가 유효한 JSON을 반환하지 않았습니다") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding 응답 개수가 요청 개수와 일치하지 않습니다")

        indexed: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("embedding 응답 항목 형식이 올바르지 않습니다")
            index = item.get("index")
            values = item.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("embedding 응답 index가 정수가 아닙니다")
            if index in indexed or not 0 <= index < len(texts):
                raise ValueError("embedding 응답 index가 중복되었거나 범위를 벗어났습니다")
            if not isinstance(values, list) or not values:
                raise ValueError("embedding 벡터가 비어 있거나 배열이 아닙니다")
            vector: list[float] = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("embedding 벡터에 숫자가 아닌 값이 있습니다")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("embedding 벡터에 유한하지 않은 값이 있습니다")
                vector.append(number)
            indexed[index] = vector

        return [indexed[index] for index in range(len(texts))]
