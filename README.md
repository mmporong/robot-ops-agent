# Robot Ops Agent

로봇 운용 문서와 실행 기록을 로컬에서 다루는 프로젝트입니다. 두 부분으로 나뉩니다.

1. **문서 검색·조회 도구**: 로봇 운용 문서와 실험 기록을 찾고, 답변이 어떤 파일을 근거로 했는지 확인합니다. Markdown 증분 인덱서, 키워드·의미 검색, 조회 전용 MCP 도구, 로컬 LLM 측정 하네스까지 구현했습니다.
2. **사고 재생·회귀 평가**: 실행 기록을 다시 읽어 명령과 관측이 언제부터 어긋났는지 수치와 영상으로 보여 주고, 재현한 사고를 회귀 시나리오로 저장해 결정적 격자로 다시 평가합니다(`robot-ops replay`·`regress`·`report`). 모든 결과에 출처와 판정 조건을 붙이고 정적 리포트 한 곳에서 봅니다.

이 프로젝트는 로봇을 움직이지 않습니다. 문서 쪽은 ROS 2 상태와 로그를 읽을 뿐이며 토픽 발행, 서비스 호출, 프로세스 재시작, 임의 셸은 제공하지 않습니다. 재생·회귀 쪽은 이미 기록된 파일과 시뮬레이터만 다룹니다. 자세한 경계는 [시스템 경계와 안전 원칙](docs/architecture-and-safety.md)과 [사고 재생·회귀 평가 설계](docs/replay-regress.md)에 정리했습니다.

## 별도 프로젝트로 만든 이유

JD-AMR 한 대의 로그 분석 기능으로 시작했지만, 검색·출처 평가·안전한 조회 도구·로컬 모델 측정은 특정 로봇 애플리케이션에 묶이지 않았습니다. JD-AMR, SO-101 모바일 매니퓰레이터, 차동구동 로봇의 기록을 같은 인터페이스로 다루면서 Robot Ops 계층을 별도 프로젝트로 분리했습니다.

로봇 애플리케이션은 주행과 조작을 담당합니다. Robot Ops Agent는 그 바깥에서 문서와 상태를 조회하고, 어떤 근거로 진단했는지 기록합니다. 이 경계 덕분에 진단 입력이 잘못되더라도 이동 명령이나 임의 셸 실행으로 이어지지 않습니다.

사고 재생·회귀 평가도 같은 경계를 따릅니다. 코어는 로봇을 모르고(표준 라이브러리만 사용), 로봇별 코드는 프로파일이 가리키는 어댑터에만 둡니다. 첫 어댑터는 양팔 로봇 팀 프로젝트(`adapters/bimanual`)이고, 팀 저장소는 읽기만 합니다.

## 현재 확인할 수 있는 것

### 문서 검색·조회 도구

- PKM과 로봇 프로젝트의 Markdown을 SQLite에 증분 인덱싱합니다.
- 소스 루트와 DB 경로를 CLI 인자나 환경변수로 받습니다.
- 파일 내용 해시로 재실행 시 불필요한 임베딩을 건너뜁니다.
- 수정·삭제·이름 변경을 반영합니다.
- 파일 하나의 임베딩이 실패해도 이전 인덱스를 보존합니다.
- 자격증명 경로와 비밀값 패턴이 있는 파일은 인덱스에서 제외합니다.
- 한국어 문자 3-gram을 포함한 BM25 키워드 기준선을 실행합니다.
- llama.cpp의 로컬 `/v1/embeddings`에 문서 청크를 묶어 보내고 벡터·하이브리드 검색에 사용합니다.
- 임베딩 요청은 명시한 루프백 IP로만 보냅니다. 외부 주소, 시스템 프록시, HTTP 리다이렉트는 차단합니다.
- 개발용 고정 평가셋의 해시와 Hit Rate·Recall@k·MRR·출처 정밀도를 JSON으로 남깁니다.
- ROS 2 상태·rosbag 메타데이터·허용된 로그만 읽는 진단 도구 계층이 있습니다.
- 이동 명령, 서비스 호출, 프로세스 재시작, 임의 셸은 도구 allowlist에 없습니다.
- MCP의 미등록 도구와 잘못된 스키마 요청도 실행 전에 감사 로그에 기록합니다.
- llama.cpp의 스트리밍 응답에서 TTFT·처리량·서버 RSS·답변 개념·인용 경로를 함께 측정합니다.

