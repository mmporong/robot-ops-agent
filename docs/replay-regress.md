# 사고 재생·회귀 평가 설계

실행 기록을 다시 읽어 명령과 관측이 언제부터 어긋났는지 수치와 영상으로 보여 주고, 재현한 사고를 회귀 시나리오로 저장해 결정적 격자로 다시 평가하는 기능의 설계 문서다. CLI는 `robot-ops replay`, `robot-ops regress`, `robot-ops report build|serve` 셋이다.

## 1. 구조: 코어와 어댑터

```text
robot-ops CLI (uv, stdlib 코어)
  │  profiles/<id>.toml  ── (동사, 과제) → 인터프리터
  ▼
src/robot_ops/replay/            adapters/<id>/entry.py
  profile · schema · adapter ──▶ <interp> entry.py <convert|run|judge>
  divergence · grid · regress       --task <task> --request req.json
  scenario_bank · artifacts         --response resp.json --workdir <dir>
  media · report                      │
  ▲                                   ▼
  └── 응답 JSON 파일만 믿는다 ◀── 결과 파일·궤적·영상
```

- **코어**(`src/robot_ops/replay`)는 표준 라이브러리만 import한다. 로봇·시뮬레이터·numpy를 모른다. 프로파일 로드, 스키마 검증, 어댑터 호출과 자원 격리, 어긋남 계산, 격자·판정 일치율, 시나리오 status, 산출물 manifest와 삭제, ffmpeg 명령 조립, 정적 리포트를 맡는다.
- **어댑터**(`adapters/<id>/`)는 로봇별 코드다. 코어가 subprocess로 부르고, 응답 파일만 진실로 본다. stdout·stderr는 로그(`<verb>.log`)로만 남긴다.
- 문서 검색·MCP·LLM 경로와 코드를 공유하지 않고 서로 호출하지 않는다.

### 1.1 어댑터 프로토콜 `robot-ops-adapter/1`

동사는 `convert | run | judge` 셋뿐이고, 그 밖의 동사는 코어가 호출 전에 거부한다.

| 동사 | 요청 | 응답(ok일 때) |
| --- | --- | --- |
| `convert` | `{source: {kind: "lerobot_v3", path, episode}}` | `{trajectory_path, sha256, fps, n_frames, video}` |
| `run` | `{scenario_id, task, condition, params, grid_point, repeat, record}` | `{run_dir, result_path, trajectory_path?, video_path?, sim_time_s, wall_s, tool_sha256, hardware_accessed: false}` |
| `judge` | `{result_path, task}` | `{verdict: pass\|fail, judgement_basis, assembly, checks: [{id, pass, value, threshold, unit}], failure_code}` |

- 공통 응답 `{"protocol": "robot-ops-adapter/1", "status": "ok" | "unsupported" | "infra_error", "error"?}`. 종료 코드 0 / 2(unsupported) / 3(infra_error), 그 밖은 crash로 본다. 시간 초과는 코어가 `timeout`으로 기록한다.
- 궤적이 있는 과제(`lerobot_episode`, `drive_kinematic`)의 `run` 응답에는 `trajectory_path`가 있어야 한다.
- `entry.py`는 동사·과제 모듈을 지연 import한다. core 인터프리터로 `judge`를 돌릴 때 서드파티 모듈이 로드되지 않아야 한다(`tests/test_bimanual_judge_cpu.py`).

### 1.2 (동사, 과제) → 인터프리터

프로파일이 과제마다 동사별 인터프리터를 정한다. `core`는 예약어로, 코어를 돌리는 stdlib 인터프리터를 뜻한다.

```toml
[interpreters]
lerobot = "~/miniforge3/envs/lerobot/bin/python"      # numpy·pyarrow
leisaac = "/data/$USER/conda-envs/leisaac/bin/python" # Isaac 5.1
rlwalk  = "~/miniforge3/envs/rlwalk/bin/python"       # MuJoCo 미러

[adapter.tasks.lerobot_episode]
convert = "lerobot"   # parquet → trajectory(q_cmd, q_obs)
run     = "rlwalk"    # MuJoCo 미러: TCP 계산 + 고스트 렌더

[adapter.tasks.cup_contact]
run   = "leisaac"
judge = "core"        # stdlib, 결과 파일만 읽음

[adapter.tasks.drive_kinematic]
run   = "lerobot"
judge = "core"
```

