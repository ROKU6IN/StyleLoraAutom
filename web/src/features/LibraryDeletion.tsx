import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { allPages, request } from '../api/client';
import { errorText } from '../api/errors';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, HoldButton, Modal, ui } from '../ui';

type Kind = 'dictionary' | 'coverage';
export type LibraryResource = {
  kind: Kind;
  resource_id: string;
  name: string;
  revision: number;
  version: string;
};
const nameOf = (kind: Kind) => (kind === 'dictionary' ? '整份词典' : '覆盖方案');
const pathOf = (kind: Kind) =>
  '/api/prompt-production/' + (kind === 'dictionary' ? 'dictionaries' : 'coverage-plans');

export function DeleteLibrary({
  resource,
  disabled,
  pending = false,
  onDeleted,
  onBusy,
}: {
  resource: LibraryResource;
  disabled: boolean;
  pending?: boolean;
  onDeleted: (id: string) => void;
  onBusy: (busy: boolean) => void;
}) {
  const [selection, setSelection] = useState<LibraryResource | null>(null),
    [busy, setBusy] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false),
    [error, setError] = useState<unknown>(null);
  const identity = useRef({ signature: '', key: '' });
  const label = nameOf(resource.kind);
  async function remove() {
    if (!selection || busy || disabled || !acknowledged || !canWrite()) return;
    const signature = JSON.stringify([selection.kind, selection.resource_id, selection.version]);
    if (identity.current.signature !== signature) identity.current = { signature, key: crypto.randomUUID() };
    setBusy(true);
    onBusy(true);
    setError(null);
    try {
      await request('DELETE', pathOf(selection.kind) + '/' + selection.resource_id, undefined, {
        version: selection.version,
        key: identity.current.key,
      });
      setSelection(null);
      onDeleted(selection.resource_id);
      void queryClient.invalidateQueries({ queryKey: ['prompt'] });
      useSession.getState().notify(label + '已删除');
    } catch (failure) {
      setError(failure);
      void queryClient.invalidateQueries({ queryKey: ['prompt'] });
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  return (
    <>
      <Button
        danger
        disabled={disabled || busy || !canWrite()}
        onClick={() => {
          setSelection({ ...resource });
          setAcknowledged(false);
          setError(null);
        }}
      >
        {pending ? '继续清理 ' + resource.name : '删除' + label}
      </Button>
      <Modal
        open={selection !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setSelection(null);
        }}
        title={'确认删除' + label}
        description="删除后无法在库中恢复，请先导出需要保留的内容。已创建任务的冻结输入、结果和已有项目不受影响。"
      >
        <p>
          {selection?.name} · r{selection?.revision}
        </p>
        <p className={ui.caption}>编号：{selection?.resource_id}</p>
        {resource.kind === 'dictionary' && (
          <p>
            此词典将不能用于新采样或接收追加。正在运行的任务仍使用冻结输入；自动回写此词典会明确报错，结果仍保留在任务中。
          </p>
        )}
        <p>
          会保留少量删除记录，阻止旧保存请求重新创建资源。若清理中断，可重试，或在“检查未完成删除”中继续。
        </p>
        <ErrorBox message={error ? errorText(error) : undefined} />
        <label className={ui.row}>
          <input
            type="checkbox"
            checked={acknowledged}
            disabled={busy}
            onChange={(e) => setAcknowledged(e.target.checked)}
          />
          我已保存需要的内容，确认删除{label}
        </label>
        <div className={ui.actions}>
          <Button disabled={busy} onClick={() => setSelection(null)}>
            取消删除
          </Button>
          <HoldButton
            disabled={busy || disabled || !acknowledged || !canWrite()}
            onConfirm={() => void remove()}
          >
            {error ? '重试删除' : '按住删除' + label}
          </HoldButton>
        </div>
      </Modal>
    </>
  );
}

export function PendingLibraryDeletions({
  kind,
  disabled,
  onDeleted,
  onBusy,
}: {
  kind: Kind;
  disabled: boolean;
  onDeleted: (id: string) => void;
  onBusy: (busy: boolean) => void;
}) {
  const [open, setOpen] = useState(false),
    [page, setPage] = useState(0);
  const pending = useQuery({
    queryKey: ['prompt', 'library-deletions', kind],
    enabled: open,
    queryFn: ({ signal }) => allPages<LibraryResource>(pathOf(kind) + '/deletions', signal),
  });
  const rows = pending.data ?? [],
    current = Math.min(page, Math.max(0, Math.ceil(rows.length / 20) - 1));
  return (
    <div className={ui.stack}>
      <Button
        disabled={disabled}
        onClick={() => {
          setOpen(true);
          if (open) void pending.refetch();
        }}
      >
        检查未完成删除
      </Button>
      {open && (
        <section aria-label="未完成的库文件删除" className={ui.stack}>
          <ErrorBox message={pending.error ? errorText(pending.error) : undefined} />
          {pending.isPending ? (
            <p>正在检查删除记录…</p>
          ) : (
            !pending.error && <p>待完成文件清理：{rows.length} 项。已删除内容不会重新出现在可用列表中。</p>
          )}
          {rows.slice(current * 20, (current + 1) * 20).map((row) => (
            <div key={row.resource_id} className={ui.between}>
              <span>
                {row.name} · r{row.revision}
              </span>
              <DeleteLibrary
                resource={row}
                disabled={disabled}
                pending
                onDeleted={onDeleted}
                onBusy={onBusy}
              />
            </div>
          ))}
          {rows.length > 20 && (
            <div className={ui.actions}>
              <Button disabled={disabled || current === 0} onClick={() => setPage(current - 1)}>
                上一页未完成删除
              </Button>
              <span>
                第 {current + 1} / {Math.ceil(rows.length / 20)} 页
              </span>
              <Button
                disabled={disabled || (current + 1) * 20 >= rows.length}
                onClick={() => setPage(current + 1)}
              >
                下一页未完成删除
              </Button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
