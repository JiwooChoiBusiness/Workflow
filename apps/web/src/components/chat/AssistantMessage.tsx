import { memo } from "react";
import Markdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { rehypeHighlight } from "@/lib/highlight";

type PluggableList = NonNullable<Options["remarkPlugins"]>;

const remarkPlugins: PluggableList = [remarkGfm];
const rehypePlugins: PluggableList = [rehypeHighlight];

// 원시 HTML은 react-markdown 기본값대로 그리지 않는다(설계 S12). 이미지는 링크로만 보여 준다.
// 모델이 쓴 ![](https://.../?q=...)가 저절로 요청되면 대화 내용이 밖으로 새어 나갈 수 있어서다.
const components: Components = {
  a: ({ node: _node, children, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  img: ({ src, alt }) => {
    const href = typeof src === "string" ? src : undefined;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {alt || href}
      </a>
    );
  },
};

export const AssistantMessage = memo(function AssistantMessage({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  return (
    <div className="wf-markdown text-sm leading-relaxed text-ink">
      {/* 스트리밍 중에는 매 프레임 다시 그리므로 코드 강조를 끝난 뒤로 미룬다. */}
      <Markdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={streaming ? undefined : rehypePlugins}
        components={components}
      >
        {text}
      </Markdown>
      {streaming ? (
        <span
          className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse rounded-[1px] bg-ink-subtle align-middle"
          aria-hidden
        />
      ) : null}
    </div>
  );
});
