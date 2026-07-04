declare module 'phoenix' {
  export class Socket {
    constructor(url: string, opts?: any);
    connect(): void;
    disconnect(): void;
    channel(topic: string, params?: any): Channel;
    on(event: string, callback: (...args: any[]) => void): void;
    remove(event: string, callback?: (...args: any[]) => void): void;
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
