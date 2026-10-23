# Robot Ops Agent

로봇 운용 문서와 실행 기록을 로컬에서 다루는 프로젝트입니다. 두 부분으로 나뉩니다.

1. **문서 검색·조회 도구**: 로봇 운용 문서와 실험 기록을 찾고, 답변이 어떤 파일을 근거로 했는지 확인합니다. Markdown 증분 인덱서, 키워드·의미 검색, 조회 전용 MCP 도구, 저장 궤적 CPU 분석, 로컬 LLM 측정 하네스까지 구현했습니다.
2. **사고 재생·회귀 평가**: 실행 기록을 다시 읽어 명령과 관측이 언제부터 어긋났는지 수치와 영상으로 보여 주고, 재현한 사고를 회귀 시나리오로 저장해 결정적 격자로 다시 평가합니다(`robot-ops replay`·`regress`·`report`). 모든 결과에 출처와 판정 조건을 붙이고 정적 리포트 한 곳에서 봅니다.

이 프로젝트는 로봇을 움직이지 않습니다. 문서 쪽은 ROS 2 상태와 로그를 읽을 뿐이며 토픽 발행, 서비스 호출, 프로세스 재시작, 임의 셸은 제공하지 않습니다. 재생·회귀 쪽은 이미 기록된 파일과 시뮬레이터만 다룹니다. 자세한 경계는 [시스템 경계와 안전 원칙](docs/architecture-and-safety.md)과 [사고 재생·회귀 평가 설계](docs/replay-regress.md)에 정리했습니다.

## 계획 위치 오차와 목표 보정 검증

컵에 접근할 때 계획한 위치와 관측 위치가 다르면, 그리퍼를 닫기도 전에 컵을 건드릴 수 있습니다. `robot-ops cup-cause-check`는 동작 전의 안정된 RESET 관측과 계획 좌표를 비교해 XY 보정량을 계산합니다. 그런 다음 컵 배치·물리 설정·판정 기준은 고정하고 로봇의 계획 목표만 바꿔 효과를 검사합니다.

에이전트는 읽기 전용 MCP `analyze_cup_alignment(path)`로 위치 차이와 보정 후보를 조회할 수 있습니다. 이 도구에는 실험을 실행하거나 파일을 고치는 권한이 없습니다. 시뮬 실험 실행은 위 CLI가 담당하고, MCP `analyze_simulation`은 각 실행의 원시 접촉력과 물리 시간 진행을 재검사합니다.

원래 목표, 같은 목표 재실행, 관측 위치로 보정한 목표, 반대 방향으로 바꾼 목표를 각각 세 번 실행합니다. 주변 3×3 위치에서는 원래 목표와 관측 보정을 짝지어 비교합니다. 모든 조건에서 사후 복구는 꺼져 있습니다. 보정 조건이 실패하거나 정상 조건을 망가뜨리면 실험 성공으로 처리하지 않습니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops cup-cause-check --profile profiles/bimanual.toml \
  --case "$HOME/robot-ops-agent/.local/cup-run/isaac/result.json" \
  --out .local/cup-cause-check/run1
