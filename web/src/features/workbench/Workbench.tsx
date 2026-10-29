import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { useInfiniteQuery, useQuery, type InfiniteData } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { allPages, request } from '../../api/client';
import { projectPath, queryFromUrl, queryString, readImages } from '../../api/endpoints';
import type {
  ImageItem,
  ImagePage,
  ImageStats,
  Review,
  ReviewPreview,
  ReviewResult,
  Source,
} from '../../api/types';
import { errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { useDraftGuard } from '../../hooks/drafts';
import { useSelection } from '../../state/selection';
import { usePrefs } from '../../state/prefs';
import { canWrite, useSession } from '../../state/session';
import { queryClient, projectKey } from '../../state/queryClient';
import { Button, EmptyState, ErrorBox, FloatingPanel, Modal, ui } from '../../ui';
import JobBatches from '../JobBatches';
import BatchIndicator from '../executors/BatchIndicator';
import { Editor } from './Editor';
import { Grid } from './Grid';
import { Preview } from './Preview';
import { BatchDialog } from '../cleaning/BatchDialog';
import type { Scope } from '../cleaning/types';
import { isEditingTarget, reviewKey } from './keyboard';
import { ReviewQueue, advanceUndoHistory, undoEntry, type UndoEntry } from './reviewQueue';
import styles from './workbench.module.css';
export default function Workbench({ pid }: { pid: string }) {
  const [params, setParams] = useSearchParams();
  const filter = useMemo(() => queryFromUrl(params), [params]);
  const sort = params.get('sort') === '-id' ? '-id' : 'id';
  const [search, setSearch] = useState(filter.q ?? '');
  const filterPending = search !== (filter.q ?? '');
  const [columns, setColumns] = useState(1);
  const selection = useSelection(),
    prefs = usePrefs(),
    { run: guardRun } = useDraftGuard();
  const connection = useSession((s) => s.connection);
  const action = useAction();
  const [executorOpen, setExecutorOpen] = useState(false);
  const executorBatch = params.get('executor_batch');
  const [batch, setBatch] = useState<{ kind: 'bulk' | 'rule' | 'stats'; scope: Scope } | null>(null);
  const openBatch = (kind: 'bulk' | 'rule' | 'stats') =>
    guardRun(() => {
      const selected = useSelection.getState().selection;
      const scope: Scope =
        selected.kind === 'filter'
          ? { filter: selected.query }
          : {
              selected_ids: selected.ids.size
                ? [...selected.ids]
                : selection.focus !== null
                  ? [selection.focus]
                  : [],
            };
      setBatch({ kind, scope });
    });
  const [reviewError, setReviewError] = useState<unknown>(null),
    [reviewBusy, setReviewBusy] = useState(false),
    [undo, setUndo] = useState<UndoEntry[]>([]),
    [previewOpen, setPreviewOpen] = useState(false),
    [confirmation, setConfirmation] = useState<{ preview: ReviewPreview; target: Review } | null>(null);
  const images = useInfiniteQuery({
    queryKey: projectKey(pid, 'images', filter, sort),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => readImages(pid, filter, sort, pageParam, signal),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: connection === 'online' ? false : connection === 'reauth' ? false : 5000,
  });
  const stats = useQuery({
    queryKey: projectKey(pid, 'stats', filter),
    queryFn: ({ signal }) =>
      request<ImageStats>('GET', `${projectPath(pid)}/images/stats?${queryString(filter)}`, undefined, {
        signal,
      }),
  });
  const sources = useQuery({
    queryKey: projectKey(pid, 'sources'),
    queryFn: ({ signal }) => allPages<Source>(`${projectPath(pid)}/sources`, signal),
  });
  const items = useMemo(() => images.data?.pages.flatMap((p) => p.items) ?? [], [images.data]);
  const total = images.data?.pages[0]?.total ?? 0;
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const focus = selection.focus;
  // A filter change or an external review must not unmount a dirty editor.
  const focusImage = useQuery({
    queryKey: projectKey(pid, 'image', focus),
    queryFn: ({ signal }) =>
      request<ImageItem>('GET', `${projectPath(pid)}/images/${focus}`, undefined, { signal }),
    enabled: focus !== null,
    initialData: () => items.find((image) => image.image_id === focus),
  });
  const focused = focusImage.data ?? items.find((i) => i.image_id === focus) ?? null;
  const selectedCount =
    selection.selection.kind === 'filter' ? selection.selection.total : selection.selection.ids.size;
  const setFilter = useCallback(
    (key: string, value: string) =>
      guardRun(
        () => {
          const next = new URLSearchParams(params);
          if (value) next.set(key, value);
          else next.delete(key);
          setParams(next, { replace: true });
        },
        () => setSearch(params.get('q') ?? ''),
      ),
    [guardRun, params, setParams],
  );
  useEffect(() => {
    setSearch(filter.q ?? '');
  }, [filter.q]);
  useEffect(() => {
    if (search === (filter.q ?? '')) return;
    const timer = setTimeout(() => setFilter('q', search), 300);
    return () => clearTimeout(timer);
  }, [search, filter.q, setFilter]);
  useEffect(() => {
    if (selection.pid !== pid) selection.reset(pid);
  }, [pid, selection]);
  const updateCached = useCallback(
    (id: number, patch: Partial<ImageItem>) => {
      queryClient.setQueryData<ImageItem>(projectKey(pid, 'image', id), (image) =>
        image ? { ...image, ...patch } : image,
      );
      queryClient.setQueriesData<InfiniteData<ImagePage>>({ queryKey: projectKey(pid, 'images') }, (data) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                items: page.items.map((image) => (image.image_id === id ? { ...image, ...patch } : image)),
              })),
            }
          : data,
      );
    },
    [pid],
  );
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: projectKey(pid, 'images') });
    void queryClient.invalidateQueries({ queryKey: projectKey(pid, 'stats') });
  }, [pid]);
  const queue = useMemo(
    () =>
      new ReviewQueue({
        read: async (id) =>
          itemsRef.current.find((i) => i.image_id === id) ??
          request<ImageItem>('GET', `${projectPath(pid)}/images/${id}`),
        send: (body) => request<ReviewResult>('POST', `${projectPath(pid)}/images/review`, body),
        optimistic: (id, target) => updateCached(id, { review_status: target }),
        settled: (values) => {
          values.forEach((v) => updateCached(v.image.image_id, v.image));
          refresh();
        },
        failed: (_ids, error) => {
          setReviewError(error);
          refresh();
        },
        undo: (entry) => setUndo((old) => [...old, entry].slice(-50)),
        busy: setReviewBusy,
      }),
    [pid, updateCached, refresh],
  );
  useEffect(() => () => queue.close(), [queue]);
  const activate = useCallback(
    (id: number, event?: MouseEvent) =>
      guardRun(() => {
        selection.focusOn(id);
        if (event?.shiftKey)
          selection.range(
            id,
            items.map((i) => i.image_id),
          );
        else if (event?.ctrlKey || event?.metaKey) selection.toggle(id);
        requestAnimationFrame(() => {
          if (!document.querySelector('[role="dialog"]:not([hidden]):not([aria-modal="false"])'))
            document.querySelector<HTMLElement>(`[data-image-id="${id}"]`)?.focus();
        });
      }),
    [guardRun, selection, items],
  );
  const move = useCallback(
    (delta: number) => {
      const index = items.findIndex((i) => i.image_id === focus),
        next = items[Math.max(0, Math.min(items.length - 1, index + delta))];
      if (next) activate(next.image_id);
    },
    [items, focus, activate],
  );
  const review = useCallback(
    (target: Review) => {
      if (!canWrite() || action.busy) return;
      guardRun(() => {
        setReviewError(null);
        const selected = useSelection.getState().selection;
        if (selected.kind === 'filter') {
          void action.run(async () => {
            const result = await request<ReviewPreview>('POST', `${projectPath(pid)}/images/review/preview`, {
              query: selected.query,
            });
            setConfirmation({ preview: result, target });
          });
          return;
        }
        const ids = selected.ids.size ? [...selected.ids] : focus ? [focus] : [];
        if (!ids.length) return;
        if (ids.length > 5000) {
          setReviewError(new Error('batch'));
          return;
        }
        queue.enqueue(ids, target);
        if (ids.length === 1 && prefs.autoAdvance) move(1);
      });
    },
    [action, focus, guardRun, move, pid, prefs.autoAdvance, queue],
  );
  const undoReview = useCallback(() => {
    const entry = undo.at(-1);
    if (!entry || reviewBusy || !canWrite()) return;
    void action.run(async () => {
      const result = await request<ReviewResult>('POST', `${projectPath(pid)}/images/review`, {
        targets: entry.targets,
        expected_versions: entry.expected_versions,
        ids: Object.keys(entry.targets).map(Number),
      });
      result.items.forEach((i) => updateCached(i.image.image_id, i.image));
      setUndo((old) => advanceUndoHistory(old.slice(0, -1), entry, result.items));
      refresh();
    });
  }, [undo, reviewBusy, action, pid, updateCached, refresh]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!prefs.shortcuts || isEditingTarget(event.target) || event.altKey) return;
      if (event.defaultPrevented) return;
      if (event.target instanceof Element && event.target.closest('[data-executor-panel]')) return;
      if (document.querySelector('[role="dialog"]:not([hidden]):not([aria-modal="false"])') && !previewOpen)
        return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        undoReview();
        return;
      }
      if (event.ctrlKey || event.metaKey) return;
      const target = reviewKey(event.key);
      if (target) {
        event.preventDefault();
        review(target);
      } else if (['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        move(
          event.key === 'ArrowRight'
            ? 1
            : event.key === 'ArrowLeft'
              ? -1
              : event.key === 'ArrowDown'
                ? columns
                : -columns,
        );
      } else if (event.key.toLowerCase() === 'b' && previewOpen) {
        event.preventDefault();
        prefs.cycleStage();
      } else if (
        event.key === ' ' &&
        event.target instanceof Element &&
        event.target.closest('[role="grid"]')
      ) {
        event.preventDefault();
        if (focused) setPreviewOpen(true);
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [prefs, previewOpen, focused, review, move, undoReview, columns]);
  const fetchNextPage = images.fetchNextPage;
  const loadMore = useCallback(() => {
    void fetchNextPage();
  }, [fetchNextPage]);
  const count = (status: string) =>
    stats.data?.groups.find((g) => g.by === 'review_status')?.buckets.find((b) => b.key === status)?.count ??
    0;
  return (
    <div className={styles.workbench}>
      <div className={styles.top}>
        <div>
          <div className={ui.eyebrow}>DATA CURATION / 01</div>
          <h1>
            数据清洗工作台 <span className={styles.total}>{stats.data?.total ?? total}</span>
          </h1>
        </div>
        <div className={ui.row}>
          <span className={ui.muted}>
            保留 {count('keep')} · 剔除 {count('reject')} · 未审 {count('unreviewed')}
          </span>
          <Button disabled={!focused} onClick={() => setPreviewOpen(true)}>
            原图预览
          </Button>
          <Button
            aria-expanded={executorOpen}
            disabled={!executorOpen && filterPending}
            onClick={() => guardRun(() => setExecutorOpen(!executorOpen))}
          >
            执行器批次
          </Button>
        </div>
      </div>
      <div className={styles.filters}>
        <input
          aria-label="搜索标签"
          placeholder="搜索标签…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          aria-label="审核筛选"
          value={params.get('review') ?? ''}
          onChange={(e) => setFilter('review', e.target.value)}
        >
          <option value="">全部审核状态</option>
          <option value="unreviewed">未审</option>
          <option value="keep">保留</option>
          <option value="reject">剔除</option>
        </select>
        <select
          aria-label="来源筛选"
          value={params.get('src') ?? ''}
          onChange={(e) => setFilter('src', e.target.value)}
        >
          <option value="">全部来源</option>
          {sources.data?.map((s) => (
            <option key={s.source_id} value={s.source_id}>
              {s.name}
            </option>
          ))}
        </select>
        <select
          aria-label="评级筛选"
          value={params.get('rating') ?? ''}
          onChange={(e) => setFilter('rating', e.target.value)}
        >
          <option value="">全部评级</option>
          {['general', 'sensitive', 'questionable', 'explicit'].map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
        <label className={ui.row}>
          <input
            type="checkbox"
            checked={params.get('missing') === 'true'}
            onChange={(e) => setFilter('missing', e.target.checked ? 'true' : '')}
          />
          文件缺失
        </label>
        <select aria-label="图片排序" value={sort} onChange={(e) => setFilter('sort', e.target.value)}>
          <option value="id">最早导入</option>
          <option value="-id">最近导入</option>
        </select>
      </div>
      <div className={styles.selectionBar}>
        <div className={ui.row}>
          <span className={ui.mono}>{selectedCount} 已选</span>
          <Button
            small
            quiet
            onClick={() => {
              if (useSelection.getState().selection.kind === 'filter') selection.clear();
              items.forEach((i) => {
                const s = useSelection.getState().selection;
                if (s.kind === 'ids' && !s.ids.has(i.image_id)) selection.toggle(i.image_id);
              });
            }}
          >
            选择已加载 {items.length} 张
          </Button>
          <Button small quiet disabled={!total || filterPending} onClick={() => selection.all(filter, total)}>
            全选筛选结果 {total} 张
          </Button>
          {selectedCount > 0 && (
            <Button small quiet onClick={selection.clear}>
              清除选择
            </Button>
          )}
        </div>
        <div className={ui.row}>
          <Button small disabled={!canWrite() || selectedCount > 5000} onClick={() => review('keep')}>
            ✓ 保留 K
          </Button>
          <Button small disabled={!canWrite() || selectedCount > 5000} onClick={() => review('reject')}>
            × 剔除 X
          </Button>
          <Button small disabled={!canWrite() || selectedCount > 5000} onClick={() => review('unreviewed')}>
            ○ 未审 U
          </Button>
          <Button
            small
            quiet
            disabled={!undo.length || reviewBusy || action.busy || !canWrite()}
            onClick={undoReview}
          >
            撤销审核
          </Button>
        </div>
      </div>
      {selectedCount > 5000 && (
        <div className={ui.warning}>一次最多处理 5000 张。请缩小筛选范围；不会自动拆分审核。</div>
      )}
      <div className={styles.selectionBar}>
        <span className={ui.muted}>标签操作 · 未多选时使用当前图片</span>
        <div className={ui.row}>
          <Button
            small
            disabled={(!selectedCount && !focused) || selectedCount > 5000 || !canWrite()}
            onClick={() => openBatch('bulk')}
          >
            批量标签
          </Button>
          <Button
            small
            disabled={(!selectedCount && !focused) || selectedCount > 5000 || !canWrite()}
            onClick={() => openBatch('rule')}
          >
            替换规则
          </Button>
          <Button
            small
            disabled={(!selectedCount && !focused) || selectedCount > 5000}
            onClick={() => openBatch('stats')}
          >
            标签统计
          </Button>
        </div>
      </div>
      {selection.selection.kind === 'filter' && (
        <div className={ui.notice}>
          已选择当时筛选条件下的全部 {selection.selection.total}{' '}
          张；更改筛选不会改变这份选择。执行前将重新预览确认。不同筛选下不显示逐图选中状态。
        </div>
      )}
      <ErrorBox
        message={
          reviewError
            ? errorText(reviewError)
            : action.error
              ? errorText(action.error)
              : images.error
                ? errorText(images.error)
                : undefined
        }
      />
      <div className={styles.body}>
        {items.length ? (
          <Grid
            items={items}
            total={total}
            focus={focus}
            selection={selection.selection}
            sameFilter={
              selection.selection.kind !== 'filter' ||
              queryString(selection.selection.query) === queryString(filter)
            }
            onActivate={activate}
            onPreview={() => setPreviewOpen(true)}
            onMore={loadMore}
            hasMore={Boolean(images.hasNextPage)}
            loading={images.isFetchingNextPage}
            onColumnsChanged={setColumns}
          />
        ) : (
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Empty/loading content still needs keyboard scrolling on narrow screens.
          <div className={styles.emptyGrid} role="region" aria-label="图片列表状态" tabIndex={0}>
            <EmptyState
              title={images.isPending ? '正在读取图片' : '当前筛选下没有图片'}
              description="从来源管理导入本地图片，或调整筛选条件。"
            />
          </div>
        )}
        {focused ? (
          <div className={styles.editorSlot} inert={executorOpen || undefined}>
            <Editor key={focused.image_id} pid={pid} image={focused} />
          </div>
        ) : (
          <aside className={styles.editor}>
            <div className={ui.eyebrow}>ANNOTATION</div>
            <EmptyState
              title="选择一张图片"
              description="点击图片编辑标签。Ctrl / Cmd 多选，Shift 选择已加载范围。"
            />
          </aside>
        )}
        <FloatingPanel open={executorOpen} onClose={() => setExecutorOpen(false)} title="工作台执行器">
          <JobBatches
            pid={pid}
            embedded
            active={executorOpen}
            currentFilter={filter}
            scopePending={filterPending}
            batchId={executorBatch}
            onCreated={(id) => {
              setParams(
                (previous) => {
                  const next = new URLSearchParams(previous);
                  next.set('executor_batch', id);
                  return next;
                },
                { replace: true },
              );
            }}
          />
        </FloatingPanel>
      </div>
      <div className={styles.footer}>
        <span>{reviewBusy ? '正在提交审核…' : `已加载 ${items.length} / ${total} 张`}</span>
        {executorBatch && (
          <BatchIndicator pid={pid} id={executorBatch} onOpen={() => guardRun(() => setExecutorOpen(true))} />
        )}
        <span>本地项目 · 原始标注保留 · {prefs.shortcuts ? '快捷键已开启' : '快捷键已关闭'}</span>
      </div>
      <Preview
        image={previewOpen ? focused : null}
        onClose={() => {
          setPreviewOpen(false);
          requestAnimationFrame(() =>
            document.querySelector<HTMLElement>(`[data-image-id="${focus}"]`)?.focus(),
          );
        }}
        onMove={move}
        onReview={review}
      />
      {batch && (
        <BatchDialog pid={pid} kind={batch.kind} scope={batch.scope} onClose={() => setBatch(null)} />
      )}
      <Modal
        open={Boolean(confirmation)}
        onOpenChange={(open) => {
          if (!open && !action.busy) setConfirmation(null);
        }}
        title="确认批量审核"
        description="服务端已冻结完整筛选结果和每张图片的版本。任何成员或审核状态变化都会使本次提交失败。"
      >
        <p>
          将 {confirmation?.preview.total} 张图片设为
          {confirmation?.target === 'keep' ? '保留' : confirmation?.target === 'reject' ? '剔除' : '未审'}。
        </p>
        <ErrorBox message={action.error ? errorText(action.error) : undefined} />
        <div className={ui.actions}>
          <Button disabled={action.busy} onClick={() => setConfirmation(null)}>
            取消
          </Button>
          <Button
            primary
            disabled={action.busy || !canWrite()}
            onClick={() =>
              void action.run(async () => {
                if (!confirmation) return;
                const { preview, target } = confirmation;
                const result = await request<ReviewResult>('POST', `${projectPath(pid)}/images/review`, {
                  query: preview.query,
                  expected_total: preview.total,
                  preview_id: preview.preview_id,
                  digest: preview.digest,
                  target,
                });
                setUndo((old) => [...old, undoEntry(result.items)].slice(-50));
                setConfirmation(null);
                selection.clear();
                refresh();
              })
            }
          >
            确认审核
          </Button>
        </div>
      </Modal>
    </div>
  );
}
