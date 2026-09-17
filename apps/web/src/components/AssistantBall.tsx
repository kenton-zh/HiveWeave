/**
 * AssistantBall — 应用内悬浮助理球（用户 2026-09-07 澄清的正确形态：
 * 球是平台 UI 的一部分（DOM 悬浮组件，页面内任意拖动），不是 OS 级
 * 独立窗口；网页端与打包 EXE 共用同一份。
 *
 * 数据管道与 pywebview 桌面球（apps/desktop/ball）完全同源：
 *   GET  /api/ball/state            → 助理的 agentId/未读
 *   GET  /api/chat/history/{id}     → 消息流（轮询）
 *   POST /api/ball/unread/clear     → 展开即清未读
 *   POST /api/ball/chat             → 发送（source: "ball"）
 *
 * 交互：球态（56px + 未读徽章）⇄ 展开态（360×520 面板）；拖拽 = pointer
 * 位移 > 6px，松开未位移 = 展开/收起；位置记忆存 localStorage（应用内
 * 坐标，与 OS 球的 ball_position.json 无关）。
 *
 * 面板只承载助理对话（用户 2026-09-08 拍板「助理面板里只有助理」）——
 * /api/ball/state 里的项目 CEO 分支此处不消费，CEO 对话走网页端项目
 * 切换器；设计稿 assistant-and-feishu-design.md §10 已同步修订。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiKey } from "../api/shared";

const DRAG_THRESHOLD_PX = 6;
const POLL_COLLAPSED_MS = 2500;
const POLL_EXPANDED_MS = 1800;
const BALL_SIZE = 56;
const PANEL_W = 360;
const PANEL_H = 520;
const POS_KEY = "hw_assistant_ball_pos";

interface BallTarget {
  agentId: string;
  name: string;
  unread: number;
}

interface ChatMsg {
  id: string | number;
  role: string;
  content: string;
}

interface StateResp {
  assistant?: { agentId: string; name?: string; unread?: number };
  // /api/ball/state 仍会返回 projects[].ceo，但球面板按 09-08 拍板不消费
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

function clampPos(p: { x: number; y: number }) {
  return {
    x: clamp(p.x, 4, Math.max(4, window.innerWidth - BALL_SIZE - 4)),
    y: clamp(p.y, 4, Math.max(4, window.innerHeight - BALL_SIZE - 4)),
  };
}

function loadPos(): { x: number; y: number } {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p.x === "number" && typeof p.y === "number") {
        // 旧存储必须 clamp：换小屏/缩窗后超大坐标会让球落在视口外不可救
        return clampPos(p);
      }
    }
  } catch {
    /* ignore */
  }
  // 默认右下角（留 24px 边距）
  return {
    x: window.innerWidth - BALL_SIZE - 24,
    y: window.innerHeight - BALL_SIZE - 24,
  };
}