기본 `hash` 임베더는 오프라인 회귀 테스트용입니다. 의미 기반 검색 성능을 나타내지 않으며, 검색 정확도 근거로 사용하지 않습니다.

현재 의미 임베딩은 검색 경로에 연결돼 있지만, 생성 모델은 벤치마크 하네스에서만 사용합니다. 검색·ROS 2 조회·생성 모델을 한 번에 호출하는 사용자용 `robot-ops ask` 진단 루프는 아직 구현하지 않았습니다. 따라서 현재 결과를 `자동 진단 완료`나 `오류 자동 수정`으로 표현하지 않습니다. 모델을 어디서 실행하고 진단·수정 범위를 어떻게 나눌지는 [로컬 LLM 실행과 진단·수정 경계](docs/local-llm-and-remediation.md)에 정리했습니다.

### 사고 재생·회귀 평가

- `robot-ops replay`: LeRobot v3 텔레옵 기록의 명령(action)과 관측(state)을 표준 궤적(`robot-ops-trajectory/1`)으로 바꾸고, 정적 구간 교정 차를 뺀 관절 추종 오차, 명령·관측 TCP 거리, 관측 동결 구간을 계산합니다. `--ghost`를 주면 명령 자세(반투명)와 관측 자세를 같은 카메라로 겹친 고스트 렌더 영상을 만듭니다.
- `robot-ops regress`: 시나리오의 고정 격자(예: 컵 위치 x, y ∈ {−5, −2.5, 0, 2.5, 5} mm)를 조건 A/B로 실행하고, 격자점마다 시뮬레이터 정답 좌표로 판정합니다. 반복 점을 다시 실행해 판정 일치율을 따로 적습니다. `--rejudge`는 시뮬을 다시 돌리지 않고 저장된 결과 파일로 판정 항목만 다시 계산합니다. 판정(verdict)이 바뀌면 그 결과 파일을 쓰지 않고 멈춥니다.
- `robot-ops report build`·`report serve`: 정적 사이트(`.local/report`)를 굽고 로컬에서 미리 봅니다. 첫 화면은 사례 두 가지(① SO-101 실측 기록 재생, ② 양팔 로봇 컵 접촉 시뮬 회귀 평가)를 무엇을·왜·결과로 나눠 보여 주고, 사례 ②는 `기록된 사고 → 같은 조건 재현 → 주변 25점 격자 → 복구 켬 비교` 흐름으로 이어집니다. 격자점마다 패드 접촉력·컵 이동 시계열(판정 임계·동작 단계·판정 시각 표시)과 위·앞에서 본 실제 비율 배치도(계획 위치, 옮긴 컵, 판정 순간의 두 패드, 추정 접촉 위치)를 붙이고, 통과 지도 위에 "격자 읽는 법"(칸 하나의 뜻, 축 방향, 격자 간격으로 본 여유 범위)을 둡니다.
- 모든 카드에 출처 배지(`실측 기록`·`시뮬 실행 기록`·`오류 주입(시뮬)`)와 판정 조건 배지(`강체 패드 접촉 시뮬 기준`·`정답 좌표 복구 조건` 등), 소스 SHA-256을 붙입니다.
- 코어(`src/robot_ops/replay`)는 표준 라이브러리만 import합니다. numpy·Isaac·MuJoCo가 필요한 일은 (동사, 과제)별 인터프리터로 분리한 어댑터 subprocess에서 합니다.

