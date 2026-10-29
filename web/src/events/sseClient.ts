import { authHeaders } from '../api/client';
import { AppError } from '../api/errors';
import { timing } from '../tokens/base';
export interface Frame {
  id: string;
  event: string;
  data: string;
  retry?: number;
}
export class SseDecoder {
  private buffer = '';
  private data: string[] = [];
  private dataSize = 0;
  private id = '';
  private event = '';
  private retry: number | undefined;
  push(text: string, end = false): Frame[] {
    this.buffer += text.replace(/^\uFEFF/, '');
    const frames: Frame[] = [];
    if (this.buffer.length > 128 * 1024) throw new Error('SSE frame exceeds budget');
    for (;;) {
      const newline = /\r\n|\r|\n/.exec(this.buffer);
      if (!newline || (!end && newline[0] === '\r' && newline.index === this.buffer.length - 1)) break;
      const line = this.buffer.slice(0, newline.index);
      this.buffer = this.buffer.slice(newline.index + newline[0].length);
      if (!line) {
        if (this.data.length)
          frames.push({
            id: this.id,
            event: this.event || 'message',
            data: this.data.join('\n'),
            retry: this.retry,
          });
        this.data = [];
        this.dataSize = 0;
        this.event = '';
        this.retry = undefined;
      } else if (!line.startsWith(':')) {
        const split = line.indexOf(':');
        const field = split < 0 ? line : line.slice(0, split);
        const value = split < 0 ? '' : line.slice(split + 1).replace(/^ /, '');
        if (field === 'data') {
          this.dataSize += value.length + 1;
          if (this.dataSize > 128 * 1024) throw new Error('SSE frame exceeds budget');
          this.data.push(value);
        } else if (field === 'event') this.event = value;
        else if (field === 'id' && !value.includes('\0')) this.id = value;
        else if (field === 'retry' && /^\d+$/.test(value)) this.retry = Number(value);
      }
    }
    return frames;
  }
}
export interface EventData {
  epoch: string;
  sequence: number;
  project_id: string | null;
  resource: { kind: string; id: number | string | null };
  payload: Record<string, unknown>;
}
export class EventCursor {
  id = '';
  private epoch = '';
  private sequence = -1;
  reset() {
    this.id = '';
    this.epoch = '';
    this.sequence = -1;
  }
  accept(frame: Frame, data: EventData) {
    if (this.epoch === data.epoch && data.sequence <= this.sequence && frame.event !== 'stream.ready')
      return false;
    this.epoch = data.epoch;
    this.sequence = data.sequence;
    this.id = frame.id;
    return true;
  }
}
export function retryDelay(attempt: number, random = Math.random()) {
  return Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5)) * (0.8 + random * 0.4);
}
function pause(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}
export async function subscribeEvents(
  signal: AbortSignal,
  onEvent: (frame: Frame, data: EventData) => void,
  onState: (state: 'online' | 'degraded' | 'reauth') => void,
) {
  const cursor = new EventCursor();
  let attempt = 0;
  while (!signal.aborted) {
    const local = new AbortController();
    const abort = () => local.abort();
    signal.addEventListener('abort', abort, { once: true });
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    try {
      const response = await fetch('/api/events', {
        headers: { ...authHeaders(), ...(cursor.id ? { 'Last-Event-ID': cursor.id } : {}) },
        signal: local.signal,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
      });
      if ([401, 403].includes(response.status)) {
        onState('reauth');
        return;
      }
      if (!response.ok || !response.body) throw new Error('SSE unavailable');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseDecoder();
      let resync = false;
      try {
        while (!signal.aborted) {
          clearTimeout(watchdog);
          watchdog = setTimeout(() => local.abort(), timing.heartbeat);
          const chunk = await reader.read();
          if (chunk.done) break;
          for (const frame of parser.push(decoder.decode(chunk.value, { stream: true }))) {
            const data = JSON.parse(frame.data) as EventData;
            if (frame.event === 'stream.resync_required') {
              cursor.reset();
              onEvent(frame, data);
              resync = true;
              break;
            }
            if (!cursor.accept(frame, data)) continue;
            if (frame.event === 'stream.ready') {
              attempt = 0;
              onState('online');
            }
            onEvent(frame, data);
          }
          if (resync) break;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    } catch (error) {
      if (error instanceof AppError && error.status === 401) {
        onState('reauth');
        return;
      }
    } finally {
      clearTimeout(watchdog);
      local.abort();
      signal.removeEventListener('abort', abort);
    }
    if (!signal.aborted) {
      onState('degraded');
      await pause(retryDelay(attempt++), signal);
    }
  }
}