```

`--case`에는 초기 RESET 관측이 포함된 시뮬 실행 결과를 지정하며, 같은 폴더에 `plan.json`이 있어야 합니다. 위 입력 경로는 보유한 실행 경로로 바꿉니다. `sim-check`와 같은 Isaac·GPU·자원 격리 환경이 필요합니다. 원천 커밋은 프로파일에 고정하며, 팀 저장소는 읽기만 하고 깨끗한 사본에서 실행합니다. 다른 커밋은 `--source-commit <해시>`로 선택하며 해당 커밋이 원천 저장소에 있어야 합니다. 입력 사례의 소스·설정 해시가 선택한 커밋과 다르면 시뮬 실행 전에 거부합니다. 새 디렉터리 구조의 `plan.json`은 `tool_source_paths`로 파일 위치를 확인하고, 경로 정보가 없는 기존 기록도 재검사합니다.

시뮬레이터 정답 좌표를 사용하는 결정적 검사로, 위치 오차가 생긴 카메라·캘리브레이션 원인이나 실물 파지 성능은 판정하지 않습니다. LLM 판단이나 자동 코드 수정도 포함하지 않습니다.

결과의 `report/index.html`에서 네 실행 영상을 바꾸어 보고, 계획·관측 위치와 반복·주변 위치 검사 결과를 확인합니다. 컵과 그리퍼를 확대한 영상은 스크롤 중에도 남으며 전체 장면으로 전환할 수 있습니다. `report/evidence.json`에는 원시 결과·계획·영상 해시와 고정 기준의 재판정 결과를 남깁니다. 화면을 다시 만들 때도 전체 실행을 MCP로 재검사합니다.

개발용 시뮬 실험에서는 계획 Y=170mm와 관측 Y≈165mm의 차이가 검출됐습니다. 컵을 옮기지 않고 목표를 관측 위치로 보정하자 조기 접촉 없이 상승 검사를 통과했습니다. 원래 목표·같은 목표 재실행·반대 보정은 각각 세 번 모두 실패했고 관측 보정은 세 번 모두 통과했습니다. 주변 9개 위치의 통과 수는 고정 목표 6개, 관측 보정 9개였습니다. 총 28회이며 일부 격자 결과는 핵심 반복에서 재사용했습니다. [원시 해시·통제 조건·공통 판정·MCP 감사 요약](evaluations/evidence/2026-10-16_cup_cause_experiment.json)에 근거를 남겼습니다. 새 정렬 MCP 도구는 이 실행들이 끝난 뒤 재검사에 사용했으며, 현장 정확도나 별도 홀드아웃 성능을 측정한 결과는 아닙니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops cup-cause-check --from-experiment .local/cup-cause-check/run1 \
  --out .local/cup-cause-check/review1
```

## 새 시뮬레이션 실행 검사

**컵 위치가 5mm 어긋났을 때 그리퍼를 닫기 전에 컵을 건드리는 문제**를 다룹니다. `robot-ops sim-check`는 깨끗한 원천 커밋 사본에서 정상 위치 → 위치 오차 → 같은 오차에서 복구를 순서대로 실행합니다. 각 단계 뒤 MCP `analyze_simulation`이 원시 접촉력과 물리 시간 진행을 재검사합니다. 팀 저장소의 작업 중 변경은 실행 사본에 포함하지 않습니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops sim-check --profile profiles/bimanual.toml \
  --out .local/simulation-check/run1
```

프로파일의 `pinned_commit`을 사용하며 다른 원천 커밋은 `--source-commit <해시>`로 지정합니다. 새 실행은 기존 출력 경로를 재사용하지 않습니다. Isaac 5.1·GPU·systemd 사용자 실행 환경·디스크 검사를 통과해야 시작하며, 실물 로봇에는 접근하지 않습니다. MCP 선택 의존성이 필요합니다(`uv sync --extra mcp`).

이번 새 실행에서 확인한 결과는 다음과 같습니다. 왼팔 강체 패드 접촉 시뮬레이션 한 조건씩의 확인이며, 현장 성공률을 측정한 결과가 아닙니다.

| 실행 조건 | 결과 | 원시 기록으로 확인한 내용 |
| --- | --- | --- |
| 정상 위치, 복구 끔 | 통과 | 물리 시간 18.008초, 2,162샘플, 닫기 전 접촉 없음 |
| 컵 위치 −5mm, 복구 끔 | 조기 접촉으로 중단 | 6.425초, 중간 높이 정렬 단계에서 0.899N 접촉, 기준 0.02N |
| 같은 −5mm, 복구 켬 | 통과 | 접촉 후 후퇴, 7.600초 재계획, 최종 시도에서 조기 접촉 없이 상승 검사 통과 |

복구는 기존 후퇴·재접근 제어와 시뮬레이터 정답 좌표를 사용합니다. LLM 판단, 자동 코드 수정, 카메라 위치 추정, 실물 파지 성능을 입증하는 흐름은 아닙니다. MCP는 저장된 성공 표시만 옮기지 않고 닫기 전 단계의 접촉력을 다시 계산하고, 물리 시계·사건 시각·시도 번호·최종 시도의 접촉 유무를 대조합니다.

결과 화면은 출력 경로의 `report/index.html`입니다. 한 시뮬 영상에서 세 조건을 전환하고, **문제 시점 보기**로 검출 시각을 확인합니다. 영상은 스크롤 중에도 보이며, 컵을 닫은 뒤의 정상 접촉력은 조기 접촉으로 세지 않습니다. 영상은 10fps, 물리 신호는 120Hz이므로 작은 접촉은 수치와 함께 확인합니다. 원시 결과와 영상은 로컬에 두고, [공유용 실행 증거](evaluations/evidence/2026-10-16_simulation_workflow.json)에는 요약·해시·판정 조건을 남겼습니다.

저장된 세 실행을 다시 검사하고 화면을 만들려면 다음처럼 실행합니다. 이 명령은 시뮬레이터를 새로 시작하지 않습니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops sim-check --from-runs .local/simulation-check/run1 \
  --out .local/simulation-check/review1
```

