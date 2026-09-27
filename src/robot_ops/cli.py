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

    replay_parser = subparsers.add_parser("replay", help="실행 기록 재생과 명령·관측 어긋남 계산")
    replay_parser.add_argument("--profile", type=Path, required=True)
    replay_parser.add_argument(
        "--source", required=True, help="lerobot:<데이터셋 경로> (~, $USER만 확장)"
    )
    replay_parser.add_argument("--episode", type=int, required=True)
    replay_parser.add_argument("--ghost", action="store_true", help="고스트 렌더 영상 생성")

    regress_parser = subparsers.add_parser("regress", help="시나리오 격자 회귀 평가")
    regress_parser.add_argument("--profile", type=Path, required=True)
    regress_parser.add_argument("--scenario", required=True, help="scenarios/<id>/scenario.json의 id")
    regress_parser.add_argument("--condition", choices=("A", "B", "both"), default="both")
    regress_parser.add_argument(
        "--rejudge",
        action="store_true",
        help="시뮬을 다시 돌리지 않고 저장된 결과 파일로 judge만 다시 실행해 checks를 갱신(verdict 불변 확인)",
    )
    regress_parser.add_argument(
        "--record-cells",
        help="녹화 패스: 지정 격자점만 record=true로 다시 실행(반복 없음). "
        "형식 'x,y;x,y'(선택 조건 전부) 또는 'A:x,y;B:x,y'",
    )

    report_parser = subparsers.add_parser("report", help="정적 리포트")
    report_sub = report_parser.add_subparsers(dest="report_command", required=True)
    report_build = report_sub.add_parser("build", help="리포트 데이터 굽기 (기본 = 공개 빌드)")
    report_build.add_argument("--profile", type=Path, required=True)
    report_build.add_argument(
        "--private",
        action="store_true",
        help="after_fix_regression 시나리오까지 전체 포함(로컬 확인용, 배포 금지)",
    )
    report_build.add_argument(
        "--hero",
        choices=("auto", "cup_grid", "observation_freeze"),
        default=None,
        help="히어로 영상 소스(기본 auto: 컵 격자 결과·녹화가 있으면 cup_grid, 없으면 observation_freeze)",
    )
    report_serve = report_sub.add_parser("serve", help="Range 지원 로컬 미리보기 서버(영상 seek 동작)")
    report_serve.add_argument("--dir", type=Path, default=Path(".local/report"))
    report_serve.add_argument("--port", type=int, default=8766)
    report_serve.add_argument("--bind", default="127.0.0.1")
    return parser


def _replay_main(args: argparse.Namespace) -> int:
    from .replay.adapter import AdapterError
    from .replay.artifacts import DeletionRefused, InsufficientDisk
    from .replay.profile import expand_path, load_profile
    from .replay.regress import (
        RegressRefused,
        parse_record_cells,
        rejudge_regress,
        run_regress,
        run_replay,
        strip_timestamps,
    )
    from .replay.scenario_bank import load_scenario

    if args.command == "report" and args.report_command == "serve":
        from .replay.report import serve

        directory = args.dir.expanduser()
        if not (directory / "index.html").is_file():
            print(json.dumps({"error": f"리포트가 없습니다: {directory} (먼저 report build)", "type": "FileNotFoundError"},
                             ensure_ascii=False))
            return 2
        try:
            serve(directory, port=args.port, bind=args.bind)
        except KeyboardInterrupt:
            pass
        return 0
    try:
        profile = load_profile(args.profile)
        if args.command == "replay":
            kind, sep, raw_path = args.source.partition(":")
            if not sep or kind != "lerobot":
                raise ValueError("--source는 lerobot:<경로> 형식이어야 합니다")
            result = run_replay(
                profile, expand_path(raw_path, Path.cwd()), args.episode, ghost=args.ghost
            )
            divergence = result["divergence"]
            summary = {
                "run_id": result["run_id"],
                "incidents": divergence["incidents"],
                "max_joint_err_deg": divergence["max_joint_err_deg"],
                "tcp": divergence["tcp"],
                "warnings": result["warnings"],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "regress":
            scenario = load_scenario(
                profile.base_dir / "scenarios" / args.scenario / "scenario.json"
            )
            if args.rejudge:
                print(json.dumps({"rejudged": rejudge_regress(profile, scenario)}, ensure_ascii=False, indent=2))
                return 0
            conditions = ("A", "B") if args.condition == "both" else (args.condition,)
            record_cells = (
                parse_record_cells(args.record_cells, conditions) if args.record_cells else None
            )
            result = run_regress(profile, scenario, conditions=conditions, record_cells=record_cells)
            view = strip_timestamps(result)
            for cond in view["conditions"].values():
                cond.pop("cells", None)
            view["determinism"].pop("cells", None)
            print(json.dumps(view, ensure_ascii=False, indent=2))
            return 0
        from .replay.report import build

        print(json.dumps(build(profile, private=args.private, hero_source=args.hero), ensure_ascii=False, indent=2))
        return 0
    except (
        AdapterError,
        DeletionRefused,
        InsufficientDisk,
        RegressRefused,
        OSError,
        ValueError,
    ) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False))
        return 2


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
    if args.command in ("replay", "regress", "report"):
        return _replay_main(args)
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
