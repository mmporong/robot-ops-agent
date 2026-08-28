from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from .config import DEFAULT_INCLUDE_DIRS, IndexSettings
from .embedding import Embedder, HashEmbedder, LlamaCppEmbedder
from .evaluation import evaluate_retrieval, write_evaluation_report
from .indexer import dump_report, index_stats, sync_index
from .search import keyword_search, reciprocal_rank_fusion, vector_search


def _add_index_options(parser: argparse.ArgumentParser, *, include_embedder: bool) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("ROBOT_OPS_SOURCE_ROOT", Path.cwd())),
        help="검색할 소스 루트 (환경변수: ROBOT_OPS_SOURCE_ROOT)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB 경로 (환경변수: ROBOT_OPS_DB_PATH)",
    )
    parser.add_argument(
        "--include",
        action="append",
        dest="include_dirs",
        help="루트 아래 포함할 경로. 여러 번 지정 가능",
    )
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=120)
    if include_embedder:
        parser.add_argument(
            "--embedder",
            choices=("hash", "llama-cpp"),
            default="hash",
            help="hash는 회귀 테스트 전용, llama-cpp는 로컬 의미 임베딩용입니다",
        )
        parser.add_argument("--hash-dim", type=int, default=128)
        parser.add_argument(
            "--embedding-endpoint",
            default=os.environ.get("ROBOT_OPS_EMBEDDING_ENDPOINT"),
            help="llama.cpp /v1/embeddings 루프백 URL",
        )
        parser.add_argument(
            "--embedding-model",
            default=os.environ.get("ROBOT_OPS_EMBEDDING_MODEL"),
            help="인덱스 pipeline에 고정할 임베딩 모델 식별자",
        )
        parser.add_argument(
            "--embedding-query-instruction",
            default=os.environ.get("ROBOT_OPS_EMBEDDING_QUERY_INSTRUCTION"),
            help="질의에만 붙일 임베딩 검색 지시문",
        )
        parser.add_argument("--embedding-timeout", type=float, default=60.0)
        parser.add_argument("--embedding-batch-size", type=int, default=32)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-ops")
    subparsers = parser.add_subparsers(dest="command", required=True)

    full_parser = subparsers.add_parser("index", help="모든 문서를 강제로 다시 임베딩")
    _add_index_options(full_parser, include_embedder=True)

    update_parser = subparsers.add_parser("update", help="변경된 문서만 증분 갱신")
    _add_index_options(update_parser, include_embedder=True)

    stats_parser = subparsers.add_parser("stats", help="현재 인덱스 통계")
    _add_index_options(stats_parser, include_embedder=False)

    search_parser = subparsers.add_parser("search", help="인덱스 검색")
    _add_index_options(search_parser, include_embedder=True)
    search_parser.add_argument("query")
    search_parser.add_argument("-k", type=int, default=5)
    search_parser.add_argument(
        "--method",
        choices=("keyword", "vector", "hybrid"),
        default="keyword",
    )

    evaluate_parser = subparsers.add_parser("evaluate", help="고정 평가셋 검색 평가")
    _add_index_options(evaluate_parser, include_embedder=True)
    evaluate_parser.add_argument("--dataset", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path)
    evaluate_parser.add_argument("-k", type=int, default=5)
    evaluate_parser.add_argument(
        "--method",
        choices=("keyword", "vector", "hybrid"),
        default="keyword",
    )
    return parser


def _settings_from_args(args: argparse.Namespace) -> IndexSettings:
    root = args.root.expanduser()
    env_db = os.environ.get("ROBOT_OPS_DB_PATH")
    db_path = args.db or (Path(env_db) if env_db else root / ".robot_ops" / "index.db")
    include_dirs = tuple(args.include_dirs or DEFAULT_INCLUDE_DIRS)
    return IndexSettings(
        root=root,
        db_path=db_path,
        include_dirs=include_dirs,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )


def _embedder_from_args(args: argparse.Namespace) -> Embedder:
    if args.embedder == "hash":
        return HashEmbedder(args.hash_dim)
    if not args.embedding_endpoint:
        raise ValueError(
            "llama-cpp 임베더에는 --embedding-endpoint 또는 "
            "ROBOT_OPS_EMBEDDING_ENDPOINT가 필요합니다"
        )
    if not args.embedding_model:
        raise ValueError(
            "llama-cpp 임베더에는 --embedding-model 또는 "
            "ROBOT_OPS_EMBEDDING_MODEL이 필요합니다"
        )
    return LlamaCppEmbedder(
        args.embedding_endpoint,
        args.embedding_model,
        timeout_seconds=args.embedding_timeout,
        batch_size=args.embedding_batch_size,
        api_key=os.environ.get("ROBOT_OPS_EMBEDDING_API_KEY", "no-key"),
        query_instruction=args.embedding_query_instruction,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_from_args(args)

    try:
        if args.command == "stats":
            print(json.dumps(index_stats(settings), ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        embedder = _embedder_from_args(args)

        if args.command == "search":
            if args.method == "keyword":
                hits = keyword_search(settings.normalized().db_path, args.query, k=args.k)
            elif args.method == "vector":
                hits = vector_search(settings.normalized().db_path, args.query, embedder, k=args.k)
            else:
                keyword_hits = keyword_search(
                    settings.normalized().db_path, args.query, k=max(args.k, 20)
                )
                vector_hits = vector_search(
                    settings.normalized().db_path, args.query, embedder, k=max(args.k, 20)
                )
                hits = reciprocal_rank_fusion((keyword_hits, vector_hits), k=args.k)
            print(
                json.dumps(
                    [hit.to_dict(include_text=True) for hit in hits],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "evaluate":
            db_path = settings.normalized().db_path

            def search(query: str, k: int):
                if args.method == "keyword":
                    return keyword_search(db_path, query, k=k)
                if args.method == "vector":
                    return vector_search(db_path, query, embedder, k=k)
                return reciprocal_rank_fusion(
                    (
                        keyword_search(db_path, query, k=max(k, 20)),
                        vector_search(db_path, query, embedder, k=max(k, 20)),
                    ),
                    k=k,
                )

            query_identifier = getattr(
                embedder, "query_identifier", embedder.identifier
            )
            method_name = {
                "keyword": "keyword-bm25-char3-v1",
                "vector": f"vector:{query_identifier}",
                "hybrid": f"hybrid-rrf-v1+vector:{query_identifier}",
            }[args.method]
            report = evaluate_retrieval(
                args.dataset.expanduser().resolve(),
                search,
                k=args.k,
                method=method_name,
            )
            if args.output:
                write_evaluation_report(args.output.expanduser().resolve(), report)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        index_report = sync_index(settings, embedder, force=args.command == "index")
        print(dump_report(index_report))
        return 1 if index_report.failed else 0
    except ValueError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