현재 호스트는 실행 중인 NVIDIA 커널 모듈과 설치 라이브러리 버전이 달랐습니다. 이번 검증은 같은 버전의 사용자 라이브러리와 Vulkan 드라이버를 프로세스별 임시 환경에 연결해 수행했습니다. 전역 드라이버는 변경하지 않았으며 호스트 전체가 복구됐다는 뜻은 아닙니다. 이 임시 환경은 제품의 자동 설치 기능에 포함하지 않습니다. NVIDIA 구성요소의 버전을 맞추는 정식 복구 절차는 [Ubuntu 드라이버 문제 해결](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/)을 따릅니다.

## 현업 효용 검증 상태

현재는 **수집한 로봇 기록에서 확인할 구간을 골라 영상·관절 그래프로 검토하는 도구**로 사용할 수 있습니다. `robot-ops inspect`가 관절별 정체 후보, 기록 간격, 검사하지 못한 신호를 표시합니다. 사람의 검토를 돕는 실험용 검사이며, 원인 확정·정상 데이터 자동 승인·현업 도입 효과는 검증되지 않았습니다.

관절별·시간 기준 검사(v2)는 기존 개발용 시험의 주입 구간 11개 중 10개를 찾았습니다(v1 4개). 하지만 실제 기록 91개 중 89개에 경고가 생겼고, 정상적인 계단형 센서 값도 분해능 정보가 없으면 경고했습니다. 따라서 검출 범위 확대와 과민 경고 가능성을 함께 평가합니다. 이 자료는 모두 개발에 사용한 기존 자료라 신규 홀드아웃 정확도로 계산하지 않습니다. [평가 방법·실패 조건·도입 판단](docs/field-validation.md)과 [v2 비교 결과](evaluations/evidence/2026-10-16_joint_time_validation.json)에 근거를 남겼습니다.

## 수집 기록 검사와 영상 확인

LeRobot v3 데이터셋들이 있는 폴더를 지정하면 CPU에서 파일을 읽어 전체 목록과 기록별 확인 화면을 만듭니다. 이 경로는 ROS 2·실물 로봇·시뮬레이터를 시작하지 않습니다. 프로파일의 `lerobot` 환경에 numpy·pyarrow가 필요합니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops inspect --dataset-root "$HOME/so101_datasets" \
  --profile profiles/bimanual.toml --out .local/recording-inspection/all
```

결과는 `$HOME/robot-ops-agent/.local/recording-inspection/all/index.html`입니다. 기록을 선택하고 **이 구간 보기**를 누르면 해당 시각의 실물 녹화·저장된 모델 재생·관절 그래프를 함께 확인합니다. 기록별 화면에서 영상은 스크롤 중에도 남습니다. 같은 원본 해시와 관절 기록이 일치하는 기존 영상만 연결하며, 없는 영상은 없다고 표시합니다. 모델을 새로 렌더하지 않습니다. 전체 목록은 영상이 없는 기록도 포함합니다.

표준 `robot-ops-trajectory/1` JSONL 한 건은 `--trajectory "$HOME/path/to/trajectory.jsonl"`로, 정규화한 묶음은 `--corpus "$HOME/path/to/manifest.json"`으로 검사할 수 있습니다. `--profile`은 저장 영상 연결에 사용하며 JSONL 검사에서는 생략할 수 있습니다. HTML은 로컬 파일로 열거나 Range 지원 서버로 볼 수 있습니다.

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops report serve --dir .local/recording-inspection/all --port 8876
```

