import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useBlocker } from 'react-router';
import { Button, Modal, ui } from '../ui';
type Draft = { dirty: boolean; label?: string; save: () => Promise<boolean>; discard: () => void };
type Guard = {
  dirty: boolean;
  register: (draft: Draft | null) => void;
  run: (action: () => void, cancel?: () => void) => void;
};
const Context = createContext<Guard | null>(null);
export const useDraftGuard = () => useContext(Context)!;
export function DraftProvider({ children }: { children: ReactNode }) {
  const draft = useRef<Draft | null>(null),
    pending = useRef<{ action: () => void; cancel?: () => void } | null>(null);
  const [dirty, setDirty] = useState(false),
    [label, setLabel] = useState('标注'),
    [open, setOpen] = useState(false),
    [saving, setSaving] = useState(false);
  const register = useCallback((value: Draft | null) => {
    draft.current = value;
    setDirty(value?.dirty ?? false);
    setLabel(value?.label ?? '标注');
  }, []);
  const run = useCallback((action: () => void, cancel?: () => void) => {
    if (draft.current?.dirty) {
      pending.current = { action, cancel };
      setOpen(true);
    } else action();
  }, []);
  const finish = (proceed: boolean) => {
    const job = pending.current;
    pending.current = null;
    setOpen(false);
    if (proceed) job?.action();
    else job?.cancel?.();
  };
  useEffect(() => {
    if (!dirty) return;
    const listener = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', listener);
    return () => window.removeEventListener('beforeunload', listener);
  }, [dirty]);
  return (
    <Context.Provider value={{ dirty, register, run }}>
      {children}
      <Modal
        open={open}
        onOpenChange={(value) => {
          if (!value && !saving) finish(false);
        }}
        title={`有未保存的${label}`}
        description="切换图片或离开页面前，请选择如何处理当前草稿。"
      >
        <div className={ui.actions}>
          <Button disabled={saving} onClick={() => finish(false)}>
            继续编辑
          </Button>
          <Button
            disabled={saving}
            onClick={() => {
              draft.current?.discard();
              finish(true);
            }}
          >
            放弃草稿
          </Button>
          <Button
            primary
            disabled={saving}
            onClick={() => {
              setSaving(true);
              void draft.current
                ?.save()
                .then((ok) => {
                  if (ok) finish(true);
                  else finish(false);
                })
                .finally(() => setSaving(false));
            }}
          >
            保存并继续
          </Button>
        </div>
      </Modal>
    </Context.Provider>
  );
}
export function NavigationGuard() {
  const { dirty, run } = useDraftGuard();
  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (blocker.state === 'blocked')
      run(
        () => blocker.proceed(),
        () => blocker.reset(),
      );
  }, [blocker, run]);
  return null;
}
