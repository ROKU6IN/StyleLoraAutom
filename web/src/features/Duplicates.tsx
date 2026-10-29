import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import { projectPath } from '../api/endpoints';
import type { ImageItem, Page } from '../api/types';
import type { Scope } from './cleaning/types';
import { useAction } from '../hooks/useAction';
import { useMedia } from '../hooks/useMedia';
import { useSelection } from '../state/selection';
import { canWrite, useSession } from '../state/session';
import { projectKey, queryClient } from '../state/queryClient';
import { Badge, Button, EmptyState, ErrorBox, Field, TaskProgress, ui } from '../ui';
import styles from './duplicates.module.css';

interface Pair {
  left_id: number;
  right_id: number;
  distance: number;
}
interface Scan extends Page<Pair> {
  scan_id: string;
  operation_id: string;
  version: string;
  threshold: number;
  status: 'pending' | 'running' | 'done' | 'failed' | 'canceled';
  input_count: number;
  eligible: number;
  skipped: Record<string, number>;
  completed: number;
  total: number;
  matched_so_far: number;
  expires_at: string;
  reason: string | null;
}

function Candidate({ pid, id }: { pid: string; id: number }) {
  const navigate = useNavigate();
  const image = useQuery({
    queryKey: projectKey(pid, 'image', id),
    queryFn: ({ signal }) =>
      request<ImageItem>('GET', `${projectPath(pid)}/images/${id}`, undefined, { signal }),
  });
  const media = useMedia(image.data?.thumbnail_url ?? null);
  return (
    <div className={ui.stack}>
      <div className={styles.preview}>
        {media.url ? (
          <img src={media.url} alt={`候选图片 ${id}`} />
        ) : (
          <p>{media.failed ? '图片暂时无法读取' : '正在读取图片…'}</p>
        )}
      </div>
      <div className={ui.between}>
        <strong>图片 {id}</strong>
        {image.data && <Badge status={image.data.review_status} />}
      </div>
      <ErrorBox message={image.error ? errorText(image.error) : undefined} />
      <Button
        quiet
        disabled={!image.data}
        onClick={() => {
          useSelection.getState().focusOn(id);
          void navigate(`/p/${pid}/workbench`);
        }}
      >
        在工作台审核图片 {id}
      </Button>
    </div>
  );
}

