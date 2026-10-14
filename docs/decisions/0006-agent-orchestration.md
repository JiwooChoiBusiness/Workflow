# ADR-0006: 오케스트레이션은 "에이전트를 도구로" + 자식 런으로 만든다

- 상태: 제안 v2 (architect, 2026-10-14, design-reviewer 검토 반영). 사용자가 "설계대로 시행"을 지시했으므로 채택으로 간주하고 M12 뒤 O1부터 진행한다.
- 날짜: 2026-10-14
- 배경: 사용자는 캔버스의 고정 파이프라인(2단계 `agent` 노드)과 별개로, 지휘자 에이전트가 상황에 따라 다른 에이전트를 도구처럼 불러 일을 나눠 주는 방식(Claude Code의 서브에이전트)을 원한다. 1단계는 끝났고 2단계는 M11 구현 중이다. ADR-0001~0005는 유지한다. 상세는 `docs/design/agent-orchestration.md` v2.

## 결정

### D1. 별도 엔진이 아니라 내장 도구 `call_agent` + 1단계 과업 런
- 지휘자는 보통 에이전트이고, 빌더 '도구와 스킬' 탭에서 다른 에이전트를 고르면 `call_agent(agent, task, files?, output_schema?)` 도구가 붙는다. 도구는 하나이고 `agent`는 slug `enum`이다. 에이전트별 도구(`agent:<slug>`)는 OpenAI 호환 함수 이름 규칙(`[A-Za-z0-9_-]{1,64}`)에 걸려 버렸다.
- 호출은 1단계 `AgentTaskService`로 자식 과업 런을 만든다(instruction=task, 결과 형식은 항목 설정: 기본 글+파일, 선택 JSON 스키마, 자식에는 `ask_user` 없음). 부모 런은 1단계 승인·질문과 같은 멈춤·재개 기계(`waiting_input` + `pending_interaction.kind=agent_call`, `resume_payload.child`)로 슬롯을 내놓고, 자식이 끝나면 같은 스텝에서 결과를 tool_result로 받는다. 새 런 상태·새 테이블은 없고 `run.parent_run_id`(외래 키 없음)와 `agent_version.sub_agents` 열만 더한다(마이그레이션 0004. M17은 0005·0006).
- 자식 종료 훅은 `claim_parent`(자식 종료를 쓰는 트랜잭션 안에서 부모를 `queued + resume_payload`로)와 `launch_parent`(트랜잭션 밖에서 발행·투입) 두 단계다. 쓰기 락이 재진입하지 않고 트랜잭션 안 발행은 writer와 교착하기 때문이다. 종료 중이면 투입을 건너뛰고 복구가 잇는다. 트리 전체 호출 수는 뿌리의 `max_child_calls`로 묶는다.
- 동시 런 1에서 교착이 없다. 런은 `running`일 때만 슬롯을 쥐고, 기다리는 쪽(부모)은 슬롯을 쥐지 않으므로 대기 그래프에 사이클이 없다.

### D2. 이벤트·API 의미는 지킨다
- `run.waiting_input`·`run.completed(waiting_input)`은 사람 입력을 기다릴 때만 쓴다. 하위 호출은 `child_run.started`(자식 런·세션 id 포함)·`child_run.completed` 두 이벤트와 `tool_call.completed.child_run_id`로 표현하고, 부모 스트림은 자식이 도는 동안 닫지 않는다. API의 런 상태는 `running` + `child`로 보인다. 서비스·웹앱 API의 엔드포인트는 추가·변경이 없고 자식 런·세션의 범위는 부모 사슬로 판단한다.
- 열린 런(`queued`·`running`·하위 호출 대기)의 세션 스트림은 `EventBus.open_channel`로 항상 live 구독이다. 재기동 뒤 채널이 없을 때 웹이 재시도 끝에 연결 실패로 보이던 문제를 없앤다. 자식의 승인은 자식 세션 스트림을 부모 화면의 하위 에이전트 카드가 1단계 `ChatSessionController`로 구독해 보여 주고, 답은 자식 런으로 간다. 세션 자동 승인은 부모·자식 양방향으로 전파된다.

### D3. 2단계 agent 노드와 기반을 공유한다
- 자식 런 생성·대기·재개·승인 전파·파일 전달·취소·복구·만료 역조회는 `app/services/child_runs.py`(DB)와 `app/agent/child_runs.py`(순수, 루프는 `ports.start_child`로만 접근)에 두고, 2단계 M14 agent 노드가 같은 모듈을 쓴다. 결과 형식(글·파일·JSON) 설정 구조도 노드와 같다.
- 마일스톤은 O1(백엔드)·O2(화면)이고 순서는 M11 → M12∥M15 → O1∥M13 → O2∥M14 → M16 → …이다. O1이 M12와 같은 파일(`event_bus.py` 포함)을 고치므로 M12 뒤에 둔다.

## 기각한 대안
- 워크플로우 전용 오케스트레이터 노드만 두기: 캔버스 없이 대화에서 쓰는 지휘자가 없고, 모델이 상황에 따라 호출을 정하는 동적 분배가 그래프 정의와 맞지 않는다. 지휘자를 노드로 부르는 것은 이 설계에서도 그대로 된다.
- 새 런 상태 `waiting_child`: CHECK 제약 변경(테이블 재생성)과 `waiting_input`을 보는 여러 곳(복구, 만료, `active_for_session`, 화면 상태, 로그 필터, 통계)의 수정이 필요한데 얻는 것은 이름뿐이다.
- 자식 이벤트를 부모 스트림에 복제: 링버퍼가 두 배가 되고 1단계 이벤트의 뜻이 섞인다(2단계 3.8과 같은 결론).
- 화면 쪽 "자식 완료 시 부모 재접속" 규칙(v1): 훅 발행이 채널을 만들기 전에는 `live=False`라 경합이 남는다. 서버 쪽 `open_channel`로 대체.

## 영향
- 1단계 코드 접점은 설계 10장 O1 목록. 1단계 문서는 고치지 않는다. 2단계 문서는 10장 순서·표·M17 마이그레이션 번호와 12장 접점 목록만 맞춘다. `event_bus.py`는 M12 변경 위에서 고친다.
- 새 설정 `ORCHESTRATION_MAX_DEPTH`(기본 2), `RunLimits.max_child_calls`(기본 20). 신규 의존성 없음. `PendingInteraction.kind` 확장과 실행기 변경은 같은 배포에 들어간다.
