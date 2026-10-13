// 빌더 미리보기의 현재 세션 id. 새로 고침해도 같은 세션을 다시 열려고 에이전트별로 브라우저에 둔다.
// 저장소를 쓸 수 없으면(사생활 보호 모드 등) 새로 고침 때 새 대화로 시작할 뿐이다.
const key = (agentId: string) => `wf.preview-session.${agentId}`;

export function loadPreviewSessionId(agentId: string): string | null {
  try {
    return window.localStorage.getItem(key(agentId));
  } catch {
    return null;
  }
}

export function savePreviewSessionId(agentId: string, sessionId: string | null): void {
  try {
    if (sessionId) window.localStorage.setItem(key(agentId), sessionId);
    else window.localStorage.removeItem(key(agentId));
  } catch {
    // 저장하지 못해도 이번 화면에서는 계속 쓴다.
  }
}