새 관절별 검사는 MCP `analyze_trajectory` 응답의 `recording_check`에도 포함됩니다. 기존 재생 분석의 `freeze`와 TCP 결과는 종전 규칙을 유지합니다. 에이전트가 규칙을 선택하고 원인을 확정하는 사용자용 `ask` 루프는 아직 없습니다.

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
- MCP의 `analyze_trajectory`는 허용된 `trajectory/1` JSONL을 CPU로 읽어 관절 추종 오차, TCP 거리, 기존 관측 동결 구간과 새 관절별 정체 후보를 요약합니다. 원시 시계열은 응답에 싣지 않습니다.
- 이동 명령, 서비스 호출, 프로세스 재시작, 임의 셸은 도구 allowlist에 없습니다.
- MCP의 미등록 도구와 잘못된 스키마 요청도 실행 전에 감사 로그에 기록합니다.
- llama.cpp의 스트리밍 응답에서 TTFT·처리량·서버 RSS·답변 개념·인용 경로를 함께 측정합니다.

기본 `hash` 임베더는 오프라인 회귀 테스트용입니다. 의미 기반 검색 성능을 나타내지 않으며, 검색 정확도 근거로 사용하지 않습니다.

현재 의미 임베딩은 검색 경로에 연결돼 있지만, 생성 모델은 벤치마크 하네스에서만 사용합니다. 저장된 공개 사례에서는 실제 MCP 호출 세 건을 이어 문서 검색, 로그 조회, 궤적 분석과 감사 기록을 남깁니다. 이 시연에는 LLM 판단, 실시간 ROS 조회, 실물 동작이 없습니다. 검색·ROS 2 조회·생성 모델을 한 번에 호출하는 사용자용 `robot-ops ask` 진단 루프도 아직 구현하지 않았습니다. 따라서 현재 결과를 `자동 진단 완료`나 `오류 자동 수정`으로 표현하지 않습니다. 모델을 어디서 실행하고 진단·수정 범위를 어떻게 나눌지는 [로컬 LLM 실행과 진단·수정 경계](docs/local-llm-and-remediation.md)에 정리했습니다.

### 사고 재생·회귀 평가

- `robot-ops doctor`: 프로파일에 지정된 재생·회귀 환경을 읽기 전용으로 검사합니다. 실행 파일·저장소 상태·디스크 여유·GPU 상태·자원 격리 조건을 확인하며, 로봇이나 시뮬레이터를 시작하지 않습니다.
- `robot-ops replay`: LeRobot v3 텔레옵 기록의 명령(action)과 관측(state)을 표준 궤적(`robot-ops-trajectory/1`)으로 바꾸고, 정적 구간 교정 차를 뺀 관절 추종 오차, 명령·관측 TCP 거리, 관측 동결 구간을 계산합니다. `--ghost`를 주면 명령 자세(반투명)와 관측 자세를 같은 카메라로 겹친 고스트 렌더 영상을 만듭니다.
- `robot-ops regress`: 시나리오의 고정 격자(예: 컵 위치 x, y ∈ {−5, −2.5, 0, 2.5, 5} mm)를 조건 A/B로 실행하고, 격자점마다 시뮬레이터 정답 좌표로 판정합니다. 반복 점을 다시 실행해 판정 일치율을 따로 적습니다. `--rejudge`는 시뮬을 다시 돌리지 않고 저장된 결과 파일로 판정 항목만 다시 계산합니다. 판정(verdict)이 바뀌면 그 결과 파일을 쓰지 않고 멈춥니다.
- `robot-ops report build`·`report serve`: 정적 사이트(`.local/report`)를 굽고 로컬에서 미리 봅니다. `[report.agent_demo]`가 있으면 실제 MCP 실행 기록과 같은 관측 동결 사례를 첫 화면에 놓습니다. 실측 녹화와 고스트 시뮬레이션은 계속 보이고, 검색·로그 조회·궤적 분석 단계가 영상 위에서 바뀝니다. 단계 시각은 흐름을 설명하기 위한 구성이며 도구 실행 시간의 재현이 아닙니다.
- 분석 사례는 ① SO-101 실측 기록 재생, ② 양팔 로봇 컵 접촉 시뮬 회귀 평가로 나뉩니다. 따르기 대표 영상은 저장된 동작 예시이며, 이 두 사례의 평가 결과와 구분합니다. 컵 격자에서는 녹화와 함께 패드 접촉력·컵 이동 시계열, 판정 임계, 실제 비율 배치도를 확인합니다. 녹화가 없는 칸을 선택하면 대표 영상의 위치와 선택한 칸에 녹화가 없다는 안내를 표시합니다.
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