export default function Duplicates({ pid }: { pid: string }) {
  const [params, setParams] = useSearchParams();
  const [latest, setLatest] = useState(
    () => queryClient.getQueryData<string>(projectKey(pid, 'duplicate-latest')) ?? null,
  );
  const scanId = params.get('scan') ?? latest;
  const [threshold, setThreshold] = useState(''),
    [scopeKind, setScopeKind] = useState('project');
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const action = useAction(),
    connection = useSession((s) => s.connection),
    selection = useSelection((s) => s.selection);
  const cursor = cursors.at(-1);
  const base = `${projectPath(pid)}/duplicates/scans`;
  const registry = useQuery({
    queryKey: projectKey(pid, 'duplicate-scans'),
    queryFn: ({ signal }) =>
      request<{ items: { scan_id: string; threshold: number }[] }>('GET', base, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const result = useQuery({
    queryKey: projectKey(pid, 'duplicate-scan', scanId, cursor),
    queryFn: ({ signal }) =>
      request<Scan>(
        'GET',
        `${base}/${scanId}?limit=10${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
        undefined,
        { signal },
      ),
    enabled: !!scanId && connection !== 'reauth',
    refetchOnMount: 'always',
    refetchInterval: (query) => {
      if (connection === 'reauth' || query.state.error) return false;
      if (['pending', 'running'].includes(query.state.data?.status ?? 'pending')) return 500;
      return Math.max(1000, Date.parse(query.state.data!.expires_at) - Date.now() + 1000);
    },
  });
  const scan = result.error ? undefined : result.data,
    running = !!scanId && !result.error && (!scan || ['pending', 'running'].includes(scan.status));
  const selectedCount = selection.kind === 'ids' ? selection.ids.size : selection.total;
  const validThreshold = /^\d+$/.test(threshold) && Number(threshold) <= 64;
  const chooseScan = (id: string | null) => {
    setLatest(id);
    queryClient.setQueryData(projectKey(pid, 'duplicate-latest'), id);
    setParams(id ? { scan: id } : {}, { replace: true });
    setCursors([null]);
  };
  const skipNames: Record<string, string> = {
    missing: '文件缺失或路径不可用',
    size_changed: '文件大小已变化',
    no_hash: '没有指纹',
    invalid_hash: '指纹格式无效',
  };
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>REVIEW / DUPLICATES</div>
          <h1>近似重复扫描</h1>
          <p>寻找可能相似的图片对，由你逐张审核。距离为 0 也不代表图片相同。</p>
        </div>
      </div>
      {!!registry.data?.items.length && (
        <section className={ui.section}>
          <Field id="duplicate-history" label="本次会话的扫描结果">
            <select
              id="duplicate-history"
              value={scanId ?? ''}
              onChange={(e) => chooseScan(e.target.value || null)}
            >
              <option value="">选择已有扫描</option>
              {registry.data.items.map((item) => (
                <option key={item.scan_id} value={item.scan_id}>
                  阈值 {item.threshold} · {item.scan_id.slice(0, 8)}
                </option>
              ))}
            </select>
          </Field>
        </section>
      )}
      <section className={ui.section}>
        <div className={ui.formGrid}>
          <Field id="duplicate-scope" label="扫描范围">
            <select
              id="duplicate-scope"
              value={scopeKind}
              disabled={running}
              onChange={(e) => setScopeKind(e.target.value)}
            >
              <option value="project">项目中未剔除的图片</option>
              <option value="selection" disabled={!selectedCount}>
                工作台选中范围（{selectedCount} 张）
              </option>
            </select>
          </Field>
          <Field
            id="duplicate-threshold"
            label="最大汉明距离（必填，0–64）"
            hint="值越大，候选越多；请按素材特点填写。"
          >
            <input
              id="duplicate-threshold"
              type="number"
              min={0}
              max={64}
              step={1}
              value={threshold}
              disabled={running}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </Field>
        </div>
        <p className={ui.muted}>
          最多 10,000 张。新导入图片自动计算指纹；旧图片没有指纹时会跳过。结果保留 15 分钟，关闭项目后释放。
        </p>
        <div className={ui.row}>
          <Button
            primary
            disabled={
              !validThreshold ||
              !canWrite() ||
              action.busy ||
              running ||
              (scopeKind === 'selection' && !selectedCount)
            }
            onClick={() =>
              void action.run(async () => {
                const scope: Scope =
                  scopeKind === 'selection'
                    ? selection.kind === 'ids'
                      ? { selected_ids: [...selection.ids] }
                      : { filter: selection.query }
                    : { filter: {} };
                const accepted = await request<{ scan_id: string }>('POST', base, {
                  scope,
                  threshold: Number(threshold),
                });
                chooseScan(accepted.scan_id);
                await queryClient.invalidateQueries({ queryKey: projectKey(pid, 'duplicate-scans') });
              })
            }
          >
            开始扫描
          </Button>
          {scanId && (
            <Button
              quiet
              disabled={action.busy || !canWrite()}
              onClick={() =>
                void action.run(async () => {
                  try {
                    const current = await request<Scan>('GET', `${base}/${scanId}`);
                    await request('DELETE', `${base}/${scanId}`, undefined, { version: current.version });
                  } catch (error) {
                    if (!(error instanceof AppError && error.status === 410)) throw error;
                  }
                  queryClient.removeQueries({ queryKey: projectKey(pid, 'duplicate-scan', scanId) });
                  chooseScan(null);
                  await queryClient.invalidateQueries({ queryKey: projectKey(pid, 'duplicate-scans') });
                })
              }
            >
              {running ? '取消扫描并释放结果' : '释放扫描结果'}
            </Button>
          )}
        </div>
      </section>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : result.error
              ? errorText(result.error)
              : registry.error
                ? errorText(registry.error)
                : scan?.reason
                  ? errorText(new AppError(503, scan.reason))
                  : undefined
        }
      />
      {!scanId && (
        <EmptyState title="尚未扫描" description="选择范围并填写阈值后开始。扫描不会自动剔除或删除图片。" />
      )}
      {scan && (
        <section className={ui.section} aria-label="扫描进度">
          <div className={ui.between}>
            <h2>
              {
                {
                  pending: '等待扫描',
                  running: '正在扫描',
                  done: '扫描完成',
                  failed: '扫描失败',
                  canceled: '扫描已取消',
                }[scan.status]
              }
            </h2>
            <span className={ui.tag}>本次阈值 {scan.threshold}</span>
          </div>
          <p>
            范围 {scan.input_count} 张 · 有效指纹 {scan.eligible} 张 · 已发现 {scan.matched_so_far} 对候选
          </p>
          <TaskProgress done={scan.completed} total={scan.total} label="图片对比进度" />
          {Object.entries(scan.skipped).map(([kind, count]) => (
            <p className={ui.muted} key={kind}>
              {skipNames[kind] ?? kind}：{count} 张
            </p>
          ))}
          <p className={ui.caption}>
            相似关系不具有传递性：A 与 B、B 与 C 相似，不代表 A 与 C
            相似。候选来自扫描开始时的固定范围；审核后不会从结果中自动移除。
          </p>
        </section>
      )}
      {scan?.status === 'done' && (
        <>
          {!scan.items.length && (
            <EmptyState
              title="没有符合阈值的候选对"
              description="请查看跳过计数；这不代表所有图片都已经过相似度判断。"
            />
          )}
          {scan.items.map((pair) => (
            <section
              className={ui.section}
              key={`${pair.left_id}-${pair.right_id}`}
              aria-label={`候选对 ${pair.left_id} 与 ${pair.right_id}`}
            >
              <div className={ui.between}>
                <h2>
                  图片 {pair.left_id} ↔ 图片 {pair.right_id}
                </h2>
                <span className={ui.tag}>距离 {pair.distance} / 64</span>
              </div>
              <div className={ui.formGrid}>
                <Candidate pid={pid} id={pair.left_id} />
                <Candidate pid={pid} id={pair.right_id} />
              </div>
            </section>
          ))}
          {scan.upper_id !== 0 && (
            <div className={ui.between}>
              <Button disabled={cursors.length === 1} onClick={() => setCursors((old) => old.slice(0, -1))}>
                上一页
              </Button>
              <span>
                第 {cursors.length} 页 · 共 {scan.upper_id} 对
              </span>
              <Button
                disabled={!scan.next_cursor}
                onClick={() => setCursors((old) => [...old, scan.next_cursor])}
              >
                下一页
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
