# event-schema

런 이벤트의 JSON Schema를 두는 곳이다. 원본은 `apps/api/app/schemas/events.py`의 Pydantic 모델이고, `run-events.schema.json`은 거기서 만든 산출물이다. 손으로 고치지 않는다.

```
uv run python scripts/gen-event-schema.py          # 다시 만든다
uv run python scripts/gen-event-schema.py --check  # 모델과 같은지 확인한다(테스트도 같은 확인을 한다)
```

## 이벤트 한 개의 모양

SSE 한 건은 `id: <seq>`, `event: <type>`, `data: <JSON 한 줄>`이다. `data`의 JSON은 모든 이벤트가 같은 겉모양을 갖는다.

```json
{"run_id": "...", "session_id": "...", "seq": 12, "ts": "2026-10-13T05:01:00.123Z", "type": "tool_call.completed", "data": {...}}
```

`seq`는 런 안에서 1부터 늘어난다. 승인·질문으로 멈췄다가 이어서 실행해도 같은 런이면 seq가 이어진다. `type`별 `data`는 스키마의 `oneOf`에 있다. `ping`만 런에 속하지 않아 `seq`가 없다.

## 받는 쪽이 알아 둘 규칙

- 런은 언제나 `run.completed`로 끝나고, 서버는 그 뒤에 SSE를 닫는다. 승인·질문을 기다릴 때도 `run.completed(status=waiting_input)`가 온다.
- 실패하면 `run.error` 바로 뒤에 `run.completed(status=failed)`가 온다. `run.completed.error`에도 같은 내용이 있다. 응답 대기 시간이 지나 취소된 런은 `run.error` 없이 `run.completed(status=cancelled, error.code=input_timeout)`이다.
- `x-transient-types`의 이벤트(`message.delta`, `thinking.delta`, `tool_call.input_delta`, `tool_call.output_delta`, `ping`)는 저장하지 않는다. 링버퍼가 버려진 뒤 다시 붙으면 오지 않으므로 화면은 `message.completed`, `tool_call.completed`의 전체 내용으로 다시 그린다.
- 델타의 글은 4KB 단위로 나뉘어 올 수 있다.
- `run.retrying`이 오면 그 스텝에서 진행 중이던 메시지(그때까지의 `message.delta`)는 버린다. 다시 시도한 출력은 새 `message_id`로 온다.
- 필드는 같은 버전 안에서 늘어날 수 있다. 모르는 필드는 무시한다. 필드를 지우거나 뜻을 바꾸면 `x-schema-version`이 올라간다.
