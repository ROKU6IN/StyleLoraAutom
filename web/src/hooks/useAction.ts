import { useRef, useState } from 'react';
import { AppError } from '../api/errors';
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const run = async (work: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (failure) {
      if (!(failure instanceof AppError && failure.code === 'operation:canceled')) setError(failure);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  return { busy, error, setError, run };
}
