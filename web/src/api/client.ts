import { AppError, isAbort } from './errors';
import type { Accepted, Operation, Page, Session } from './types';

let bearer: string | null = null;
let authFailure: (() => void) | undefined;
export function onAuthFailure(callback: () => void) {
  authFailure = callback;
}
export function setSessionToken(value: string | null) {
  bearer = value;
}
export function authHeaders() {
  if (!bearer) throw new AppError(401, 'auth:required');
  return { Authorization: `Bearer ${bearer}` };
}
export function takeBootstrapCode() {
  const code = new URLSearchParams(location.hash.slice(1)).get('bootstrap');
  if (code) history.replaceState(null, '', `${location.pathname}${location.search}#/`);
  return code;
}
export async function bootstrap(code: string | null): Promise<Session> {
  if (!code) throw new AppError(401, 'auth:required');
  const response = await fetch('/api/session/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
    cache: 'no-store',
    credentials: 'omit',
    redirect: 'error',
  });
  if (!response.ok) throw new AppError(response.status, 'auth:bootstrap_invalid');
  const value = (await response.json()) as { token: string };
  setSessionToken(value.token);
  return request<Session>('GET', '/api/session');
}
export interface RequestOptions {
  responseType?: 'json' | 'blob';
  signal?: AbortSignal;
  version?: string;
  key?: string;
  expectedVersions?: Record<string, string>;
}
export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  if (!path.startsWith('/api/') || path.includes('\\') || path.includes('#'))
    throw new AppError(422, 'request:invalid_path');
  const headers: Record<string, string> = authHeaders();
  const multipart = body instanceof FormData;
  if (body !== undefined && !multipart) headers['Content-Type'] = 'application/json';
  if (options.version) headers['If-Match'] = `"${options.version}"`;
  if (options.expectedVersions) headers['X-Expected-Versions'] = JSON.stringify(options.expectedVersions);
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method))
    headers['Idempotency-Key'] = options.key ?? crypto.randomUUID();
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: multipart ? body : body === undefined ? undefined : JSON.stringify(body),
      signal: options.signal,
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
    });
  } catch (error) {
    if (isAbort(error)) throw error;
    throw new AppError(
      0,
      method === 'GET' ? 'network:offline' : 'request:unknown',
      method === 'GET' ? 'not_sent' : 'unknown',
      { status_url: path },
    );
  }
  if (response.status === 401) {
    bearer = null;
    authFailure?.();
  }
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw AppError.from(response.status, value.error);
  }
  if (response.status === 204) return undefined as T;
  if (options.responseType === 'blob') return response.blob() as Promise<T>;
  return response.json() as Promise<T>;
}
export async function allPages<T>(path: string, signal?: AbortSignal): Promise<T[]> {
  const values: T[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<T> = await request(
      'GET',
      `${path}${path.includes('?') ? '&' : '?'}limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
      undefined,
      { signal },
    );
    values.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return values;
}
export async function waitOperation<T>(accepted: Accepted, signal?: AbortSignal): Promise<T> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const task = await request<Operation<T>>('GET', accepted.status_url, undefined, { signal });
    if (task.status === 'done') return task.result;
    if (task.status === 'canceled') throw new AppError(409, 'operation:canceled');
    if (task.status === 'failed' || task.status === 'interrupted')
      throw AppError.from(409, task.error ?? undefined);
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new AppError(0, 'request:unknown', 'unknown', { status_url: accepted.status_url });
}
