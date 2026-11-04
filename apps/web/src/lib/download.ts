// POST 응답이나 화면에서 만든 내용은 링크로 받을 수 없다. 받은 내용을 객체 URL로 내려받게 한다.
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  // 바로 해제하면 브라우저가 내려받기를 시작하기 전에 URL이 사라질 수 있다.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
