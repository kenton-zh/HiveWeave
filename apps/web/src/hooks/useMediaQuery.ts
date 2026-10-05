import { useEffect, useState } from "react";

/** 极简响应式断点（`window.matchMedia`）。仅用于布局级分叉，组件内细节优先走
 * 容器查询（index.css 的 .hw-cq / --hw-region-mode）。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}