## 사고 재생·회귀 평가 실측 결과

아래 수치는 이 저장소의 코어와 `adapters/bimanual`로 2026-09-27에 측정했습니다. 각 결과는 판정 조건과 함께 읽어야 합니다.

**컵 위치 격자 회귀 (양팔 로봇 프로젝트 시뮬 실행 기록)**

| 항목 | 값 |
| --- | --- |
| 격자 | 컵 위치 ±5 mm, x·y 각 5단계 25점 |
| 조건 A: 복구 끔 | 20/25 통과 (실패 5점은 모두 y = −5 mm 행, 실패 코드 `premature_cup_contact`) |
| 조건 B: 복구 켬(정답 좌표 조건) | 25/25 통과 |
| 판정 조건 | 강체 패드 접촉 시뮬 기준, 왼팔 단독. 조건 B의 복구는 시뮬레이터 정답 좌표를 쓰는 조건 |
| 판정 일치율 | 1.0 (조건 A 5점 × 3회, 판정 지표 최대 편차 0.0) |
| 실행 조건 | Isaac Sim 5.1.0.0, 팀 저장소 커밋 `8a09a02`, infra·timeout 셀 0, 총 65회 실행 1,488초 |

조건 B가 25/25인 것은 정답 좌표를 쓸 수 있는 시뮬 조건에서의 결과입니다. 실물 핑거 파지나 카메라 기반 인식 성능을 나타내지 않습니다.

**관측 동결 재생 (SO-101 실측 텔레옵 기록)**

| 항목 | 값 |
| --- | --- |
| 기록 | `so101_teleop_bench` 에피소드 0, 10 fps |
| 대표 동결 구간 | 4.4–6.9 s (26프레임). 동결 기준: 모든 관절 \|Δ관측\| < 0.1° 이면서 명령 \|Δ\| > 0.5°가 3프레임 이상 |
| 최대 TCP 거리 | 170 mm (명령 자세와 관측 자세의 TCP 사이 거리, 임계 20 mm) |
| 궤적 모델 | SO-101 MuJoCo 미러(양팔 로봇 왼팔과 같은 SO-101 기구). TCP와 고스트 렌더 모두 이 모델로 계산하며 영상 측정값이 아닙니다 |

이 에피소드는 248프레임 동안 기록된 관측 상태 값이 두 가지뿐이었지만, 3인칭 실측 영상에서는 팔이 움직입니다. TCP 거리는 기록된 관측이 실제 자세를 반영하지 못한 정도를 모델 좌표로 보여 줍니다.

양팔 실물 기록은 아직 없습니다. 그래서 실측 재생은 같은 기구인 SO-101 기록으로 하고, 양팔 로봇 프로젝트는 시뮬 실행 기록만 다룹니다. 설계, 판정 규칙, 재현 명령과 한계는 [사고 재생·회귀 평가 설계](docs/replay-regress.md)에 있습니다.

## 의미 임베딩 연결

llama.cpp 서버를 전용 임베딩 모델과 pooling이 활성화된 상태로 실행한 뒤 별도 DB를 만듭니다. `--embedding-model` 값은 모델 파일명이나 고정 revision을 구분할 수 있는 식별자로 정합니다. 모델을 바꾸면 기존 DB를 재사용하지 않습니다.

