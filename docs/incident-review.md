# 실패 기록 검토 계약

실패로 지정된 저장 실행 한 건에서 사람이 먼저 볼 구간을 고릅니다. 고장 여부를 전체 데이터셋에 자동 분류하거나 원인을 확정하지 않습니다. 코어와 화면은 CPU에서 동작하며 실물·ROS 명령·시뮬레이터를 시작하지 않습니다.

## 입력과 실행

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops investigate --trajectory "$HOME/records/before.jsonl" \
  --events "$HOME/records/events.json" --after "$HOME/records/after.jsonl" \
  --profile profiles/bimanual.toml --out .local/incident-review
```

`--events`, `--after`, `--profile`은 선택 사항입니다. CLI는 지정한 파일을 읽어 로컬 결과를 쓰며, MCP `investigate_recording(path, events_path=None, after_path=None)`는 허용된 경로의 파일을 읽고 JSON 요약만 반환합니다. MCP는 파일·행·출력 크기와 일반 파일 여부를 검사하고 호출을 감사 기록에 남깁니다.

`--mcp`는 실제 in-process MCP 클라이언트·서버로 기본 기록 조회, 제공된 로그 대조, 변경 후 비교를 차례로 호출합니다. `mcp` 선택 의존성이 필요합니다. 출력 `trace`에 호출 이유, `execution`에 호출 수, `mcp-audit.jsonl`에 MCP·코어 감사 내역을 남깁니다. 화면을 만드는 시점에도 원본 해시를 대조해 도구 호출 이후 파일이 바뀌면 거부합니다. 이 실행기는 결정적 정책이며 자연어 LLM 조사 루프는 포함하지 않습니다.

궤적은 기존 `robot-ops-trajectory/1` 형식을 사용합니다. 각 행의 `t`는 엄격히 증가하며, `q_cmd`와 `q_obs`의 단위는 rad입니다. 선택 항목 `source_sample_timestamp_s`, `source_sequence`가 있으면 측정 식별자 반복을 검사합니다. 절대 센서 시각과 궤적 시각을 빼서 지연이라고 해석하지 않습니다.

로그 묶음 예시(해시는 실제 궤적 파일 SHA-256으로 바꿉니다):

```json
{
  "schema": "robot-ops-incident-events/1",
  "trajectory_sha256": "실제 궤적 파일의 SHA-256",
  "time_basis": "trajectory",
  "events": [
    {"t": 4.2, "level": "error", "message": "기록된 제어기 오류 메시지"}
  ]
}
```

로그 `t`는 궤적과 같은 시계·같은 원점으로 변환한 값이어야 합니다. 시간 동기화가 확인되지 않은 wall-clock 로그를 임의로 붙이지 않습니다. 다른 궤적 해시, 범위 밖 시각, 유효하지 않은 수준·메시지는 거부합니다. 수준은 `info`, `warning`, `error`입니다. 로그는 실행할 지시가 아닌 텍스트 근거로만 다룹니다.

## 검사 선택과 결과

1. 기존 관절별 시간 검사로 위치 반복과 샘플 공백을 찾습니다.
2. 위치 반복이 있을 때 측정 식별자를 검사합니다. 식별자가 없거나 일부 빠졌으면 추가 수집을 요청하고, 반복되면 센서 발행·수집기 상태 확인을 제시합니다. 식별자가 변해도 센서 정상이나 기구 걸림을 확정하지 않습니다.
3. 후보 주변의 정렬된 로그를 대조합니다. 오류가 있으면 오류 발생 조건과 제어 상태 확인을 우선 제시합니다. 시간상 인접함은 원인 확정 근거가 아닙니다.
4. 겹치는 경고를 합친 후 첫 구간을 보존하고, 신호 종류와 지속시간을 기준으로 나머지를 골라 최대 3개를 제시합니다.

긴 경고는 기본 2초와 최초 검사 확인 시각을 포함하는 구간부터 보여 줍니다. 2초는 제안 기본값이며 사람의 검토 시간으로 최적화한 값이 아닙니다. 샘플 공백은 양쪽 샘플을 확인할 수 있도록 축약하지 않습니다. JSON의 `start_s/end_s`는 제시 구간, `full_start_s/full_end_s`는 전체 경고 범위입니다. 전체 시계열을 지우지 않습니다.

`trace`에는 실행한 검사와 선택 이유·결과, `next_checks`에는 다음 확인 항목과 필요한 기록을 남깁니다. `cause_status`는 `undetermined`입니다. 후보가 없어도 실행 성공이나 정상 데이터로 승인하지 않습니다.

`--after`는 같은 명령·시각·로봇 구성·검사 설정·필드 유무인지 먼저 확인합니다. 하나라도 다르면 비교 불가로 표시합니다. 같으면 경고 수와 구간을 대조하며, 작업·환경의 일치 및 수정의 인과 효과는 `not_verified`/`not_assessed`로 남깁니다. 원인을 해결했다는 판정으로 사용하지 않습니다.

## 평가

```bash
cd "$HOME/robot-ops-agent"
uv run robot-ops investigate-evaluate --manifest "$HOME/records/evaluation.json" \
  --out .local/incident-evaluation
```

평가 manifest 스키마는 `robot-ops-incident-evaluation/1`이며 `cases` 항목마다 아래 필드가 필요합니다.

| 필드 | 의미 |
| --- | --- |
| `id`, `group_id` | 사례 ID와 같은 수집·파생 자료를 묶는 그룹 |
| `split` | `development` 또는 `evaluation` |
| `trajectory_path`, `trajectory_sha256` | 입력 경로와 파일 해시 |
| `expected_onset_s` | 별도 근거로 정한 사건 시작 시각. 정상 대조는 `null` |
| `label_source` | `synthetic_injection` 또는 `independent_review` |
| `label_reference` | 주입 일정이나 독립 검토 원본의 참조 |

같은 파일·같은 시계열의 중복과 같은 그룹이 개발·평가로 갈리는 누수를 거부합니다. 파생 자료의 관계는 `group_id`를 올바르게 지정해야 합니다. 구간 선택 정확도는 상위 3개 제시 구간에 정답 시작점이 포함되는지 기본 허용 오차 0.2초로 측정합니다. 정상 대조의 경고 수를 따로 집계합니다. 합성 결과는 현장 홀드아웃 결과로 표시하지 않습니다.

선택 항목 `human_trials`에는 `case_id`, `reviewer`, `method`(`manual`/`assisted`), 양수 `duration_s`, 불리언 `correct`를 기록합니다. 같은 검토자·사례의 두 방법이 모두 있을 때만 시간 차이와 정답률을 짝지어 집계합니다. 이는 표본 내 관찰이며 학습·순서 효과를 제거한 인과 실험은 아닙니다. 현장 도입 판단에는 처음 보는 사례, 별도 라벨, 순서 효과를 통제한 사람 검토 실험이 추가로 필요합니다.

기존 실측 corpus와 공개 동결 사례는 개발 자료입니다. 여기서 얻은 경고 시각을 독립 정답으로 재사용하지 않습니다. 제시 구간의 초 수와 프로그램 실행 시간도 사람의 조사 시간 절감으로 바꾸어 보고하지 않습니다.
