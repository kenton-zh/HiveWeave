/**
 * ErrorBoundary — 懒加载面板/对话框的渲染错误围栏（09-08 EXE 白屏防御）。
 *
 * 白屏机制：React.lazy chunk 加载失败或面板渲染抛错时，若无 boundary，
 * React 19 会卸掉整棵树 → 全屏白。此围栏把错误限制在面板区域内并给出
 * 可操作提示；对话框类 overlay 可传 fallback={null} 静默降级。
 *
 * 「重载页面」是唯一能重新拉取失败 chunk 的路径——React.lazy 一旦
 * reject 在本会话内终身 rejected（见 App.tsx Token 静态导入注记）；
 * 配合后端 index.html no-cache，重载即拿新构建。
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  label?: string;
  /** 错误时的整体替换渲染；传 null 可让 overlay 静默消失。缺省=错误面板。 */
  fallback?: ReactNode;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[ErrorBoundary]${this.props.label ? `(${this.props.label})` : ""}`,
      error,
      info.componentStack,
    );
  }

  render() {
    if (this.state.error === null) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2 p-6 text-center">
        <div className="text-sm font-medium text-g-red">
          {this.props.label ? `「${this.props.label}」面板出错了` : "面板出错了"}
        </div>
        <div className="text-xs text-g-fg-3 max-w-full break-all">
          {this.state.error.message}
        </div>
        <button
          onClick={() => window.location.reload()}
          className="px-3 py-1.5 text-xs rounded-gm bg-g-blue text-white hover:opacity-90"
        >
          重载页面
        </button>
      </div>
    );
  }
}