- 팀 모듈을 import하는 (동사, 과제)에 `core`를 지정하면 프로파일 로드가 실패한다.
- 경로 확장은 선두 `~`와 `$USER`·`${USER}`만 한다. 그 밖의 `$` 변수는 거부하고, 확장 뒤 절대경로여야 한다.

### 1.3 프로파일 (`profiles/bimanual.toml`)

| 절 | 내용 |
| --- | --- |
| `[project]` | `id`, `source_repo`(팀 저장소, 읽기 전용), `pinned_commit`(불일치면 경고, dirty tree면 regress 거부) |
| `[adapter]` | `entry`, `protocol` |
| `[interpreters]`, `[adapter.tasks.*]` | 위 1.2 |
| `[adapter.allowlist]` | 어댑터가 실행·import할 수 있는 팀 파일 전부. `scripts = ["tools/simulate_cup_contact.py"]`, `imports = ["tools/mobile_service_control.py"]`. 원본은 이 표이고, 실제로 막는 지점은 어댑터 쪽이다: 코어는 요청에 `scripts`·`imports`를 싣지 않고(코어 검사는 요청에 값이 있을 때만 걸린다), 각 run 모듈이 여는 팀 파일을 상수(`TEAM_SCRIPT`·`TEAM_FILE`)로 고정한다. `tests/test_replay_profile.py`가 `entry.py`의 `TEAM_SCRIPTS`·`TEAM_IMPORTS`가 이 표와 같고 run 모듈 상수가 그 안에 있는지 확인한다. |
| `[mirror]` | SO-101 MuJoCo 미러 위치, 작업 물체 제거 여부 |
| `[limits]` | `timeout_s`, `gpu_tasks`, `gpu_tolerance_mib`, `gpu_settle_s`, `memory_max`, `min_free_disk_gb`, `lock_file`, `per_run_keep_mb`(삭제 뒤 run 폴더 보존 크기 상한, MP4 제외. 넘으면 regress 결과 `warnings`에 남기고 실패로 치지 않는다) |
| `[artifacts]` | 산출물 root(`/data/$USER/robot-ops/bimanual`), 리포트 출력(`.local/report`), `delete_patterns` |
| `[replay]` | 임계 `joint_deg = 3.0`, `tcp_mm = 20.0` |
| `[public]` | `exclude_status`: 공개 빌드에서 뺄 시나리오 status |

## 2. 판정 조건과 출처 배지

판정은 로그 문자열이나 LLM이 아니라 시뮬레이터 정답 좌표와 저장된 판정 필드로 한다. 모든 결과에 세 가지 표기를 같은 등급으로 붙인다.

| 필드 | 값 | 화면 표기 |
| --- | --- | --- |
| `origin`(출처) | `real_recording` | 실측 기록 |
| | `sim_run` | 시뮬 실행 기록 |
| | `fault_injection` | 오류 주입(시뮬) |
| `judgement_basis`(판정 조건) | `rigid_proxy_lift` | 강체 패드 접촉 시뮬 기준 |
| | `oracle_recovery_sim_coordinates` | 정답 좌표 복구 조건 |
| | `kinematic_drive` | 차동 구동 운동학 모델 |
| | `isaac_drive` | 물리 시뮬 주행 |
| | `physical` | 실물 |
| `assembly`(구성) | `left_arm_proxy` / `bimanual_full` / `single_arm_so101` | 왼팔 단독 / 양팔 전체 / SO-101 단일 팔 |

규칙

