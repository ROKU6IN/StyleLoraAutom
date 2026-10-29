import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, ui } from '../ui';
import { storageSize } from './storageFormat';

const MIB = 1024 ** 2;
export interface StorageConfig {
  revision: number;
  version: string;
  storage_thresholds: { project_bytes: number; total_bytes: number };
  limits: { minimum_bytes: number; maximum_bytes: number; multiple_bytes: number };
}
export default function StorageThresholds() {
  const connection = useSession((s) => s.connection);
  const config = useQuery({
    queryKey: ['product-config'],
    queryFn: ({ signal }) => request<StorageConfig>('GET', '/api/config', undefined, { signal }),
    enabled: connection !== 'reauth',
  });
  return (
    <div className="page">
      <section className={ui.section} aria-label="存储提醒设置">
        <h2>存储提醒设置</h2>
        <p className={ui.muted}>
          设置保存在本机，所有项目共享。修改只影响之后发起的扫描；旧报告保留当时阈值，需要重新扫描来更新提醒。
        </p>
        <ErrorBox message={config.error ? errorText(config.error) : undefined} />
        {config.isPending && <p>正在读取提醒设置…</p>}
        {config.data?.storage_thresholds && <ThresholdForm config={config.data} blocked={!!config.error} />}
        {!config.data && config.error && (
          <Button onClick={() => void config.refetch()}>重新读取提醒设置</Button>
        )}
      </section>
    </div>
  );
}
function ThresholdForm({ config, blocked }: { config: StorageConfig; blocked: boolean }) {
  const [base, setBase] = useState(config);
  const [project, setProject] = useState(String(config.storage_thresholds.project_bytes / MIB));
  const [total, setTotal] = useState(String(config.storage_thresholds.total_bytes / MIB));
  const [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false),
    { register } = useDraftGuard();
  useSession((s) => s.connection);
  const dirty =
    project !== String(base.storage_thresholds.project_bytes / MIB) ||
    total !== String(base.storage_thresholds.total_bytes / MIB);
  const valid = [project, total].every(
    (text) =>
      /^\d+$/.test(text) &&
      Number.isSafeInteger(Number(text)) &&
      Number(text) * MIB >= config.limits.minimum_bytes &&
      Number(text) * MIB <= config.limits.maximum_bytes,
  );
  const adopt = useCallback((value: StorageConfig) => {
    setBase(value);
    setProject(String(value.storage_thresholds.project_bytes / MIB));
    setTotal(String(value.storage_thresholds.total_bytes / MIB));
  }, []);
  useEffect(() => {
    if (!dirty && !busy && !uncertain && config.version !== base.version) adopt(config);
  }, [config, base.version, dirty, busy, uncertain, adopt]);
  const save = useCallback(async () => {
    if (!valid || inFlight.current || uncertain || blocked || !canWrite()) return false;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const value = await request<StorageConfig>(
        'PATCH',
        '/api/config',
        { storage_thresholds: { project_bytes: Number(project) * MIB, total_bytes: Number(total) * MIB } },
        { version: base.version },
      );
      adopt(value);
      queryClient.setQueryData(['product-config'], value);
      void queryClient.invalidateQueries({ queryKey: ['storage-notices'] });
      useSession.getState().notify('存储提醒设置已保存，请重新扫描以更新结果');
      return true;
    } catch (e) {
      setError(e);
      if (e instanceof AppError && e.outcome === 'unknown') setUncertain(true);
      return false;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, [valid, uncertain, blocked, project, total, base.version, adopt]);
  useEffect(() => {
    register({ dirty, label: '存储提醒设置', save, discard: () => adopt(base) });
    return () => register(null);
  }, [register, dirty, save, adopt, base]);
  async function reload() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const value = await request<StorageConfig>('GET', '/api/config');
      adopt(value);
      queryClient.setQueryData(['product-config'], value);
      setUncertain(false);
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <ErrorBox message={error ? errorText(error) : undefined} />
      {config.version !== base.version && dirty && (
        <p role="status">设置已在别处更新，当前输入已保留；保存时将核对版本。</p>
      )}
      {uncertain && (
        <p role="status">保存结果未知，已停止重复提交。请重新载入已保存值核对；此操作会放弃当前输入。</p>
      )}
      <div className={ui.formGrid}>
        <Field
          id="storage-project-limit"
          label="单项目提醒阈值（MiB）"
          hint="1 GiB = 1024 MiB；默认 20480 MiB（20 GiB）。"
        >
          <input
            id="storage-project-limit"
            type="number"
            min="1"
            max={config.limits.maximum_bytes / MIB}
            step="1"
            value={project}
            disabled={busy || uncertain}
            onChange={(e) => setProject(e.target.value)}
          />
        </Field>
        <Field
          id="storage-total-limit"
          label="扫描范围总量提醒阈值（MiB）"
          hint="默认 102400 MiB（100 GiB）。选择部分项目时，仅判断选定范围。"
        >
          <input
            id="storage-total-limit"
            type="number"
            min="1"
            max={config.limits.maximum_bytes / MIB}
            step="1"
            value={total}
            disabled={busy || uncertain}
            onChange={(e) => setTotal(e.target.value)}
          />
        </Field>
      </div>
      <p className={ui.muted}>
        已保存：单项目 {storageSize(base.storage_thresholds.project_bytes)}，范围总量{' '}
        {storageSize(base.storage_thresholds.total_bytes)}。仅提醒，不会自动删除文件。
      </p>
      {!valid && <p role="alert">请输入 1–{config.limits.maximum_bytes / MIB} 的整数 MiB。</p>}
      <div className={ui.actions}>
        <Button
          primary
          disabled={busy || uncertain || blocked || !dirty || !valid || !canWrite()}
          onClick={() => void save()}
        >
          保存提醒设置
        </Button>
        <Button disabled={busy || !canWrite()} onClick={() => void reload()}>
          重新载入已保存值（放弃输入）
        </Button>
      </div>
    </>
  );
}
