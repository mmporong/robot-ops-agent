from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from robot_ops.embedding import LlamaCppEmbedder  # noqa: E402


class _EmbeddingHandler(BaseHTTPRequestHandler):
    response_mode: ClassVar[str] = "ok"
    requests: ClassVar[list[dict[str, object]]] = []
    authorization_headers: ClassVar[list[str | None]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        type(self).authorization_headers.append(self.headers.get("Authorization"))

        if type(self).response_mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "http://203.0.113.10/v1/embeddings")
            self.end_headers()
            return

        inputs = payload["input"]
        data = [
            {"index": index, "embedding": [float(len(text)), float(index + 1)]}
            for index, text in enumerate(inputs)
        ]
        if type(self).response_mode == "reverse":
            data.reverse()
        elif type(self).response_mode == "duplicate":
            data[-1]["index"] = 0
        elif type(self).response_mode == "non_finite":
            data[0]["embedding"][0] = float("nan")

        body = json.dumps({"object": "list", "data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class LlamaCppEmbedderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.endpoint = f"http://{host}:{port}/v1/embeddings"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _EmbeddingHandler.response_mode = "ok"
        _EmbeddingHandler.requests = []
        _EmbeddingHandler.authorization_headers = []

    def test_batches_requests_and_restores_response_index_order(self) -> None:
        _EmbeddingHandler.response_mode = "reverse"
        embedder = LlamaCppEmbedder(
            self.endpoint,
            "bge-m3-q8_0",
            batch_size=2,
        )

        vectors = embedder.embed_many(("하나", "둘둘", "셋셋셋"))

        self.assertEqual(vectors, [[2.0, 1.0], [2.0, 2.0], [3.0, 1.0]])
        self.assertEqual(len(_EmbeddingHandler.requests), 2)
        self.assertEqual(_EmbeddingHandler.requests[0]["input"], ["하나", "둘둘"])
        self.assertEqual(embedder.identifier, "llama.cpp-openai-v1:bge-m3-q8_0")

    def test_single_embedding_uses_openai_compatible_payload(self) -> None:
        embedder = LlamaCppEmbedder(
            self.endpoint,
            "embeddinggemma-300m",
            api_key="local-test-key",
        )

        vector = embedder.embed("로봇 상태")

        self.assertEqual(vector, [5.0, 1.0])
        self.assertEqual(
            _EmbeddingHandler.requests[0],
            {
                "input": ["로봇 상태"],
                "model": "embeddinggemma-300m",
                "encoding_format": "float",
            },
        )
        self.assertEqual(
            _EmbeddingHandler.authorization_headers,
            ["Bearer local-test-key"],
        )

    def test_query_instruction_is_used_only_for_query_embedding(self) -> None:
        instruction = "Retrieve robot operations evidence."
        embedder = LlamaCppEmbedder(
            self.endpoint,
            "embeddinggemma-300m",
            query_instruction=instruction,
        )

        embedder.embed_query("모터가 멈춘 이유")
        embedder.embed("문서 원문")

        self.assertEqual(
            _EmbeddingHandler.requests[0]["input"],
            [f"Instruct: {instruction}\nQuery: 모터가 멈춘 이유"],
        )
        self.assertEqual(_EmbeddingHandler.requests[1]["input"], ["문서 원문"])
        self.assertRegex(embedder.query_identifier, r":query-[0-9a-f]{12}$")
        self.assertNotEqual(embedder.query_identifier, embedder.identifier)

    def test_remote_or_ambiguous_endpoint_is_rejected(self) -> None:
        invalid = (
            "https://127.0.0.1:8080/v1/embeddings",
            "http://localhost:8080/v1/embeddings",
            "http://192.168.0.10:8080/v1/embeddings",
            "http://127.0.0.1:8080/embedding",
            "http://user:secret@127.0.0.1:8080/v1/embeddings",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    LlamaCppEmbedder(endpoint, "safe-model")

    def test_redirect_is_not_followed(self) -> None:
        _EmbeddingHandler.response_mode = "redirect"
        embedder = LlamaCppEmbedder(self.endpoint, "safe-model")

        with self.assertRaisesRegex(RuntimeError, "HTTP 오류: 307"):
            embedder.embed("외부로 보내지 않음")

    def test_malformed_or_non_finite_response_is_rejected(self) -> None:
        embedder = LlamaCppEmbedder(self.endpoint, "safe-model")
        for mode in ("duplicate", "non_finite"):
            with self.subTest(mode=mode):
                _EmbeddingHandler.response_mode = mode
                with self.assertRaises(ValueError):
                    embedder.embed_many(("하나", "둘"))


if __name__ == "__main__":
    unittest.main()
