# Robot Ops Agent

로봇 운용 문서와 실험 기록을 로컬에서 찾고, 답변이 어떤 파일을 근거로 했는지 확인하기 위한 프로젝트입니다. Markdown 증분 인덱서, 키워드 검색 기준선, 조회 전용 MCP 도구, 로컬 LLM 측정 하네스까지 구현했습니다.

이 프로젝트는 로봇을 움직이지 않습니다. ROS 2 상태와 로그를 읽을 뿐이며 토픽 발행, 서비스 호출, 프로세스 재시작, 임의 셸은 제공하지 않습니다. 자세한 경계는 [시스템 경계와 안전 원칙](docs/architecture-and-safety.md)에 정리했습니다.

## 별도 프로젝트로 만든 이유

JD-AMR 한 대의 로그 분석 기능으로 시작했지만, 검색·출처 평가·안전한 조회 도구·로컬 모델 측정은 특정 로봇 애플리케이션에 묶이지 않았습니다. JD-AMR, SO-101 모바일 매니퓰레이터, 차동구동 로봇의 기록을 같은 인터페이스로 다루면서 Robot Ops 계층을 별도 프로젝트로 분리했습니다.

로봇 애플리케이션은 주행과 조작을 담당합니다. Robot Ops Agent는 그 바깥에서 문서와 상태를 조회하고, 어떤 근거로 진단했는지 기록합니다. 이 경계 덕분에 진단 입력이 잘못되더라도 이동 명령이나 임의 셸 실행으로 이어지지 않습니다.

## 현재 확인할 수 있는 것

- PKM과 로봇 프로젝트의 Markdown을 SQLite에 증분 인덱싱합니다.
- 소스 루트와 DB 경로를 CLI 인자나 환경변수로 받습니다.
- 파일 내용 해시로 재실행 시 불필요한 임베딩을 건너뜁니다.
- 수정·삭제·이름 변경을 반영합니다.
- 파일 하나의 임베딩이 실패해도 이전 인덱스를 보존합니다.
- 자격증명 경로와 비밀값 패턴이 있는 파일은 인덱스에서 제외합니다.
- 한국어 문자 3-gram을 포함한 BM25 키워드 기준선을 실행합니다.
- 개발용 고정 평가셋의 해시와 Hit Rate·Recall@k·MRR·출처 정밀도를 JSON으로 남깁니다.
- ROS 2 상태·rosbag 메타데이터·허용된 로그만 읽는 진단 도구 계층이 있습니다.
- 이동 명령, 서비스 호출, 프로세스 재시작, 임의 셸은 도구 allowlist에 없습니다.
- MCP의 미등록 도구와 잘못된 스키마 요청도 실행 전에 감사 로그에 기록합니다.
- llama.cpp의 스트리밍 응답에서 TTFT·처리량·서버 RSS·답변 개념·인용 경로를 함께 측정합니다.

기본 `hash` 임베더는 오프라인 회귀 테스트용입니다. 의미 기반 검색 성능을 나타내지 않으며, 검색 정확도 근거로 사용하지 않습니다.

## 5분 스모크 테스트

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다. 공개 저장소 안의 문서만 인덱싱하므로 별도 로봇 데이터 없이 실행할 수 있습니다.

```bash
cd "$HOME/robot-ops-agent"
uv sync --extra mcp
uv run python -m unittest discover -s tests -v
uv run robot-ops update \
  --root "$PWD" \
  --db "$PWD/.local/demo.db" \
  --include docs
uv run robot-ops search \
  --root "$PWD" \
  --db "$PWD/.local/demo.db" \
  --include docs \
  "위험한 로봇 명령을 어떻게 막았는가?" -k 3
```

저장소의 기준선 수치는 개인 작업 공간의 로봇 문서 2,317개로 측정했습니다. 원본 문서 전체는 이 저장소에 복제하지 않았으며, 질문별 검색 순위와 모델 답변은 비식별 원시 증거로 추적합니다. 같은 평가를 자신의 문서로 실행하려면 `--root`, `--include`, `--db`를 해당 경로로 바꾸면 됩니다.

MCP stdio 서버는 공식 Python SDK 2.1.1을 선택 기능으로 고정합니다.

