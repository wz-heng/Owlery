export interface PrincipleReadingItem {
  href: string;
  index: string;
  kicker: string;
  title: string;
  question: string;
}

export function PrincipleReadingGuide({
  thesis,
  boundary,
  codeRoots,
  items,
}: {
  thesis: string;
  boundary: string;
  codeRoots: string[];
  items: PrincipleReadingItem[];
}) {
  return (
    <aside className="principle-reading-guide" aria-label="原理阅读路线">
      <div className="reading-guide-intro">
        <span className="eyebrow">START HERE / 先看结论</span>
        <h3>{thesis}</h3>
        <p>{boundary}</p>
        <div className="reading-guide-method">
          <span>阅读方法</span>
          <ol>
            <li>先抓住每章要回答的问题</li>
            <li>再看机制、数据契约和状态变化</li>
            <li>最后核对失败出口、取舍与代码落点</li>
          </ol>
        </div>
      </div>
      <nav className="reading-guide-nav" aria-label="原理章节索引">
        {items.map((item) => (
          <a href={item.href} key={item.href}>
            <span>{item.index}</span>
            <div>
              <small>{item.kicker}</small>
              <strong>{item.title}</strong>
              <p>{item.question}</p>
            </div>
          </a>
        ))}
      </nav>
      <div className="reading-guide-code">
        <span>CODE MAP / 先从这些实现入口核对</span>
        <div>{codeRoots.map((path) => <code key={path}>{path}</code>)}</div>
      </div>
    </aside>
  );
}
