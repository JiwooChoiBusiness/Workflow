# Workflow

Dify 같은 LLM 에이전트·워크플로우 플랫폼을 혼자 쓰려고 직접 만드는 프로젝트다. 내 PC의 Docker Desktop에서 돌리는 걸 기준으로 하고, 이 저장소는 코드 보존용이다.

아직 코드는 없고 문서만 있다.

- `docs/research/dify-analysis.md` : Dify를 뜯어본 기록
- `docs/decisions/` : 범위, 배포 방식 같은 결정 기록
- `docs/design/phase1-agent-sandbox.md` : 1단계(에이전트 샌드박스) 설계

진행 순서는 에이전트 샌드박스, 워크플로우 엔진, RAG 순이다.

`.claude/`에는 Claude Code 작업 설정이 들어 있다. 설계와 구현은 Claude와 같이 하되 결정과 커밋은 내가 한다.
