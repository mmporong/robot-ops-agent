# 양팔 로봇 프로젝트 어댑터 (`robot-ops-adapter/1`)

`profiles/bimanual.toml`이 가리키는 어댑터다. 코어(`src/robot_ops/replay`, stdlib)는 이 폴더를
(동사, 과제)별 인터프리터로 subprocess 호출하고, 응답 JSON 파일만 믿는다.

```text
<interp> adapters/bimanual/entry.py <convert|run|judge> --task <task> \
    --request req.json --response resp.json --workdir <dir>
```

응답은 항상 `{"protocol": "robot-ops-adapter/1", "status": "ok" | "unsupported" | "infra_error", ...}`이고
종료 코드는 0 / 2 / 3이다. 동사·과제 모듈은 지연 import한다.

## (동사, 과제) 표

| 동사 | 과제 | 인터프리터 | 모듈 | 하는 일 |
|---|---|---|---|---|
| convert | lerobot_episode | lerobot | `convert_lerobot.py` | LeRobot v3 parquet 에피소드 → `trajectory.jsonl`(q_cmd=action, q_obs=state, 5관절 rad, 그리퍼 별도) + 카메라 구간 H.264 MP4(`media/camera_third_person.mp4` "실측 3인칭 카메라", `media/camera_wrist.mp4` "실측 손목 카메라") |
| run | lerobot_episode | rlwalk | `run_mujoco_episode.py` | SO-101 MuJoCo 미러 FK로 `tcp_cmd_m`·`tcp_obs_m`을 채운 `trajectory_mujoco.jsonl`, `--ghost`면 고스트 MP4(`ghost.mp4`)와 합성 PNG(`ghost_composite.png`) |
| run | drive_kinematic | lerobot | `run_drive_kinematic.py` | 차동 구동 운동학 격자 셀 1개 → `result.json` + `trajectory.jsonl` |
| judge | cup_contact | core | `judge.py` | 저장된 `task_pass`/`rigid_proxy_lift_pass`로 판정, samples 재계산 항목을 checks에 첨부 |
| judge | drive_kinematic | core | `judge.py` | `final_alignment_bounded`(임계 `evaluations/replay/f1004_thresholds.json`) |
| run | cup_contact | leisaac | `run_cup_contact.py` | allowlist `tools/simulate_cup_contact.py`를 `--headless --output-dir <workdir>/isaac --spawn-x/y-offset-mm`(+ 조건 B `--recover`, 녹화 요청 시 `--record`)로 실행 → `isaac/result.json`, 녹화 시 10 fps H.264 `cup_contact.mp4` |

그 밖의 조합과 `convert|run|judge` 밖의 동사는 `unsupported`다.

## 지켜야 할 경계

- 팀 저장소 `~/bimanual-robot`에는 쓰지 않는다. 실행·import 가능한 팀 파일은 `entry.py`의
  `TEAM_SCRIPTS`·`TEAM_IMPORTS`(= profile `[adapter.allowlist]`)뿐이고, 요청에 그 밖의 경로가 오면 거부한다.
- `run_drive_kinematic.py`는 `git show <rev>:tools/mobile_service_control.py` 단일 파일 사본을
  workdir `snapshot/`에 두고 import한다. 적분식·상수·반전 계산은 M0 `drive_discriminate.py`에서 가져온다.
- `judge.py`는 stdlib만 쓴다. `python -S`로 돌려도 서드파티 모듈이 로드되지 않아야 한다
  (`tests/test_bimanual_judge_cpu.py`).
- 모든 호출에 `PYTHONDONTWRITEBYTECODE=1`(코어가 넣는다) + `sys.dont_write_bytecode = True`.
- 하드웨어·직렬 장치·네트워크에 접근하지 않는다. run 응답과 결과 파일에 `hardware_accessed: false`.

## 고스트 렌더 (run, lerobot_episode)

- 미러 = `~/so101_tools/sim` `SimMirror.set_pose_deg(deg, attach=False)`. 생성자가 부르는
  `arm_lib.load_mapping`(파일이 없으면 기본값을 씀)을 같은 파일을 읽기만 하는 함수로 바꿔 끼운다.
- 작업 물체(`piece`, `piece_cyl`, `dropbox`)는 장면 밖으로 치운다. 책상·차량 상판은 남긴다.
- 같은 `MjvCamera`로 명령 자세와 관측 자세를 따로 렌더하고, 명령 렌더의 팔 픽셀(세그멘테이션)만
  채도 높은 파랑(가중 0.75)으로 물들여 alpha 0.55로 관측 렌더 위에 합성하고, 명령 팔 윤곽 2 px를 진한 파랑으로 칠한다. 10 fps(궤적 fps), 640×480.
- 두 렌더의 카메라 외부·내부 파라미터를 프레임마다 비교해 `camera_matrices_equal`로 남기고,
  `mj_step` 호출 횟수(`physics_steps`)가 0이 아니면 실패한다.
- 카메라(방위 150°, 고도 -35°, 거리 0.75 m)는 teleop_bench 3인칭 영상과 육안 대조로 골랐다. 실측 장면과
  정확히 맞춘 캘리브레이션 카메라가 아니다.
- 캡션: "궤적 모델 = SO-101 MuJoCo 미러(양팔 로봇 왼팔과 같은 SO-101 기구)".

## Isaac 컵 격자 (run, cup_contact)

- 팀 스크립트 종료 코드 0 = 통과, 2 = 판정 실패(정상 종료). 그 밖이거나 `stop_reason`이
  `exception:*`·`initializing`·`window_closed`이면 `infra_error`.
- `isaac/scene.usda`(약 78 MB)와 `isaac/frames/*.png`는 코어가 manifest(SHA·크기) 기록 뒤
  profile `delete_patterns`로 지운다. 어댑터는 지우지 않는다.
- 시간 상한 180초(1회 실측 24.7초 × 2 < 180), GPU 과제라 run마다 `nvidia-smi` 원복(+200 MiB) 확인.
- 녹화는 본 격자와 따로 한다: `robot-ops regress ... --record-cells "A:0,-5;B:0,-5"`.

## 재현

```bash
cd ~/robot-ops-agent
uv run robot-ops replay --profile profiles/bimanual.toml \
    --source lerobot:~/so101_datasets/so101_teleop_bench --episode 0 --ghost
uv run python -m unittest tests.test_bimanual_judge_cpu -v
```

산출물은 `/data/$USER/robot-ops/bimanual/runs/<run_id>/`에 남는다(`frames/*.png`는 manifest 기록 후 삭제).
