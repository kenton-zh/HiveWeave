/**
 * winbox 0.2.82 类型声明（手写）
 *
 * 该包 package.json 无 `types` 字段，上游也未发布 @types。此处按官方 README
 * 的公开 API 手写**最小**声明 —— 只覆盖本项目实际调用的部分，避免假装覆盖全量。
 * 上游 API 文档：node_modules/winbox/README.md
 */
/**
 * ⚠ 模块名必须是**深路径** `winbox/src/js/winbox.js`，不能写 `"winbox"`：
 * winbox 的 package.json 同时有 `main`（ESM 源）与 `browser`（UMD bundle），
 * Vite 生产构建优先取 `browser` ⇒ rollup 报 `"default" is not exported by
 * winbox/dist/winbox.bundle.min.js`。深路径引用 `main` 指向的 ESM 源即可绕开
 * （该文件第 329 行 `export default WinBox`）。类型声明因此也挂在深路径上。
 */
declare module "winbox/src/js/winbox.js" {
  export interface WinBoxOptions {
    /** 标题栏文字 */
    title?: string;
    /** 附加类名（多类用空格分隔或数组）—— 本项目用 "hw-win" 挂像素皮肤 */
    class?: string | string[];
    x?: number | string;
    y?: number | string;
    width?: number | string;
    height?: number | string;
    minwidth?: number;
    minheight?: number;
    maxwidth?: number;
    maxheight?: number;
    top?: number | string;
    right?: number | string;
    bottom?: number | string;
    left?: number | string;
    /** 直接挂 DOM 片段 */
    mount?: HTMLElement;
    background?: string;
    border?: number;
    index?: number;
    modal?: boolean;
    autosize?: boolean;
    overflow?: boolean;
    onclose?: (this: WinBoxInstance, force?: boolean) => boolean | void;
    onfocus?: (this: WinBoxInstance) => void;
    onblur?: (this: WinBoxInstance) => void;
    onmove?: (this: WinBoxInstance, x: number, y: number) => void;
    onresize?: (this: WinBoxInstance, width: number, height: number) => void;
    onmaximize?: (this: WinBoxInstance) => void;
    onminimize?: (this: WinBoxInstance) => void;
    onrestore?: (this: WinBoxInstance) => void;
  }

  export interface WinBoxInstance {
    id: string;
    /** 内容区 DOM —— React portal 的挂载目标 */
    body: HTMLElement;
    /** ⚠ 是数字（header 高度 px），不是元素 —— 见 winbox.js:171 */
    header: number;
    /** ⚠ 是标题文本，不是元素 */
    title: string;
    window: HTMLElement;
    /** 当前 z-index（由 winbox 的 index_counter 分配） */
    index: number;
    focus(): WinBoxInstance;
    blur(): WinBoxInstance;
    hide(): WinBoxInstance;
    show(): WinBoxInstance;
    /** 返回 `true` 表示「关闭被 onclose 中止」；正常关闭返回 `undefined`（winbox.js:1207-1212） */
    close(force?: boolean): boolean | undefined;
    setTitle(title: string): WinBoxInstance;
    setBackground(background: string): WinBoxInstance;
    move(x?: number | string, y?: number | string): WinBoxInstance;
    resize(width?: number | string, height?: number | string): WinBoxInstance;
    maximize(): WinBoxInstance;
    minimize(): WinBoxInstance;
    restore(): WinBoxInstance;
    addClass(name: string): WinBoxInstance;
    removeClass(name: string): WinBoxInstance;
  }

  export interface WinBoxConstructor {
    new (options?: WinBoxOptions | string | HTMLElement): WinBoxInstance;
    (options?: WinBoxOptions | string | HTMLElement): WinBoxInstance;
    /** 当前所有实例，按层级从低到高 */
    stack(): WinBoxInstance[];
    // ⚠ 不要在此声明 mount()/unmount()：它们是**实例**方法（winbox.js:814/831 挂在
    //   原型上），不是静态方法。本项目未使用，删掉以免误导调用方。
  }

  const WinBox: WinBoxConstructor;
  export default WinBox;
}