- 문구는 조건을 중립적으로 적는다. 조건 B가 모든 칸을 통과해도 "정답 좌표 복구 조건"을 함께 적는다.
- 조건 이름은 과제가 정한다. 컵 과제는 `A = 복구 끔`, `B = 복구 켬(정답 좌표 조건)`이고, 코드 버전 대조가 아니므로 "수정 전/후"라고 쓰지 않는다.
- 컵 `judge`는 저장된 `task_pass`(없으면 `rigid_proxy_lift_pass`)만 판정으로 쓰고 새 판정을 만들지 않는다. 같은 폴더의 `plan.json` 설정으로 `samples`에서 상승 판정 항목을 다시 계산해 `checks`에 붙이고, 저장값과 다르면 `sample_recheck.agrees = false`로 남긴다. 결과의 `recovery_enabled`가 참이면 `judgement_basis`는 `oracle_recovery_sim_coordinates`다.
- 조기 종료로 측정할 수 없는 검사는 `pass = null`(측정 안 됨)로 두고, 판정을 결정한 실패 이벤트를 첫 행 `failure_event`로 적는다.
- 재생 불가 고정: `replayable.layer == "hardware"`, 전기·펌웨어 사고 코드(`SERVO_THERMAL_LATCH`, `OVERLOAD_TRIP`, `USB_DEADLOCK`, `BUS_DEATH`, `I2C_BUS_DEATH`), 궤적 소스 없음 중 하나면 코어가 `replayable.value = false`로 고정한다. 리포트에는 한 줄과 접이식 목록으로만 나온다.

### 2.1 시나리오 status

- `status`는 단일값이고, 동시에 성립하는 모델 관련 사실은 `model_note`에 보존한다(`not_reproducible_in_model`).
- status 규칙은 코드 수정 전(A)과 수정 후(B)를 대조하는 과제(`drive_kinematic`)에만 적용한다. A에서 실패가 없으면 `not_reproducible_in_model`, A에서 재현됐고 B에 실패가 없으면 `stale`, 그 밖은 `active`다. 같은 격자점에서 A 통과·B 실패인 칸이 있으면 우선순위가 가장 높은 별도 status가 붙는다. 이 status는 프로파일 `[public] exclude_status`에 들어 있어, 공개 빌드에서는 카드·regress 데이터·미디어·개수까지 모두 빠진다. 우선순위와 값 목록은 `src/robot_ops/replay/scenario_bank.py`에 있다.
- 컵 과제는 status 규칙을 적용하지 않는다(A/B는 기능 켬/끔이다).

## 3. 데이터 형식 요약

형식 검증은 `src/robot_ops/replay/schema.py` 하나가 맡는다.

### 3.1 궤적 `robot-ops-trajectory/1` (JSONL)

- 헤더: `{"schema", "robot_id", "arm", "joint_names", "units": "rad", "fps", "source": {kind, path, episode, sha256}, "origin"}`
- 행: `{"t", "q_cmd": [...], "q_obs": [...], "gripper_cmd", "gripper_obs", "tcp_cmd_m": [x, y, z], "tcp_obs_m": [x, y, z]}`
- TCP는 SO-101 MuJoCo 미러로 계산한 팔 기준 모델 좌표다. 고스트 렌더와 같은 모델이며 영상 측정값이 아니다.

어긋남 계산(`divergence.py`)

1. 정적 구간의 관절별 평균 `q_cmd − q_obs`를 교정 차로 보고 빼서, 텔레옵 리더·팔로워 교정 차를 제거한다.
2. 관절별 추종 오차(도)와 명령·관측 TCP 거리(mm)를 프레임마다 계산하고, 임계(관절 3.0°, TCP 20 mm)를 처음 넘은 시각을 기록한다.
3. 관측 동결: 모든 관절(그리퍼 포함) |Δq_obs| < 0.1° 이고 어느 관절이든 |Δq_cmd| > 0.5°인 프레임이 3개 이상 이어지면 동결 구간이다.
   구간 표기는 시작 = 조건이 처음 성립한 프레임(Δ가 i−1→i인 프레임 i)의 시각, 끝 = 마지막 동결 프레임의 시각이다. README·리포트·`scenario.json`의 대표 구간 4.4–6.9 s(26프레임)가 이 규약이다. M0 재고표(`evaluations/replay/inventory.json`)의 `longest_run_t_s` [4.3, 6.9]는 차분의 출발 프레임을 시작으로 적어 시작이 1프레임(0.1 s) 앞선다. 같은 구간이다.
