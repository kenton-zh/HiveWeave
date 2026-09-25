/**
 * DeliveryCard — 任务详情的固定交付摘要（FE-15，方案 §9.5，验收 T-24）。
 *
 * 安全红线（§9.5 + T-24）：
 *  - 文件路径**不是下载链接**：路径只展示 + 复制，绝不渲染成
 *    <a>/假下载按钮（本组件 0 个 href，测试锁死）；
 *  - 「Agent 说完成」（claim）与「产物已验证」（verifications）分列呈现；
 *  - 只有真实记录的产物才有复制操作；无产物时只给说明，不做无效按钮；
 *  - 无预览能力时替代动作 = 复制路径，并明说平台未提供下载/预览。
 */

import { useEffect, useRef, useState } from "react";
import type { TaskTimelineResponse } from "./types";
import { copyText, extractDelivery } from "./delivery";
import type { DeliverySummary } from "./delivery";
import { formatDateTime } from "../../utils/format";

/** 单个复制按钮：成功短暂打勾，失败原样可重试（无假承诺）。 */
type CopyState = "idle" | "ok" | "fail";

function copyBtnCls(state: CopyState): string {
  if (state === "ok") return "border-g-green text-g-green";
  if (state === "fail") return "border-g-red text-g-red";
  return "border-g-border text-g-fg-3 hover:bg-g-bg-muted hover:text-g-fg-2";
}

function copyBtnLabel(state: CopyState, label: string): string {
  if (state === "ok") return "已复制 ✓";
  if (state === "fail") return "复制失败";
  return label;
}

function CopyButton({
  text,
  label,
  testId,
}: {
  text: string;
  label: string;
  testId?: string;
}) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  const onCopy = async () => {
    const ok = await copyText(text);
    setState(ok ? "ok" : "fail");
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1500);
  };
  return (
    <button
      onClick={() => void onCopy()}
      data-testid={testId}
      title={`复制${label}（仅复制，不跳转）`}
      className={`shrink-0 px-1.5 py-px rounded-gm border text-[10px] transition-colors ${copyBtnCls(state)}`}
    >
      {copyBtnLabel(state, label)}
    </button>
  );
}

function StageRow({
  label,
  ts,
  actor,
  tone,
  note,
  testId,
}: {
  label: string;
  ts: number;
  actor: string | null;
  tone: "claim" | "verified";
  note?: string;
  testId?: string;
}) {
  return (
    <li className="flex items-center gap-1.5 flex-wrap" data-testid={testId}>
      <span
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${
          tone === "claim" ? "bg-g-yellow" : "bg-g-green"
        }`}
        aria-hidden
      />
      <span className="text-[11px] text-g-fg-2">{label}</span>
      <span className="text-[10px] text-g-fg-4 font-mono">{formatDateTime(ts)}</span>
      {actor && <span className="text-[10px] text-g-fg-3">by {actor}</span>}
      {note && <span className="text-[10px] text-g-yellow">{note}</span>}
    </li>
  );
}

export default function DeliveryCard({ data }: { data: TaskTimelineResponse }) {
  const d: DeliverySummary = extractDelivery(data);
  const { artifacts } = d;

  return (
    <div data-testid="delivery-card" className="px-4 py-3 border-b border-g-border bg-white">
      <h4 className="text-[11px] font-medium text-g-fg-3 mb-1.5">交付与产物</h4>

      {/* ── 说完成 vs 已验证：两列分呈现（§9.4 / §9.5）────────── */}
      <ul className="space-y-1">
        {d.claim ? (
          <StageRow
            testId="delivery-claim"
            tone="claim"
            label={
              d.claim.count > 1
                ? `执行者声明完成（第 ${d.claim.count} 次提交）`
                : "执行者声明完成"
            }
            ts={d.claim.ts}
            actor={d.claim.actor}
            note={d.claim.reworkAfter ? "其后已被打回，需重新提交" : undefined}
          />
        ) : (
          <li className="text-[11px] text-g-fg-4" data-testid="delivery-claim-none">
            未记录到执行者提交
          </li>
        )}
        {d.verifications.length > 0 ? (
          d.verifications.map((v) => (
            <StageRow
              key={v.kind}
              tone="verified"
              label={v.label}
              ts={v.ts}
              actor={v.actor}
            />
          ))
        ) : (
          <li className="text-[11px] text-g-fg-4" data-testid="delivery-unverified">
            尚无验证记录 —— 已提交 ≠ 已验证
          </li>
        )}
      </ul>

      {/* ── 产物清单：只有真实记录的路径才有操作；绝不渲染链接 ── */}
      <div className="mt-2">
        <p className="text-[11px] text-g-fg-3 mb-1">
          产物{artifacts.files.length > 0 && `（${artifacts.files.length} 个文件路径）`}
        </p>
        {artifacts.files.length > 0 ? (
          <ul className="space-y-1">
            {artifacts.files.map((f, i) => (
              <li key={`${f}-${i}`} className="flex items-center gap-1.5 min-w-0">
                <code
                  className="flex-1 min-w-0 truncate text-[10px] font-mono text-g-fg-2 bg-g-bg-soft rounded-gm px-1.5 py-0.5"
                  title={f}
                >
                  {f}
                </code>
                <CopyButton text={f} label="复制路径" testId={i === 0 ? "delivery-copy-path" : undefined} />
              </li>
            ))}
            {artifacts.mergeCommit && (
              <li className="flex items-center gap-1.5 min-w-0">
                <code
                  className="flex-1 min-w-0 truncate text-[10px] font-mono text-g-fg-3 bg-g-bg-soft rounded-gm px-1.5 py-0.5"
                  title={`合并提交 ${artifacts.mergeCommit}`}
                >
                  合并提交 {artifacts.mergeCommit.slice(0, 12)}
                  {artifacts.targetBranch ? ` → ${artifacts.targetBranch}` : ""}
                </code>
                <CopyButton text={artifacts.mergeCommit} label="复制提交号" testId="delivery-copy-commit" />
              </li>
            )}
          </ul>
        ) : (
          <p className="text-[11px] text-g-fg-4" data-testid="delivery-no-artifacts">
            暂无产物记录 —— 平台未记录本任务的产物文件
          </p>
        )}
        {/* T-24：明确能力边界，不给无效下载承诺；路径只能复制查看 */}
        <p className="mt-1.5 text-[10px] text-g-fg-4" data-testid="delivery-capability-note">
          产物路径仅供复制查看，平台暂未提供产物下载与预览入口。
        </p>
      </div>

      {/* ── 已知限制 ──────────────────────────────────────────── */}
      {d.limitations.length > 0 && (
        <div className="mt-1.5" data-testid="delivery-limitations">
          {d.limitations.map((l, i) => (
            <p key={i} className="text-[10px] text-g-yellow">
              已知限制：{l}
            </p>
          ))}
        </div>
      )}

      {/* ── 用户验收入口：无验收 API 时不放假按钮，只说明现状 ──── */}
      <p className="mt-1.5 text-[10px] text-g-fg-4" data-testid="delivery-acceptance-note">
        用户验收：以上验证记录齐备后由用户确认；平台暂未开放一键验收操作。
      </p>
    </div>
  );
}
