declare module 'phoenix' {
  export class Socket {
    constructor(url: string, opts?: any);
    connect(): void;
    disconnect(): void;
    channel(topic: string, params?: any): Channel;
    on(event: string, callback: (...args: any[]) => void): void;
    remove(event: string, callback?: (...args: any[]) => void): void;
    /** 连接建立（含重连）回调；运行时由 phoenix.mjs 提供，返回内部 ref。 */
    onOpen(callback: () => void): number;
    /** 已建立的连接次数（onConnOpen 内自增，先于 open 回调触发）。 */
    establishedConnections: number;
  }

  export class Channel {
    constructor(topic: string, params?: any);
    join(): { receive(status: string, callback: (...args: any[]) => void): any };
    leave(): void;
    push(event: string, payload?: any): void;
    on(event: string, callback: (...args: any[]) => void): void;
    off(event: string, callback?: (...args: any[]) => void): void;
    state: string;
  }
}
