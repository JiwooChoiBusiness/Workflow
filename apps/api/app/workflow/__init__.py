# 워크플로우 엔진(2단계 설계 3장). app.api·app.auth·app.db·app.services를 import하지 않고 밖은
# ports.EnginePorts로만 본다(tests/unit/test_layering.py가 확인한다). app.agent.task_service가
# render를 import하므로 여기서는 하위 모듈을 미리 불러오지 않는다(순환 import).
