import { useEffect, useState } from 'react';
import { mediaCache } from '../api/media';
import { isAbort } from '../api/errors';
export function useMedia(path: string | null) {
  const [result, setResult] = useState<{ path: string | null; url: string | null; failed: boolean }>({
    path: null,
    url: null,
    failed: false,
  });
  useEffect(() => {
    if (!path) return;
    let mounted = true;
    const lease = mediaCache.acquire(path);
    lease.ready
      .then((url) => {
        if (mounted) setResult({ path, url, failed: false });
      })
      .catch((error) => {
        if (mounted && !isAbort(error)) setResult({ path, url: null, failed: true });
      });
    return () => {
      mounted = false;
      lease.release();
    };
  }, [path]);
  return result.path === path ? result : { path, url: null, failed: false };
}