```bash
cd "$HOME/robot-ops-agent"
export ROBOT_OPS_EMBEDDING_API_KEY="$(openssl rand -hex 32)"
export LLAMA_API_KEY="$ROBOT_OPS_EMBEDDING_API_KEY"
llama-server \
  -m "$EMBEDDING_MODEL_PATH" \
  --embedding \
  --pooling last \
  --host 127.0.0.1 \
  --port 8081

uv run robot-ops index \
  --root "$PWD" \
  --db "$PWD/.local/semantic.db" \
  --include docs \
  --embedder llama-cpp \
  --embedding-endpoint http://127.0.0.1:8081/v1/embeddings \
  --embedding-model embedding-model-fixed-revision

uv run robot-ops evaluate \
  --root "$PWD" \
  --db "$PWD/.local/semantic.db" \
  --include docs \
  --embedder llama-cpp \
  --embedding-endpoint http://127.0.0.1:8081/v1/embeddings \
  --embedding-model embedding-model-fixed-revision \
  --embedding-query-instruction "Retrieve evidence for a robot operations diagnostic question." \
  --dataset evaluations/datasets/robot_diagnostics_dev_v0.1.0.json \
  --method hybrid \
  -k 5
```

pooling 방식은 사용하는 모델의 배포 문서를 따릅니다. 클라이언트는 숫자 루프백 주소만 허용하고 시스템 프록시와 HTTP 리다이렉트를 사용하지 않습니다. API 키는 `ROBOT_OPS_EMBEDDING_API_KEY` 환경변수로만 받습니다.

Qwen3-Embedding 0.6B Q8의 Vulkan 개발 기준선도 측정했습니다. 2,321개 문서와 3,075개 청크의 첫 인덱싱은 162.33초, 변경 없는 재실행은 0.75초였습니다. 개발 질문 7개에서 하이브리드 검색의 Chunk Precision@5는 0.4000으로 키워드 기준선 0.3143보다 높았지만, MRR은 1.0에서 0.9286으로 낮았습니다. 벡터 검색만으로는 키워드 기준선을 넘지 못했습니다. 자세한 조건과 실패 결과는 [의미 검색 개발 기준선](docs/semantic-retrieval-2026-08-28.md)에 있습니다.

## 5분 스모크 테스트

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다. 공개 저장소 안의 문서만 인덱싱하므로 별도 로봇 데이터 없이 실행할 수 있습니다. 단위 테스트는 재생·회귀 코어를 가짜 어댑터(`tests/fake_adapter.py`)로 검사하므로 Isaac·MuJoCo·로봇 데이터 없이 돕니다.

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

## 사고 재생·회귀 평가 실행

프로파일(`profiles/bimanual.toml`)이 과제와 동사마다 쓸 인터프리터를 정합니다. 코어는 `uv`로 실행하고, 어댑터는 아래 인터프리터로 subprocess 실행합니다.

| 프로파일 이름 | 용도 | (동사, 과제) |
| --- | --- | --- |
| `core` | 표준 라이브러리만 쓰는 판정 | `judge`: `cup_contact`, `drive_kinematic` |
| `lerobot` | numpy·pyarrow (LeRobot 데이터셋 읽기) | `convert`: `lerobot_episode` / `run`: `drive_kinematic` |
| `rlwalk` | MuJoCo (SO-101 미러 FK·고스트 렌더) | `run`: `lerobot_episode` |
| `leisaac` | Isaac Sim 5.1 | `run`: `cup_contact` |

인터프리터 경로는 프로파일 `[interpreters]`에서 `~`와 `$USER`만 확장합니다. 다른 환경에서는 이 경로를 바꿔 씁니다.

```bash
cd "$HOME/robot-ops-agent"

# 실측 텔레옵 기록 재생 (lerobot → rlwalk, GPU 불필요)
uv run robot-ops replay --profile profiles/bimanual.toml \
  --source lerobot:~/so101_datasets/so101_teleop_bench --episode 0 --ghost

# 컵 격자 회귀 (Isaac, 조건 A/B 25점씩 + 반복 15회)
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact --condition both

# 지정 격자점만 녹화 (반복 없음)
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact --record-cells "A:0,-5;B:0,-5"

# 저장된 결과로 판정 항목만 다시 계산 (시뮬 재실행 없음)
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact --rejudge

# 정적 리포트 굽기(기본 = 공개 빌드)와 로컬 미리보기
uv run robot-ops report build --profile profiles/bimanual.toml
uv run robot-ops report serve --dir .local/report --port 8766
```

