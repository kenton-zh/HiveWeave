/**
 * 窗口层 —— 把 windowStore 里的窗口列表渲染成一叠 GameWindow。
 *
 * 位置：办公室主界面之上（`docs/前端设计规格.md` §2.3 的「窗口层」）。
 * 业务数据一律从主 store 读，窗口层自身不持有业务状态（§2.1 分层原则）。
 */
import { useAppStore } from "../../store";
import GameWindow from "./GameWindow";
import { renderGamePanel } from "./registry";
import { useGameWindowStore } from "./store";

export default function GameWindowLayer() {
  const windows = useGameWindowStore((s) => s.windows);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);

  return (
    <>
      {windows.map((w) => (
        <GameWindow key={w.id} win={w}>
          {renderGamePanel(w, { selectedProjectId })}
        </GameWindow>
      ))}
    </>
  );
}
