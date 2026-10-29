import { QueryClient } from '@tanstack/react-query';
import { AppError } from '../api/errors';
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) =>
        count < 2 && !(error instanceof AppError && error.status >= 400 && error.status < 500),
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
    },
    mutations: { retry: false },
  },
});
export const projectKey = (pid: string, resource: string, ...rest: unknown[]) =>
  ['project', pid, resource, ...rest] as const;
