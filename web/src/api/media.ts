import { authHeaders } from './client';
import { AppError } from './errors';
type Entry = {
  refs: number;
  size: number;
  url?: string;
  promise: Promise<string>;
  controller: AbortController;
  used: number;
};
export class MediaCache {
  private entries = new Map<string, Entry>();
  private active = 0;
  private queue: (() => void)[] = [];
  constructor(
    private limit = 64,
    private byteLimit = 48 * 1024 * 1024,
  ) {}
  acquire(path: string) {
    let entry = this.entries.get(path);
    if (!entry) {
      entry = {
        refs: 0,
        size: 0,
        used: Date.now(),
        controller: new AbortController(),
        promise: Promise.resolve(''),
      };
      const own = entry;
      own.promise = new Promise<string>((resolve, reject) => {
        const start = () => {
          if (own.controller.signal.aborted) {
            reject(new DOMException('Aborted', 'AbortError'));
            return;
          }
          this.active++;
          void Promise.resolve()
            .then(() =>
              fetch(path, {
                headers: authHeaders(),
                signal: own.controller.signal,
                credentials: 'omit',
                cache: 'no-cache',
                redirect: 'error',
              }),
            )
            .then(async (response) => {
              if (!response.ok)
                throw AppError.from(response.status, (await response.json().catch(() => ({}))).error);
              return response.blob();
            })
            .then((blob) => {
              own.controller.signal.throwIfAborted();
              own.size = blob.size;
              own.url = URL.createObjectURL(blob);
              resolve(own.url);
              this.evict();
            })
            .catch((error) => {
              if (this.entries.get(path) === own) this.entries.delete(path);
              reject(error);
            })
            .finally(() => {
              this.active--;
              this.pump();
            });
        };
        this.queue.push(start);
      });
      this.entries.set(path, entry);
    }
    entry.refs++;
    entry.used = Date.now();
    this.pump();
    const current = entry;
    let released = false;
    return {
      ready: entry.promise,
      release: () => {
        if (released) return;
        released = true;
        current.refs--;
        if (!current.refs && !current.url) {
          current.controller.abort();
          if (this.entries.get(path) === current) this.entries.delete(path);
        }
        this.evict();
      },
    };
  }
  private pump() {
    while (this.active < 6 && this.queue.length) this.queue.shift()!();
  }
  private evict() {
    let bytes = [...this.entries.values()].reduce((sum, e) => sum + e.size, 0);
    for (const [path, entry] of [...this.entries].sort((a, b) => a[1].used - b[1].used)) {
      if (this.entries.size <= this.limit && bytes <= this.byteLimit) break;
      if (entry.refs || !entry.url) continue;
      URL.revokeObjectURL(entry.url);
      bytes -= entry.size;
      this.entries.delete(path);
    }
  }
  clear() {
    for (const entry of this.entries.values()) {
      entry.controller.abort();
      if (entry.url) URL.revokeObjectURL(entry.url);
    }
    this.entries.clear();
    this.pump();
  }
}
export const mediaCache = new MediaCache();
