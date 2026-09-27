"""사고 재생·회귀 평가 코어 (stdlib 전용).

로봇·팀 코드·numpy·Isaac은 이 패키지에서 import하지 않는다. 그런 작업은
(동사, 과제)별 인터프리터로 분리된 어댑터 subprocess가 맡는다.
"""

ADAPTER_PROTOCOL = "robot-ops-adapter/1"
VERBS = ("convert", "run", "judge")

__all__ = ["ADAPTER_PROTOCOL", "VERBS"]
