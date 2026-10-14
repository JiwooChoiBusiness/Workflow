// 접고 펼치는 JSON 보기. 두 단계까지는 펼쳐 두고 그 아래는 항목 수만 보인다.
export function JsonTree({
  name,
  value,
  depth = 0,
  openDepth = 2,
}: {
  name?: string;
  value: unknown;
  depth?: number;
  openDepth?: number;
}) {
  const label =
    name === undefined ? null : <span className="text-info">{JSON.stringify(name)}: </span>;
  if (value !== null && typeof value === "object") {
    const array = Array.isArray(value);
    const entries: [string, unknown][] = array
      ? value.map((item, index) => [String(index), item])
      : Object.entries(value as Record<string, unknown>);
    const [open, close] = array ? ["[", "]"] : ["{", "}"];
    if (entries.length === 0) {
      return (
        <div>
          {label}
          {open}
          {close}
        </div>
      );
    }
    return (
      <details open={depth < openDepth}>
        <summary className="cursor-pointer select-none hover:bg-hover/60">
          {label}
          {open}
          {/* 접혀 있을 때만 항목 수와 닫는 괄호를 한 줄로 보인다. */}
          <span className="text-ink-subtle [details[open]>summary>&]:hidden">
            {` ${entries.length} … ${close}`}
          </span>
        </summary>
        <div className="border-l border-line pl-3">
          {entries.map(([key, item]) => (
            <JsonTree
              key={key}
              name={array ? undefined : key}
              value={item}
              depth={depth + 1}
              openDepth={openDepth}
            />
          ))}
        </div>
        <div>{close}</div>
      </details>
    );
  }
  const tone =
    typeof value === "string"
      ? "text-ok"
      : value === null
        ? "text-ink-subtle"
        : typeof value === "number" || typeof value === "boolean"
          ? "text-warn"
          : "text-code-ink";
  return (
    <div className="break-all">
      {label}
      <span className={tone}>{JSON.stringify(value)}</span>
    </div>
  );
}