4. 교정 차가 5°를 넘는 관절이 있으면 `calibration_suspect`를 TCP 지표에만 붙인다(관절 오차는 이미 교정 차를 뺐다).

### 3.2 `scenario.json` (`robot-ops-scenario/1`)

| 키 | 내용 |
| --- | --- |
| `scenario_id`, `title_ko`, `adapter`, `task` | 식별자와 과제(`cup_contact` · `drive_kinematic` · `lerobot_episode`) |
| `origin`, `judgement_basis`, `assembly` | 2절의 배지 값 |
| `replayable` | `{value, layer, reason}` |
| `sources` | `[{path, sha256, role}]`, role = `incident_run` · `recording` · `failure_record`. 경로는 `~`로 정규화 |
| `incident` | `{t_start_s, t_end_s, code, detected_by}` |
| `conditions` | `{A: {label_ko, params, judgement_basis}, B: {...}}` |
| `grid` | `{spawn_x_offset_mm, spawn_y_offset_mm, repeat_points, repeats}` |
| `verifiers`, `limits`, `status`, `model_note`, `provenance` | 검사 목록, 시나리오별 timeout, 2.1의 status |

### 3.3 `regress_result.json` (`robot-ops-regress/1`)

| 키 | 내용 |
| --- | --- |
| `regress_id`, `profile`, `scenario_id`, `task`, `mode` | `mode`는 `grid`(본 격자) 또는 `record`(녹화 패스) |
| `conditions.<A\|B>` | `label_ko`, `judgement_basis`, `cells`, `pass`, `valid`, `invalid` |
| `cells[]` | `{x_mm, y_mm, status: ok\|infra\|timeout, verdict, failure_code, checks, run_id, sim_time_s, wall_s, video_path?}` |
| `determinism` | 첫 조건의 반복 점 × 반복 횟수, `verdict_agreement`, `max_metric_spread`, 점별 판정 목록 |
| `provenance` | 팀 저장소 커밋, `adapter_sha256`, 팀 도구 파일별 `tool_sha256`, `sim_version` |
| `gpu_check` | GPU 과제일 때 실행 수, 원복 여부, 최대 초과량, 대기 시간 |
| `scenario_status`, `warnings`, `created_at`, `wall_s` | |

- `valid`는 status가 ok인 칸 수다. `pass`는 그중 통과 수이고, ok가 아닌 칸(infra·timeout)이 3개를 넘으면 `invalid = true`로 지도 전체를 무효로 본다.
- `robot-ops regress --rejudge`는 이 시나리오의 모든 `regress_result.json`(격자·녹화)을 대상으로, 칸마다 저장된 `run.response.json`의 결과 파일로 `judge`만 다시 돌려 `checks`·`failure_code`를 갱신한다. 한 결과 파일 안에서 `verdict`가 하나라도 바뀌면 그 파일을 쓰지 않고 멈춘다(앞서 처리한 다른 결과 파일의 갱신은 남는다). 처음 갱신할 때 원본을 `regress_result.before-rejudge.json`으로 한 번만 보존하고 `rejudged_at`을 남긴다.

### 3.4 `replay_result.json` (`robot-ops-replay/1`)

`{run_id, profile, task, origin, source: {path, episode, sha256}, trajectory_path, video_path, convert_video, divergence, warnings, created_at}`. `divergence`에는 정적 구간, 교정 차, 동결 기준, 관절별 최대 오차, TCP 요약, 사고 구간(`freeze`·`joint_tracking`·`tcp_distance`), 그래프용 시계열이 들어간다.

## 4. 산출물과 삭제 규칙

