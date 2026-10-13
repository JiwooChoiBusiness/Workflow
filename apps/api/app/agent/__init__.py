# 에이전트 루프는 HTTP·DB를 모른다(설계 4.1, 13.2). app.api·app.auth·app.db·app.services를
# import하지 않고, 저장과 이벤트는 loop.LoopPorts로 받는다(tests/unit/test_layering.py가 확인한다).