```bash
cd "$HOME/robot-ops-agent"
uv sync --extra mcp
ROBOT_OPS_ALLOWED_ROOTS="$PWD/docs:$PWD/evaluations" \
ROBOT_OPS_DB_PATH="$PWD/.local/demo.db" \
ROBOT_OPS_AUDIT_LOG="$PWD/.local/tool_audit.jsonl" \
uv run robot-ops-mcp
```

stdout은 MCP 프로토콜 전용입니다. 실행 로그와 감사 기록은 `ROBOT_OPS_AUDIT_LOG`로 지정한 JSONL 파일에 남습니다.

## 실측 기준선

2026-08-28에는 Qwen3 0.6B Q8 모델을 llama.cpp로 실행해 7개 개발 질문을 두 번씩 측정했습니다. x86_64 CPU 첫 실행의 TTFT P50은 18.04초, 전체 지연 P50은 24.48초, 생성 처리량 P50은 13.43 tokens/s, 서버 최대 RSS는 2,008.98 MiB였습니다.

검색은 개발 질문 7개 모두 상위 5개 안에서 정답 문서를 하나 이상 찾았지만, 여러 정답 경로를 모두 찾는 Recall@5는 0.9286이었습니다. 생성 답변은 모든 필수 개념을 맞힌 질문이 2/7에 그쳤습니다. 경로 인용 정밀도는 0.7143이었고, 요구한 인용 형식 준수율은 0.0714였습니다. 작은 모델을 실제 진단에 바로 쓰기에는 정확도와 첫 응답 지연이 부족합니다.

전체 조건과 원본 해시는 [로컬 CPU 기준선](docs/benchmark-2026-08-28.md), 집계 JSON은 [2026-08-28 기준선](evaluations/baselines/2026-08-28_x86_64_cpu_qwen3_0.6b_q8.json)에서 확인할 수 있습니다. 질문별 검색 순위·점수는 [검색 원시 증거](evaluations/evidence/2026-08-28_keyword_dev_top5_raw.json), 모델 답변·지연은 [생성 원시 증거](evaluations/evidence/2026-08-28_qwen3_0.6b_q8_cpu_dev_raw.json)에 남겼습니다. 모델과 런타임 바이너리는 저장소에 포함하지 않습니다.

단계별 완료 여부와 공개할 수 있는 주장 범위는 [검증 상태와 다음 단계](docs/validation-status.md)에 분리했습니다.

같은 입력을 강제로 다시 임베딩하려면 `update` 대신 `index`를 사용합니다. 통계는 다음 명령으로 확인합니다.

```bash
cd "$HOME/robot-ops-agent"
PYTHONPATH=src python3 -m robot_ops stats \
  --root "$HOME" \
  --db "$HOME/robot-ops-agent/.local/robot_ops.db"
```

## 주장 경계

지금 확인된 범위는 Linux에서 재현 가능한 인덱싱·증분 갱신, 키워드 검색, 조회 전용 MCP, x86_64 CPU 로컬 생성 기준선입니다. 저장소에 든 질문은 개발용이므로 최종 성능 수치로 쓰지 않습니다. 실제 임베딩 모델과 별도 조건 홀드아웃을 검증하기 전에는 `검색 정확도를 개선했다`고 표현하지 않습니다. Raspberry Pi 또는 Jetson에서 모델 프로세스와 ROS 2를 함께 실행하고 자원·지연을 측정하기 전에는 `엣지 배포`라고 표현하지 않습니다.

진단 도구는 고정된 인자 배열을 `shell=False`로 실행합니다. `/cmd_vel`은 메시지를 읽을 수 있지만 발행할 수 없습니다. 도구 이름 자체가 allowlist에 없으면 외부 프로세스를 만들기 전에 거부하고, 입력·결과 요약·거부 이유를 JSONL 감사 로그에 남깁니다.

## 저장소 구성

```text
src/robot_ops/          인덱싱, 검색, 평가, 진단 도구, MCP 서버
tests/                  안전 경계와 회귀 테스트
evaluations/datasets/   개발용 진단 질문과 채점 기준
evaluations/evidence/   질문별 검색 결과와 모델 원시 측정
evaluations/baselines/  공개 가능한 집계 기준선
docs/                   아키텍처, 안전 경계, 벤치마크, 검증 상태
```