/** 应用内悬浮助理球。始终挂载于 App 根节点（覆盖在所有视图之上）。 */
export default function AssistantBall() {
  const [pos, setPos] = useState<{ x: number; y: number }>(loadPos);
  const [expanded, setExpanded] = useState(false);
  const [targets, setTargets] = useState<BallTarget[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const draggingRef = useRef(false);
  const listRef = useRef<HTMLDivElement>(null);
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;

  // 窗口缩放后把球拉回视口内（旧位置可能超出）
  useEffect(() => {
    const onResize = () => setPos((p) => clampPos(p));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const api = useCallback((path: string, opts?: RequestInit) => {
    const key = getApiKey() || "";
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(key ? { "x-api-key": key } : {}),
    };
    return fetch(path, { ...opts, headers }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`);
      return r.json();
    });
  }, []);

  const loadMessages = useCallback(
    (agentId: string | null) => {
      if (!agentId) return;
      api(`/api/chat/history/${encodeURIComponent(agentId)}?limit=60`)
        .then((data) => {
          const visible = (data.messages || []).filter(
            (m: Record<string, unknown>) =>
              (m.role === "user" || m.role === "assistant") &&
              !(m.isBackground || m.is_background) &&
              !(m.isContext || m.is_context),
          );
          setMessages(visible as ChatMsg[]);
        })
        .catch(() => {});
    },
    [api],
  );

  const clearUnread = useCallback(
    (agentId: string) => {
      api("/api/ball/unread/clear", {
        method: "POST",
        body: JSON.stringify({ agentId }),
      }).catch(() => {});
    },
    [api],
  );

  // 轮询：球态红点 + 展开态消息流
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const tick = () => {
      if (stopped) return;
      api("/api/ball/state")
        .then((data: StateResp) => {
          if (stopped) return;
          const t: BallTarget[] = [];
          if (data.assistant) {
            t.push({
              agentId: data.assistant.agentId,
              name: data.assistant.name || "助理",
              unread: data.assistant.unread || 0,
            });
          }
          setTargets(t);
          setActiveId((cur) => {
            if (cur && t.some((x) => x.agentId === cur)) return cur;
            return t.length ? t[0].agentId : null;
          });
        })
        .catch(() => {})
        .then(() => {
          if (stopped) return;
          if (activeIdRef.current && expanded) loadMessages(activeIdRef.current);
          timer = window.setTimeout(tick, expanded ? POLL_EXPANDED_MS : POLL_COLLAPSED_MS);
        });
    };
    tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [api, expanded, loadMessages]);

  // 展开时清当前目标未读
  useEffect(() => {
    if (expanded && activeId) clearUnread(activeId);
  }, [expanded, activeId, clearUnread]);

  // 消息流：仅当原本贴底时自动跟随（否则每 1.8s 把上翻读史的用户拽回底部）
  useEffect(() => {
    const box = listRef.current;
    if (!box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }, [messages]);

  // 助理目标消失/重现（如后端重启）时回落，并清空旧消息流
  useEffect(() => {
    setMessages([]);
  }, [activeId]);

  const savePos = (p: { x: number; y: number }) => {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(p));
    } catch {
      /* ignore */
    }
  };

  // 页面内拖拽（pointer events；位移>阈值=拖，松开未位移=展开/收起）
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    // 落在交互元素上不启动拖拽：setPointerCapture 会把后续 click 重定向到
    // 捕获元素，头部按钮（收起/标签）将永远收不到 click（实测踩坑）
    if ((e.target as HTMLElement).closest("button,input,textarea,a")) return;
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = { ...pos };
    const bound = expanded
      ? { w: PANEL_W, h: PANEL_H }
      : { w: BALL_SIZE, h: BALL_SIZE };
    let last = { ...orig };
    let moved = false;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);

    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!moved && dx * dx + dy * dy > DRAG_THRESHOLD_PX * DRAG_THRESHOLD_PX) {
        moved = true;
        draggingRef.current = true;
      }
      if (moved) {
        last = {
          x: clamp(orig.x + dx, 4, Math.max(4, window.innerWidth - bound.w - 4)),
          y: clamp(orig.y + dy, 4, Math.max(4, window.innerHeight - bound.h - 4)),
        };
        setPos(last);
      }
    };
    const onUp = () => {
      detach();
      if (moved) {
        savePos(last);
        // 让 click 不触发展开：下一次 click 抑制
        suppressClickRef.current = true;
        window.setTimeout(() => {
          suppressClickRef.current = false;
          draggingRef.current = false;
        }, 0);
      }
    };
    const onCancel = () => {
      detach();
      if (moved) savePos(last); // 系统取消（右键混按/触屏接管）也落盘
    };
    const detach = () => {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onCancel);
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onCancel);
  };

  const suppressClickRef = useRef(false);
  const onBallClick = () => {
    if (suppressClickRef.current || draggingRef.current) return;
    setExpanded(true);
  };

  const send = () => {
    const content = input.trim();
    if (!content || sending || !activeId) return;
    setSending(true);
    setSendError(null);
    api("/api/ball/chat", {
      method: "POST",
      body: JSON.stringify({ agentId: activeId, content, source: "ball" }),
    })
      .then(() => {
        setInput("");
        return loadMessages(activeId);
      })
      .catch((err: Error) => {
        // 独立错误条：不进 messages（轮询整体替换会把它 ≤1.8s 冲掉）
        setSendError(err.message || "未知错误");
      })
      .then(() => setSending(false));
  };

  const totalUnread = targets.reduce((s, t) => s + t.unread, 0);

  return (
    <>
      {/* 球态 */}
      {!expanded && (
        <div
          onPointerDown={onPointerDown}
          onClick={onBallClick}
          title="HiveWeave 助理（可拖动）"
          className="fixed z-50 flex items-center justify-center rounded-full shadow-gm cursor-grab active:cursor-grabbing select-none touch-none bg-gradient-to-br from-g-yellow-vivid to-g-yellow border-2 border-white/70 hover:scale-105 transition-transform"
          style={{ left: pos.x, top: pos.y, width: BALL_SIZE, height: BALL_SIZE }}
        >
          <span className="text-2xl pointer-events-none">🐝</span>
          {totalUnread > 0 && (
            <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-g-red-vivid text-white text-[11px] font-semibold flex items-center justify-center pointer-events-none">
              {totalUnread > 99 ? "99+" : totalUnread}
            </span>
          )}
        </div>
      )}

      {/* 展开态面板 */}
      {expanded && (
        <div
          className="fixed z-50 flex flex-col bg-white border border-g-border rounded-gm shadow-gm overflow-hidden"
          style={{
            left: clamp(pos.x, 4, Math.max(4, window.innerWidth - PANEL_W - 4)),
            top: clamp(pos.y, 4, Math.max(4, window.innerHeight - PANEL_H - 4)),
            width: PANEL_W,
            height: PANEL_H,
          }}
        >
          <div
            className="flex items-center gap-1 px-2 py-1.5 border-b border-g-border bg-g-bg-soft shrink-0 touch-none"
            onPointerDown={onPointerDown}
          >
            <button
              onClick={() => setExpanded(false)}
              title="收起"
              className="w-6 h-6 rounded-gm hover:bg-black/10 text-g-fg-3 text-sm leading-none"
            >
              −
            </button>
            <div className="flex items-center gap-1.5 flex-1 min-w-0 px-1">
              <span className="text-[12px] font-medium text-g-fg truncate">
                {targets[0]?.name || "助理"}
              </span>
              {(targets[0]?.unread || 0) > 0 && (
                <span className="w-1.5 h-1.5 rounded-full bg-g-red-vivid shrink-0" />
              )}
            </div>
          </div>

          <div ref={listRef} className="flex-1 overflow-y-auto p-3 space-y-2">
            {messages.length === 0 && (
              <div className="text-center text-g-fg-3 text-[12px] mt-8">暂无消息，说点什么吧</div>
            )}
            {messages.map((m) => (
              <div
                key={m.id}
                className={`max-w-[85%] px-2.5 py-1.5 rounded-gm text-[12px] whitespace-pre-wrap break-words ${
                  m.role === "user"
                    ? "ml-auto bg-g-blue text-white"
                    : "mr-auto bg-g-bg-soft border border-g-border text-g-fg"
                }`}
              >
                {m.content}
              </div>
            ))}
            {sendError && (
              <div className="mx-auto px-2.5 py-1 rounded-gm bg-g-red-bg text-g-red text-[12px] text-center">
                发送失败：{sendError}
              </div>
            )}
          </div>

          <div className="flex items-center gap-2 p-2 border-t border-g-border shrink-0">
            <input
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                if (sendError) setSendError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") send();
              }}
              placeholder="输入消息…"
              className="flex-1 min-w-0 px-2.5 py-1.5 text-[12px] rounded-gm border border-g-border focus:border-g-blue/60 bg-white text-g-fg"
            />
            <button
              onClick={send}
              disabled={sending || !activeId || !input.trim()}
              className="px-3 py-1.5 text-[12px] rounded-gm bg-g-blue text-white disabled:opacity-40 hover:opacity-90"
            >
              发送
            </button>
          </div>
        </div>
      )}
    </>
  );
}