- 산출물 root는 저장소 밖 `/data/$USER/robot-ops/bimanual`이다. run마다 `runs/<run_id>/`에 요청·응답·로그·결과를 두고, `manifest.json`에 생성 파일의 상대경로·크기·SHA-256과 격자 오프셋을 기록한다.
- 삭제는 다음을 모두 만족할 때만 한다. 하나라도 어기면 `DeletionRefused`("외부 경로 삭제 거부")로 멈춘다.
  1. run 폴더 기준 상대경로가 `delete_patterns`(`scene.usda`, `frames/*.png`, `isaac/scene.usda`, `isaac/frames/*.png`) 중 하나와 경로 조각 단위로 맞는다.
  2. manifest에 기록돼 있고, 현재 크기·SHA-256이 기록과 같다.
  3. realpath가 산출물 root 아래에 있다.
  4. root부터 대상까지 경로 조각 중 심링크가 없다.
- Isaac의 `scene.usda`(약 78 MB)는 manifest에 SHA·크기를 남긴 뒤 실행 직후 지운다. 어댑터는 아무것도 지우지 않는다. 하드웨어 가드 위반(`HardwareGuardError`)으로 regress가 멈출 때도 그 run의 manifest 기록과 삭제는 먼저 하고 예외를 올린다. GPU 원복 실패(`GpuMemoryNotReleased`)는 정리 뒤에 판정하므로 같은 보장을 받는다.
- 팀 저장소의 파일은 읽기만 하고 삭제 대상이 될 수 없다(root 밖).
- 리포트는 `.local/report`(Git 추적 제외)에 굽는다. 클립 ≤ 3 MB, 히어로 ≤ 6 MB, 사이트 ≤ 60 MB 예산을 넘으면 경고를 남긴다. 빌드 산출물 전체에 `/home/<user>`·`/data/<user>` 절대경로가 남아 있으면 빌드가 실패한다.

## 5. 자원 상한

| 항목 | 값·방식 |
| --- | --- |
| 격리 | `run` 동사는 `systemd-run --user --scope --unit robot-ops-<run_id> -p MemoryMax=20G timeout --kill-after=30 <timeout_s> ...`로 실행하고, 끝나면 `systemctl --user stop`으로 scope를 정리한 뒤 남은 unit이 0인지 확인한다. systemd user 세션을 쓸 수 없으면 새 세션(`start_new_session`) + `killpg`로 정리하고, 이 run 표식(`ROBOT_OPS_RUN_ID`)을 가진 자기 소유 프로세스를 `/proc`에서 찾아 끝낸다. 이 경로에는 MemoryMax가 적용되지 않는다. 명령줄 패턴 매칭(`pkill -f`)은 쓰지 않는다 |
| 시간 상한 | `timeout_s = 180`. Isaac 컵 1회를 600초 상한으로 먼저 실측(24.7초, 종료 코드 0)하고 `max(180, 24.7 × 2)`로 정했다. 본 격자 65회의 칸별 실행 시간은 14.2–28.1초였다 |
| 단일 실행 | 프로파일 `lock_file`에 `fcntl.flock`(비차단 배타 잠금). 이미 잠겨 있으면 바로 거부한다 |
| GPU 원복 | `gpu_tasks`(`cup_contact`)는 run마다 실행 전 `nvidia-smi memory.used`를 기준으로 잡고, 실행·정리 뒤 최대 20초 동안 1초 간격으로 다시 읽는다. 기준 + 200 MiB 안으로 돌아오지 않거나 값을 읽을 수 없으면 regress 전체를 멈춘다 |
| 디스크 | 산출물 root 여유가 5 GB 미만이면 실행 전·칸마다 거부한다 |
| 하드웨어 가드 | `run` 응답과 결과 파일의 `hardware_accessed`가 정확히 `false`가 아니거나 키가 없으면 regress 전체를 즉시 멈춘다 |
| 팀 저장소 | 모든 어댑터 subprocess에 `PYTHONDONTWRITEBYTECODE=1`. 과거 코드는 `git show <sha>:<path>` 단일 파일을 run 폴더 `snapshot/`에 꺼내 import한다 |

## 6. 재현 명령