산출물은 저장소 밖 `/data/$USER/robot-ops/bimanual/runs/<run_id>/`에 남고, 리포트는 `.local/report`(Git 추적 제외)에 생깁니다. `--private` 빌드(팀 공개 전 기록까지 포함, 배포 금지)는 `.local/report-private`에 따로 생깁니다. 미리보기는 영상 탐색에 필요한 HTTP Range 요청을 처리하는 `robot-ops report serve`를 씁니다.

## 실측 기준선

2026-08-28에는 Qwen3 0.6B Q8 모델을 llama.cpp로 실행해 7개 개발 질문을 두 번씩 측정했습니다. x86_64 CPU 첫 실행의 TTFT P50은 18.04초, 전체 지연 P50은 24.48초, 생성 처리량 P50은 13.43 tokens/s, 서버 최대 RSS는 2,008.98 MiB였습니다.

검색은 개발 질문 7개 모두 상위 5개 안에서 정답 문서를 하나 이상 찾았지만, 여러 정답 경로를 모두 찾는 Recall@5는 0.9286이었습니다. 생성 답변은 모든 필수 개념을 맞힌 질문이 2/7에 그쳤습니다. 경로 인용 정밀도는 0.7143이었고, 요구한 인용 형식 준수율은 0.0714였습니다. 작은 모델을 실제 진단에 바로 쓰기에는 정확도와 첫 응답 지연이 부족합니다.

전체 조건과 원본 해시는 [로컬 CPU 기준선](docs/benchmark-2026-08-28.md), 집계 JSON은 [2026-08-28 기준선](evaluations/baselines/2026-08-28_x86_64_cpu_qwen3_0.6b_q8.json)에서 확인할 수 있습니다. 질문별 검색 순위·점수는 [검색 원시 증거](evaluations/evidence/2026-08-28_keyword_dev_top5_raw.json), 모델 답변·지연은 [생성 원시 증거](evaluations/evidence/2026-08-28_qwen3_0.6b_q8_cpu_dev_raw.json)에 남겼습니다. 모델과 런타임 바이너리는 저장소에 포함하지 않습니다.

의미 검색의 모델 해시, 인덱싱 시간, GPU 메모리 표본과 검색 방식별 결과는 [Vulkan 의미 검색 기준선](evaluations/baselines/2026-08-28_x86_64_vulkan_qwen3_embedding_0.6b_q8.json)에 따로 기록했습니다.

단계별 완료 여부와 공개할 수 있는 주장 범위는 [검증 상태와 다음 단계](docs/validation-status.md)에 분리했습니다.

같은 입력을 강제로 다시 임베딩하려면 `update` 대신 `index`를 사용합니다. 통계는 다음 명령으로 확인합니다.

```bash
cd "$HOME/robot-ops-agent"
PYTHONPATH=src python3 -m robot_ops stats \
  --root "$HOME" \
  --db "$HOME/robot-ops-agent/.local/robot_ops.db"
```

## 안전 경계

문서 검색·조회 도구

- 진단 도구는 고정된 인자 배열을 `shell=False`로 실행합니다. `/cmd_vel`은 메시지를 읽을 수 있지만 발행할 수 없습니다. 도구 이름 자체가 allowlist에 없으면 외부 프로세스를 만들기 전에 거부하고, 입력·결과 요약·거부 이유를 JSONL 감사 로그에 남깁니다.

사고 재생·회귀 평가

