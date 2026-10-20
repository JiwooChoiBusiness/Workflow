#!/usr/bin/env bash
# 떠 있는 서버의 서비스 API(/api/v1)를 curl로 훑는다(설계 6.4, 수용 조건 A9·A14, 2단계 A7).
#   bash scripts/smoke-api.sh <API 키>        (또는 API_KEY 환경변수)
# 키는 발행한 에이전트·워크플로우·챗플로우의 것이어야 하고 /info의 종류에 따라 훑는 경로가 다르다.
# 실제 모델을 부르므로 Gemini 호출이 몇 번 나간다.
# 에이전트: chat-messages·대화·중지·과업. 승인 모드가 all_shell이면 셸 실행 승인을 대신 보낸다.
# 워크플로우: workflows/run을 streaming·blocking으로 한 번씩, 런 조회·로그. start 입력은
#   SMOKE_INPUTS에 JSON으로 준다(기본 {}).
# 챗플로우: chat-messages 3턴(같은 대화, 가운데 턴은 streaming)과 대화·메시지 목록.
# 워크플로우·챗플로우는 런마다 토큰과 걸린 시간을 출력한다.
# SMOKE_RATE_LIMIT=1이면 마지막에 같은 키로 /info를 61번까지 불러 429를 확인한다(1분 동안 키가 막힌다).
set -uo pipefail

cd "$(dirname "$0")/.."

# Git Bash(Windows)가 /로 시작하는 인자를 Windows 경로로 바꾸지 않게 한다.
export MSYS_NO_PATHCONV=1

key="${1:-${API_KEY:-}}"
if [ -z "$key" ]; then
  echo "사용법: bash scripts/smoke-api.sh <API 키>" >&2
  exit 2
fi
port="${SERVER_PORT:-$(sed -n 's/^SERVER_PORT=//p' .env 2>/dev/null | tail -n 1 | tr -d '\r[:space:]')}"
base="${BASE_URL:-http://127.0.0.1:${port:-8500}}/api/v1"
user="smoke-$(date +%s)"
work="$(mktemp -d)"
# Windows의 curl.exe는 MSYS 경로(/tmp/...)를 모른다. C:/... 꼴로 바꿔 쓴다.
if command -v cygpath >/dev/null 2>&1; then
  work="$(cygpath -m "$work")"
fi
trap 'rm -rf "$work"' EXIT

failed=0

report() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = 1 ]; then
    echo "OK    $name"
  else
    echo "FAIL  $name: $(head -c 400 <<<"$detail")"
    failed=1
  fi
}

# api <메서드> <경로> [JSON 파일] : 본문은 $work/body, 헤더는 $work/headers, 상태 코드를 출력한다.
api() {
  local method="$1" path="$2" payload="${3:-}"
  local args=(-s -N -X "$method" -D "$work/headers" -o "$work/body" -w '%{http_code}'
    -H "Authorization: Bearer $key" --max-time 600)
  if [ -n "$payload" ]; then
    args+=(-H 'Content-Type: application/json' --data-binary "@$payload")
  fi
  curl "${args[@]}" "$base$path"
}

json() {
  # 한글이 든 본문은 인자로 넘기면 코드 페이지를 거치며 깨질 수 있어 파일로 넘긴다.
  local file="$work/$1.json"
  printf '%s' "$2" >"$file"
  echo "$file"
}

header() {
  sed -n "s/^$1: *//Ip" "$work/headers" | tr -d '\r' | tail -n 1
}

completed_status() {
  # SSE 본문에서 run.completed의 status.
  grep '^data: ' "$1" | grep '"type":"run.completed"' | tail -n 1 |
    sed -n 's/.*"data":{"status":"\([a-z_]*\)".*/\1/p'
}

workflow_status() {
  # SSE 본문에서 workflow.completed의 status.
  grep '^data: ' "$1" | grep '"type":"workflow.completed"' | tail -n 1 |
    sed -n 's/.*"data":{"status":"\([a-z_]*\)".*/\1/p'
}