```bash
cd "$HOME/robot-ops-agent"

# 단위 테스트 (가짜 어댑터, Isaac·MuJoCo 불필요)
uv run python -m unittest discover -s tests -v

# 코어가 서드파티 모듈 없이 import되는지 확인
env -i /usr/bin/python3 -S -c "import sys; sys.path.insert(0,'src'); import robot_ops.replay.profile, robot_ops.replay.schema, robot_ops.replay.adapter, robot_ops.replay.divergence, robot_ops.replay.grid, robot_ops.replay.scenario_bank, robot_ops.replay.regress, robot_ops.replay.artifacts, robot_ops.replay.media, robot_ops.replay.report; bad={'numpy','pyarrow','mcp','mujoco','isaacsim'} & {m.split('.')[0] for m in sys.modules}; assert not bad, bad"

# 팀 저장소 무쓰기 확인용 상태 기록
BEFORE=$(mktemp) && STAMP=$(mktemp)
git -C ~/bimanual-robot status --porcelain --ignored > "$BEFORE"

# 관측 동결 재생 + 고스트 렌더
uv run robot-ops replay --profile profiles/bimanual.toml \
  --source lerobot:~/so101_datasets/so101_teleop_bench --episode 0 --ghost

# 컵 격자 회귀: 조건 A/B 25점씩 + 조건 A 5점 × 3회 반복 = 65회
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact --condition both

# 녹화 패스: 조건 A 실패 행과 같은 좌표의 조건 B 1점, 대표 통과 1점
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact \
  --record-cells "A:-5,-5;A:-2.5,-5;A:0,-5;A:2.5,-5;A:5,-5;A:0,0;B:0,-5"

# 저장된 결과로 판정 항목만 다시 계산
uv run robot-ops regress --profile profiles/bimanual.toml \
  --scenario bimanual-cup-premature-contact --rejudge

# 실행 뒤 확인
systemctl --user list-units 'robot-ops-*' --no-legend | wc -l      # 0
nvidia-smi --query-gpu=memory.used --format=csv
git -C ~/bimanual-robot status --porcelain --ignored | diff "$BEFORE" -
find ~/bimanual-robot -newer "$STAMP" -not -path '*/.git/*' | wc -l # 0

# 리포트 굽기(기본 = 공개 빌드)와 미리보기
uv run robot-ops report build --profile profiles/bimanual.toml
uv run robot-ops report serve --dir .local/report --port 8766
```

미리보기는 `robot-ops report serve`를 쓴다. 이 서버는 HTTP Range 요청에 `206 Partial Content`로 답하므로 브라우저에서 영상 탐색(seek)과 그래프 클릭 이동이 동작한다. `python3 -m http.server`는 Range를 처리하지 않아 영상 탐색이 맞지 않는다. 서버는 기본으로 `127.0.0.1`에만 바인딩한다.

`report build --private`는 공개 제외 status의 시나리오까지 포함하는 로컬 확인용 빌드이고, 배포하지 않는다. 공개 폴더를 덮어쓰지 않도록 `<report_dir>-private`(bimanual 기준 `.local/report-private`)에 굽는다. 공개 빌드는 마지막에 산출물 전체 바이트(미디어 포함)에서 제외 시나리오 id와 제외 status 문자열을 찾고, 하나라도 남아 있으면 개인 절대경로 검사와 같이 빌드를 실패시킨다. `--hero`로 히어로 영상 소스(`auto`·`cup_grid`·`observation_freeze`)를 고른다.

### 6.1 리포트 화면 구성

| 화면 | 내용 |
| --- | --- |
| 첫 화면 `#/` | 히어로 영상(컵 격자: 조건 A 실패 격자점을 판정 순간까지 재생하고 멈춤, 그때의 패드 접촉력·컵 이동량을 글자로 얹음 → 같은 격자점 조건 B의 재계획·들어 올림 → 통과 지도 A \| B와 읽는 법 한 줄), 사례 두 가지(① 실측 기록 재생 ② 시뮬 회귀 평가)의 무엇을·왜·결과 |
| 사례 ② `#/s/<컵 시나리오>` | 네 단계 흐름(기록된 사고 → 같은 조건 재현 → 주변 25점 → 복구 켬 비교), 기록과 재현의 판정 시각·접촉력·컵 이동 비교표, 두 격자점(조건 A·B)의 접촉 증거 |
| 격자 평가 `#/eval/<시나리오>[/<조건>/<x>/<y>]` | 흐름(3·4단계 강조), 격자 읽는 법(실제 비율 도식·칸의 뜻·축 방향·여유 범위), 통과 지도, 칸을 누르면 녹화·접촉 증거·검사값. 주소에 조건·x·y를 붙이면 그 칸을 연 채로 열린다 |