- 로봇을 움직이지 않습니다. 실물 팔·베이스에 명령을 보내는 코드와 하드웨어 연결 코드가 없고, 입력은 기록된 파일과 시뮬레이터뿐입니다.
- 코어는 `run` 응답과 결과 파일 양쪽의 `hardware_accessed`가 정확히 `false`인지 확인합니다. 값이 다르거나 키가 없으면 regress 전체를 즉시 멈추고 남은 셀을 실행하지 않습니다. 어댑터가 이 값을 스스로 쓰는 경로도 있으므로, 실질적인 보호는 다음 allowlist입니다.
- 어댑터가 실행·import할 수 있는 팀 파일은 프로파일 `[adapter.allowlist]`에 적은 두 개(`tools/simulate_cup_contact.py` 실행, `tools/mobile_service_control.py` import)뿐입니다. 어댑터 run 모듈은 이 파일을 상수로 고정해 열고, 요청에 다른 팀 파일이 오면 거부합니다. 어댑터 쪽 목록·상수가 프로파일과 같은지는 테스트가 확인합니다.
- 양팔 로봇 팀 저장소는 읽기 전용입니다. 모든 어댑터 subprocess에 `PYTHONDONTWRITEBYTECODE=1`을 넣고, 과거 코드가 필요하면 `git show <sha>:<path>`로 파일 하나만 산출물 폴더에 꺼냅니다. 팀 저장소에 커밋되지 않은 변경이 있으면 regress를 거부합니다.
- Isaac 실행은 `systemd-run --user --scope`(MemoryMax 20G)와 `timeout` 안에서 하고, 잠금 파일(`flock`)로 한 번에 하나만 돌리며, 실행마다 GPU 메모리가 실행 전 +200 MiB 안으로 돌아왔는지 확인합니다.
- 산출물 삭제는 프로파일 `delete_patterns`에 맞고 manifest에 기록된 파일만, 산출물 root 안에서 심링크를 거치지 않을 때만 허용합니다.

## 주장 경계

지금 확인된 범위는 Linux에서 재현 가능한 인덱싱·증분 갱신, 키워드 검색, 조회 전용 MCP, x86_64 CPU 로컬 생성 기준선입니다. 저장소에 든 질문은 개발용이므로 최종 성능 수치로 쓰지 않습니다. 실제 임베딩 모델과 별도 조건 홀드아웃을 검증하기 전에는 `검색 정확도를 개선했다`고 표현하지 않습니다. Raspberry Pi 또는 Jetson에서 모델 프로세스와 ROS 2를 함께 실행하고 자원·지연을 측정하기 전에는 `엣지 배포`라고 표현하지 않습니다.

사고 재생·회귀 평가의 컵 격자 결과는 왼팔 단독, 강체 패드 접촉 시뮬 기준이고, 조건 B는 시뮬레이터 정답 좌표를 쓰는 복구 조건입니다. 실물 파지 성공률로 표현하지 않습니다. 관측 동결 재생의 TCP 거리는 SO-101 MuJoCo 미러로 계산한 모델 좌표이며, 동역학 차이는 다루지 않습니다.

## 저장소 구성

```text
src/robot_ops/          인덱싱, 검색, 평가, 진단 도구, MCP 서버, CLI
src/robot_ops/replay/   사고 재생·회귀 평가 코어(stdlib만): 프로파일, 스키마, 어댑터 호출,
                        어긋남 계산, 격자, 시나리오, regress, 산출물 삭제, 미디어, 리포트
adapters/bimanual/      양팔 로봇 프로젝트 어댑터(convert | run | judge)
profiles/               프로파일(과제·동사별 인터프리터, allowlist, 자원 상한, 산출물 경로)
scenarios/              회귀 시나리오(scenario.json)
tests/                  안전 경계와 회귀 테스트, 가짜 어댑터
evaluations/datasets/   개발용 진단 질문과 채점 기준
evaluations/evidence/   질문별 검색 결과와 모델 원시 측정
evaluations/baselines/  공개 가능한 집계 기준선
evaluations/replay/     재생 대상 인벤토리 스캔 결과와 결과 전에 고정한 판정 임계
docs/                   아키텍처, 안전 경계, 벤치마크, 재생·회귀 설계, 검증 상태
```