## 재생·회귀 실행 전 확인

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops doctor --profile profiles/bimanual.toml --task cup_contact
```

`--task`를 생략하면 프로파일의 모든 과제를 검사합니다. JSON의 각 검사 결과는 `ok`, `warning`, `blocked`로 나뉘며, 차단 항목이 없으면 종료 코드 0, 있으면 2를 반환합니다. 팀 저장소의 미커밋 변경은 차단하고 기준 커밋과의 차이는 경고합니다. GPU 과제는 메모리 상한을 적용할 systemd 사용자 환경과 GPU 조회가 모두 필요합니다.

이 명령은 실행 전 기본 조건을 확인합니다. 서드파티 패키지 import, 시뮬레이터의 실제 실행, ROS·LLM 연결, 결과 정확도나 성능을 검증하지는 않습니다. 기존 `replay`·`regress`의 실행 중 검사와 별도로 사용하는 진단 명령입니다.

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

저장된 관측 동결 사례를 실제 MCP 호출로 다시 분석해 리포트용 실행 기록을 만들 수 있습니다.

```bash
cd "$HOME/robot-ops-agent"
PYTHONPATH=src .venv/bin/python -m robot_ops.agent_demo \
  --profile profiles/bimanual.toml
```

이 명령은 프로세스 안에서 `search_documents` → `log_tail` → `analyze_trajectory`를 호출하고, 응답과 시작·완료 감사 기록을 `.local/agent-demo`에 저장합니다. 현재 기록에서는 248프레임 가운데 4.4~6.9초의 26프레임 동결과 최대 TCP 거리 170.459 mm를 확인했습니다. 저장된 궤적을 CPU로 읽는 시연이므로 ROS 2나 시뮬레이터를 시작하지 않고 실물 로봇에도 명령을 보내지 않습니다.

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

프로파일에 `[report.agent_demo]`가 있으면 `auto` 빌드는 `agent_replay`를 우선합니다. 실행 기록, 감사 로그, 응답, 궤적, 영상의 출처와 해시가 모두 맞아야 하며, 자료가 없거나 trace 무결성 검사가 실패하면 미디어 빌드를 중단합니다. 설정이 없는 프로파일만 기존 양팔 따르기, 컵 격자, 관측 동결 순서로 전환합니다. `--hero agent_replay`·`bimanual_pour`·`cup_grid`·`observation_freeze`로 대표 소스를 지정할 수 있습니다.

공개 리포트에는 원시 MCP 응답과 감사 로그를 복사하지 않습니다. 검증된 출처 해시, 도구 상태, 실행 시간, 동결 구간 요약만 싣습니다. 대표 영상은 실측 녹화와 저장된 고스트 시뮬레이션을 계속 보여 주며, 단계 전환 시각은 검색과 분석 흐름을 설명하기 위한 구성입니다. 리포트를 만들 때 ROS 2, 시뮬레이터, 실물 로봇은 실행하지 않습니다.

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
- 어댑터가 시작하는 팀 코드는 컵 접촉 시뮬레이터와 주행 제어 모듈이며, 프로파일 `[adapter.allowlist]`에 각 파일의 `simulation/`·이전 `tools/` 경로를 명시합니다. 컵 실행은 현재 저장소의 `simulation/simulate_cup_contact.py`를 우선하고, 이전 구조에서는 `tools/simulate_cup_contact.py`를 사용합니다. 주행 재생은 요청한 커밋 안에서 `tools/mobile_service_control.py` 또는 `simulation/mobile_service_control.py`를 찾아 사본으로 읽습니다. 허용 목록 밖 요청은 거부하며, 어댑터의 경로 후보가 프로파일과 같은지는 테스트가 확인합니다.
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