usage_of() {
  # blocking 응답의 usage(입력·출력 토큰)를 "입력 N, 출력 M"으로.
  sed -n 's/.*"usage":{"input_tokens":\([0-9]*\),"output_tokens":\([0-9]*\).*/입력 \1, 출력 \2/p' "$1"
}

smoke_workflow() {
  local inputs="${SMOKE_INPUTS:-}" payload code run_id status started elapsed
  [ -n "$inputs" ] || inputs='{}'
  payload="$(json wf-stream '{"inputs":'"$inputs"',"user":"'"$user"'","response_mode":"streaming"}')"
  started=$(date +%s%3N)
  code="$(api POST /workflows/run "$payload")"
  elapsed=$(($(date +%s%3N) - started))
  run_id="$(header x-run-id)"
  status="$(workflow_status "$work/body")"
  report "workflows/run streaming (run $run_id, ${elapsed}ms)" \
    "$([ "$code" = 200 ] && [ "$status" = succeeded ] && echo 1 || echo 0)" \
    "$code status=$status $(tail -c 400 "$work/body")"
  payload="$(json wf-block '{"inputs":'"$inputs"',"user":"'"$user"'","response_mode":"blocking"}')"
  code="$(api POST /workflows/run "$payload")"
  run_id="$(sed -n 's/.*"workflow_run_id":"\([^"]*\)".*/\1/p' "$work/body")"
  elapsed="$(sed -n 's/.*"elapsed_ms":\([0-9]*\).*/\1/p' "$work/body")"
  if [ "$code" = 200 ] && grep -q '"status":"succeeded"' "$work/body"; then
    report "workflows/run blocking (토큰 $(usage_of "$work/body"), ${elapsed:-?}ms)" 1
  else
    report "workflows/run blocking" 0 "$code $(cat "$work/body")"
  fi
  code="$(api GET "/workflows/run/$run_id?user=$user")"
  report "workflows/run/{run_id}" "$([ "$code" = 200 ] && grep -q '"status":"succeeded"' "$work/body" && echo 1 || echo 0)" \
    "$code $(cat "$work/body")"
  code="$(api GET "/workflows/logs?user=$user")"
  report "workflows/logs" "$([ "$code" = 200 ] && grep -q "\"workflow_run_id\":\"$run_id\"" "$work/body" && echo 1 || echo 0)" \
    "$code $(head -c 400 "$work/body")"
}

smoke_chatflow() {
  local payload code conversation="" status started elapsed turn mode
  for turn in 1 2 3; do
    mode=blocking
    [ "$turn" = 2 ] && mode=streaming
    if [ -n "$conversation" ]; then
      payload="$(json "chat$turn" '{"query":"'"$turn"'번째 질문이다. 앞에서 한 말을 한 문장으로 이어서 답한다.","user":"'"$user"'","conversation_id":"'"$conversation"'","response_mode":"'"$mode"'"}')"
    else
      payload="$(json "chat$turn" '{"query":"1번째 질문이다. 좋아하는 색을 하나 골라 한 문장으로 답한다.","user":"'"$user"'","response_mode":"'"$mode"'"}')"
    fi
    started=$(date +%s%3N)
    code="$(api POST /chat-messages "$payload")"
    elapsed=$(($(date +%s%3N) - started))
    if [ "$mode" = streaming ]; then
      status="$(workflow_status "$work/body")"
      if [ "$code" = 200 ] && [ "$status" = succeeded ] && grep -q '^event: answer.delta' "$work/body"; then
        report "chat-messages ${turn}턴 streaming(answer.delta, ${elapsed}ms)" 1
      else
        report "chat-messages ${turn}턴 streaming" 0 "$code status=$status $(tail -c 400 "$work/body")"
      fi
      continue
    fi
    [ -n "$conversation" ] || conversation="$(sed -n 's/.*"conversation_id":"\([^"]*\)".*/\1/p' "$work/body")"
    if [ "$code" = 200 ] && grep -q '"status":"succeeded"' "$work/body" && ! grep -q '"answer":""' "$work/body"; then
      report "chat-messages ${turn}턴 blocking (토큰 $(usage_of "$work/body"), ${elapsed}ms)" 1
    else
      report "chat-messages ${turn}턴 blocking" 0 "$code $(cat "$work/body")"
    fi
  done
  code="$(api GET "/conversations?user=$user")"
  report "conversations" "$([ "$code" = 200 ] && grep -q "\"id\":\"$conversation\"" "$work/body" && echo 1 || echo 0)" \
    "$code $(cat "$work/body")"
  code="$(api GET "/messages?conversation_id=$conversation&user=$user")"
  report "messages (3턴 = 6개)" \
    "$([ "$code" = 200 ] && [ "$(grep -o '"role":"assistant"' "$work/body" | wc -l)" = 3 ] && echo 1 || echo 0)" \
    "$code $(head -c 400 "$work/body")"
}

check_rate_limit() {
  # 키당 분당 60번
  [ "${SMOKE_RATE_LIMIT:-0}" = 1 ] || return 0
  local limited=0 i code retry
  for i in $(seq 1 61); do
    code="$(api GET /info)"
    if [ "$code" = 429 ]; then
      limited=$i
      break
    fi
  done
  retry="$(header retry-after)"
  report "레이트 리밋 429 (${limited}번째, Retry-After ${retry:-없음})" \
    "$([ "$limited" -gt 0 ] && [ -n "$retry" ] && echo 1 || echo 0)" "61번 불러도 429가 없다"
}

finish() {
  if [ "$failed" = 0 ]; then
    echo "모두 통과"
  fi
  exit "$failed"
}

approve_stream() {
  # 승인 대기면 승인을 보내고 재개된 런의 SSE를 $work/body로 받는다.
  local run_id="$1" payload
  payload="$(json approve "{\"decision\":\"approve\",\"response_mode\":\"streaming\",\"user\":\"$user\"}")"
  api POST "/chat-messages/$run_id/input" "$payload" >/dev/null
}

code="$(api GET /info)"
if [ "$code" = 200 ] && grep -q '"published":true' "$work/body"; then
  report "/info (발행된 대상)" 1
else
  report "/info (발행된 대상)" 0 "$code $(cat "$work/body")"
  echo "키가 맞는지, 에이전트·워크플로우를 발행했는지 확인한다."
  exit 1
fi
kind="$(sed -n 's/.*"kind":"\([a-z]*\)".*/\1/p' "$work/body")"
if [ "$kind" = workflow ] || [ "$kind" = chatflow ]; then
  echo "      종류: $kind"
  "smoke_$kind"
  check_rate_limit
  finish
fi
approval="$(sed -n 's/.*"approval_mode":"\([a-z_]*\)".*/\1/p' "$work/body")"
echo "      승인 모드: ${approval:-?}"

# 1. chat-messages streaming → (승인) → run.completed
payload="$(json chat '{"query":"셸에서 Get-ChildItem을 한 번 실행하고 작업공간에 무엇이 있는지 한 줄로 알려줘.","user":"'"$user"'","response_mode":"streaming"}')"
code="$(api POST /chat-messages "$payload")"
run_id="$(header x-run-id)"
conversation="$(header x-session-id)"
cp "$work/body" "$work/chat.sse"
if [ "$code" = 200 ] && [ -n "$run_id" ] && grep -q '^event: run.queued' "$work/chat.sse"; then
  report "chat-messages streaming (run $run_id)" 1
else
  report "chat-messages streaming" 0 "$code $(cat "$work/body")"
fi
status="$(completed_status "$work/chat.sse")"
if [ "$status" = waiting_input ]; then
  if grep -q '"kind":"approval"' "$work/chat.sse"; then
    approve_stream "$run_id"
    status="$(completed_status "$work/body")"
    report "승인 응답 후 재개(input approve)" "$([ "$status" = succeeded ] && echo 1 || echo 0)" \
      "status=$status $(tail -c 400 "$work/body")"
  else
    report "승인 응답" 0 "질문(ask_user)을 기다린다: $(grep -m1 'run.waiting_input' "$work/chat.sse")"
  fi
else
  report "런 완료(승인 없이)" "$([ "$status" = succeeded ] && echo 1 || echo 0)" \
    "status=$status $(tail -c 400 "$work/chat.sse")"
fi
grep -q '^event: message.delta' "$work/chat.sse" "$work/body" 2>/dev/null &&
  report "토큰 스트리밍(message.delta)" 1 || report "토큰 스트리밍(message.delta)" 0 "message.delta가 없다"

# 2. conversations, messages
code="$(api GET "/conversations?user=$user")"
report "conversations" "$([ "$code" = 200 ] && grep -q "\"id\":\"$conversation\"" "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"
code="$(api GET "/messages?conversation_id=$conversation&user=$user")"
report "messages" "$([ "$code" = 200 ] && grep -q '"role":"assistant"' "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"
code="$(api GET "/runs/$run_id?user=$user")"
report "runs/{run_id}" "$([ "$code" = 200 ] && grep -q '"status":"succeeded"' "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"

# 3. stop: 같은 대화에 오래 걸리는 요청을 보내 놓고 바로 멈춘다.
payload="$(json long '{"query":"PowerShell에서 Start-Sleep -Seconds 20을 실행한 뒤 끝났다고 알려줘.","user":"'"$user"'","conversation_id":"'"$conversation"'","response_mode":"streaming"}')"
curl -s -N -D "$work/long.headers" -o "$work/long.sse" --max-time 120 \
  -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
  --data-binary "@$payload" "$base/chat-messages" &
stream_pid=$!
long_run=""
for _ in $(seq 1 100); do
  long_run="$(sed -n 's/^x-run-id: *//Ip' "$work/long.headers" 2>/dev/null | tr -d '\r')"
  [ -n "$long_run" ] && break
  sleep 0.1
done
payload="$(json stop "{\"user\":\"$user\"}")"
code="$(api POST "/chat-messages/$long_run/stop" "$payload")"
report "chat-messages stop" "$([ "$code" = 200 ] && grep -q '"result":"success"' "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"
wait "$stream_pid"
# 승인을 기다리다 멈춘 런은 스트림이 먼저 닫히므로 런 상태로 확인한다.
code="$(api GET "/runs/$long_run?user=$user")"
report "중지된 런은 cancelled로 끝난다" \
  "$([ "$code" = 200 ] && grep -q '"status":"cancelled"' "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"

# 4. agent-tasks: 출력 스키마에 맞는 JSON 결과
schema='{"type":"object","properties":{"sum":{"type":"integer"}},"required":["sum"],"additionalProperties":false}'
payload="$(json task '{"user":"'"$user"'","inputs":{"a":2,"b":3},"instruction":"{{a}}와 {{b}}를 더한 값을 sum에 담아 submit_result로 제출한다. 도구로 계산하지 말고 바로 제출한다.","output_schema":'"$schema"',"response_mode":"blocking"}')"
code="$(api POST /agent-tasks "$payload")"
task_run="$(header x-run-id)"
if grep -q '"status":"waiting_input"' "$work/body"; then
  payload="$(json task-approve "{\"decision\":\"approve\",\"response_mode\":\"blocking\",\"user\":\"$user\"}")"
  api POST "/chat-messages/$task_run/input" "$payload" >/dev/null
  code="$(api GET "/agent-tasks/$task_run?user=$user")"
fi
if [ "$code" = 200 ] && grep -q '"status":"succeeded"' "$work/body" && grep -q '"result":{"sum":5}' "$work/body"; then
  report "agent-tasks (결과가 출력 스키마에 맞다: {\"sum\":5})" 1
else
  report "agent-tasks (출력 스키마)" 0 "$code $(cat "$work/body")"
fi
code="$(api GET "/agent-tasks/$task_run?user=$user")"
report "agent-tasks/{run_id}" "$([ "$code" = 200 ] && grep -q '"result":{"sum":5}' "$work/body" && echo 1 || echo 0)" \
  "$code $(cat "$work/body")"

# 5. (선택) 레이트 리밋
check_rate_limit
finish