접촉 증거(`data/regress-cell/<시나리오>/<조건>_x<x>_y<y>.json`, 격자점마다 하나)는 칸 실행 폴더의 Isaac 결과(`result.json`·`plan.json`·`contact_proxy.urdf`)에서 만든다.

- **시계열**: 샘플의 두 패드 접촉력(`contact_force_n`, 순서는 팀 `cup_contact_model.FINGER_LINKS` = 고정 죠 링크, 이동 죠 링크)과 컵 수평 이동(`cup_displacement_from_start_m`)을 0.05초 칸마다 최대값으로 줄인다(짧은 접촉 봉우리가 사라지지 않게). 동작 단계 구간, 사건(판정·후퇴·재계획), 임계(`minimum_contact_force_n`, `maximum_preclose_displacement_m`)는 결과·plan.json 값을 그대로 쓴다. 닫기 전 단계 목록(RESET·REORIENT_ABOVE·PREGRASP_ABOVE·ALIGN_MIDDLE·APPROACH)은 팀 규칙 `preclose_failure`(고정 커밋 `8a09a02`)와 같다.
- **패드 위치**: 결과 샘플에는 패드 위치가 없어 계산한다. 샘플의 접촉 중심 위치·그리퍼 각 + 그 단계 목표 자세의 접근축·닫힘축(왼팔: 접근 = tool z, 닫힘 = tool x, 팀 `grasp_axes` 규약) + URDF의 가정 패드 상자(`assumed_fixed_pad`·`assumed_moving_pad`)와 관절 원점. 판정 순간(실패) 또는 닫기 전 패드가 컵에 가장 가까운 순간(통과)의 두 패드 윤곽, 컵 테두리와의 수평 여유(음수 = 겹침), 패드 아래면과 컵 윗면의 높이 차, 추정 접촉 위치를 싣는다. 위에서 볼 때 겹쳐도 패드가 컵 윗면보다 0.5 mm 넘게 위에 있으면 여유 계산에서 뺀다.
- **축 방향**: 시뮬은 로봇을 장면 원점에 회전 없이 놓고 중력은 −z다. URDF에서 base_link에 고정된 `left_*` 부품이 모두 y > 0, `right_*` 부품이 모두 y < 0이면 +y = 로봇 왼쪽, 오른손 좌표로 +x = 로봇 앞으로 표기한다. 근거가 맞지 않으면 월드 x·y로만 표기한다.
- **격자 읽는 법**: 한쪽 끝에서 이어진 행(또는 열)만 전부 실패하고 나머지 칸이 모두 통과이면, 그 방향의 여유를 "안쪽 통과 행의 오프셋 이상, 실패 행의 오프셋 미만"으로 적는다(격자 간격으로 본 추정). 실패가 흩어져 있거나 원래 위치에서도 실패하면 적지 않는다. 계산한 여유가 0 미만인 칸과 조건 A 실패 칸이 몇 칸 일치하는지(`geometry_check`)를 함께 싣는다.
- **기록된 사고**: 시나리오 원천(`role = incident_run`)의 `result.json`·`plan.json`에서 판정 시각·접촉력·컵 오프셋을 읽고, 같은 오프셋의 조건 A 격자점과 판정 시각·판정 종류가 같은지(`same_time`)를 표시한다.

관측 동결 상세(사례 ①)의 고스트 캡션과 실측 대조 문장은 `mujoco_result.json`의 `scene`(예: `desk_clamp`, 치운 받침대)과 리포트 폴더 옆 `fidelity/<조건>/`의 선택 파일로 만든다. `camera_fit.json`이 있으면 "실측 3인칭 카메라 시점 근사(N점 적합, RMS px)"를 붙인다(그 조건 폴더에 없으면 고스트 카메라 설정과 같은 값의 다른 조건 적합 결과를 쓴다). `tracking_lag.json`(`lag_s_by_t`: 명령을 시간 이동해 실측 영상에 겹쳐 본 시각별 지연)이 있으면 지연 구간·범위와 이동 없이 겹친 시각을 그래프 아래에 적는다. 두 파일이 없으면 해당 문장을 생략한다. 대조 이미지 자체는 리포트에 넣지 않는다.

