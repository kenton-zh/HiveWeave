/**
 * 窗口层 —— 把 windowStore 里的窗口列表渲染成一叠 GameWindow。
 *
 * 位置：办公室主界面之上（`docs/前端设计规格.md` §2.3 的「窗口层」）。
 * 业务数据一律从主 store 读，窗口层自身不持有业务状态（§2.1 分层原则）。
 *
 * FE-16：本层还承载窗口管理浮动控件（已打开面板菜单 / 一键归位，
 * 见 WindowMenu.tsx —— §8.5 规则 3/4；挂 OfficeWorkspace HUD 归另一工作流，
 * 此处为自包含挂载点，需要挪动时整个组件原样搬走即可）。
 *
 * FE-18（§16.3 高频状态更新）：`GameWindowSlot` 用 memo 隔离 —— 拖拽窗口 A 时
 * windows 数组高频更新，未 memo 时所有窗口的 portal 内容（ChatPanel 等重组件）
 * 会跟着每次 mousemove 重渲染；memo 后只有被拖的窗（win 引用变化）重渲染。
 */
import { memo } from "react";
import { useAppStore } from "../../store";
import GameWindow from "./GameWindow";
import { renderGamePanel } from "./registry";
import { useGameWindowStore, type GameWindowState } from "./store";
import WindowMenuControl from "./WindowMenu";

interface SlotProps {
  win: GameWindowState;
  selectedProjectId: string | null;
}

/** 单窗渲染槽：memo 依赖 = win 引用 + selectedProjectId（浅比较即够 ——
 *  store 的 setGeometry 只替换目标窗对象，其余窗引用稳定） */
const GameWindowSlot = memo(function GameWindowSlot({ win, selectedProjectId }: SlotProps) {
  return (
    <GameWindow win={win}>{renderGamePanel(win, { selectedProjectId })}</GameWindow>
  );
});

export default function GameWindowLayer() {
  const windows = useGameWindowStore((s) => s.windows);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);

  return (
    <>
      {windows.map((w) => (
        <GameWindowSlot key={w.id} win={w} selectedProjectId={selectedProjectId} />
      ))}
      <WindowMenuControl />
    </>
  );
}