## 7. 새 로봇 어댑터 추가

1. `adapters/<id>/entry.py`를 만든다. `argv`는 `<verb> --task <task> --request <path> --response <path> --workdir <dir>`이고, 동사·과제별 모듈은 지연 import한다. 응답 파일에 `protocol`·`status`를 항상 쓰고 종료 코드 0/2/3을 지킨다.
2. 필요한 동사만 구현한다.
   - 실측 기록이 있으면 `convert`로 `robot-ops-trajectory/1` 궤적을 만든다(명령·관측을 같은 단위 rad로).
   - 시뮬 과제는 `run`에서 격자 오프셋(`grid_point`)과 조건 `params`로 시뮬을 돌리고 결과 파일 경로를 돌려준다. 응답과 결과 파일에 `hardware_accessed: false`를 쓴다.
   - `judge`는 결과 파일만 읽는 stdlib 코드로 두고, 저장된 판정 필드를 쓴다.
3. `profiles/<id>.toml`을 만든다. `[interpreters]`, `[adapter.tasks.<task>]`에 (동사, 과제) → 인터프리터를 적고, 실행·import할 외부 파일을 `[adapter.allowlist]`에 모두 적는다. 어댑터는 목록 밖 요청을 거부해야 한다.
4. `[artifacts] delete_patterns`에 지워도 되는 대용량 부산물만 명시한다. 명시하지 않은 파일은 지워지지 않는다.
5. 새 과제 이름이나 배지 값이 필요하면 `schema.py`의 목록과 리포트 표기(`report_assets/app.js`)를 함께 늘린다.
6. `scenarios/<scenario_id>/scenario.json`을 3.2 형식으로 쓰고, 소스 SHA-256과 `origin`·`judgement_basis`·`assembly`를 채운다.
7. 가짜 어댑터 테스트(`tests/fake_adapter.py` 패턴)와, 가능하면 저장된 결과로 `judge`만 도는 CPU 테스트를 추가한다.

## 8. 한계

- **운동학·모델 좌표**: 관측 동결 재생의 TCP 거리와 고스트 렌더는 SO-101 MuJoCo 미러의 기구학으로 계산한다. 물리 스텝을 돌리지 않으므로 동역학 차이(지연·마찰·부하)는 다루지 않는다. 고스트 카메라는 3인칭 영상과 육안으로 맞춘 것이고 캘리브레이션 카메라가 아니다.
- **proxy 판정**: 컵 과제 판정은 왼팔 단독 강체 패드 접촉 시뮬 기준이다. 실물 핑거 파지 검증(`real_finray_grasp_verified`)은 없고, 관측은 시뮬레이터 정답 좌표이며 RGB 인식이 아니다.
- **정답 좌표 조건**: 조건 B의 복구는 시뮬레이터 정답 좌표로 제한된 접촉 복구다. 조건 B의 25/25는 실물 복구 성능을 뜻하지 않는다.
- **양팔 실물 기록 없음**: 양팔 LeRobot 데이터셋과 정책 체크포인트가 아직 없다. 실측 재생은 같은 기구인 SO-101 텔레옵 기록으로 하고, 양팔 로봇 프로젝트는 시뮬 실행 기록만 다룬다. 양팔 텔레옵 기록이 생기면 같은 `convert`로 추가한다.
- **운동학 모델 미재현**: 차동 구동 운동학 모델은 동역학에 의존하는 사고를 재현하지 못할 수 있다. 이런 경우 `not_reproducible_in_model`로 표기하고 원인을 단정하지 않는다.
- **결정성 범위**: 판정 일치율은 조건 A의 5점 × 3회에서 잰 값이다. 다른 조건·격자점의 비결정성은 따로 측정하지 않았다.
