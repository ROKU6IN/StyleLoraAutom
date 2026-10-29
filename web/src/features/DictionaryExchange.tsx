import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import type { Page } from '../api/types';
import { errorText } from '../api/errors';
import { Button, ErrorBox, Modal, ui } from '../ui';

const P = '/api/prompt-production';
type Action = (work: () => Promise<void>) => Promise<void>;
type ImportValue = {
  dimension?: string;
  tag?: string;
  tags?: string[];
  natural_language?: string | null;
  rating?: string | null;
};
export type ImportPreview = {
  preview_id: string;
  digest: string;
  valid: boolean;
  count: number;
  issue_count: number;
  issues: { code: string; row?: number; severity: string }[];
  examples: ImportValue[];
  row_count: number;
  selectable_count: number;
  selection_available: boolean;
  selection: { rows: number[]; excluded_count: number } | null;
};
type ImportRow = {
  row: number;
  original: string | null;
  value: ImportValue | null;
  issues: { code: string }[];
};

export function DictionaryDownload({
  base,
  disabled,
  busy,
  action,
}: {
  base: {
    dictionary_id: string;
    name: string;
    version: string;
    revision: number;
    kind: 'features' | 'prompts';
  };
  disabled: boolean;
  busy: boolean;
  action: Action;
}) {
  const [open, setOpen] = useState(false),
    [ack, setAck] = useState(false),
    [failure, setFailure] = useState<unknown>(null);
  const features = base.kind === 'features';
  const save = (format: 'json' | 'csv' | 'txt') =>
    action(async () => {
      setFailure(null);
      try {
        const blob = await request<Blob>(
          'GET',
          `${P}/dictionaries/${base.dictionary_id}/export?format=${format}${format === 'txt' ? '&acknowledge_lossy=true' : ''}`,
          undefined,
          { version: base.version, responseType: 'blob' },
        );
        const url = URL.createObjectURL(blob),
          link = document.createElement('a');
        link.href = url;
        link.download = `dictionary-${base.dictionary_id}.${format === 'txt' && features ? 'zip' : format}`;
        link.click();
        URL.revokeObjectURL(url);
        setOpen(false);
        setAck(false);
      } catch (error) {
        setFailure(error);
        throw error;
      }
    });
  return (
    <>
      <Button disabled={disabled} onClick={() => void save('json')}>
        导出词典 JSON
      </Button>
      <Button disabled={disabled} onClick={() => void save('csv')}>
        导出词典 CSV
      </Button>
      <Button
        disabled={disabled}
        onClick={() => {
          setAck(false);
          setFailure(null);
          setOpen(true);
        }}
      >
        导出词典 TXT
      </Button>
      <Modal
        open={open}
        onOpenChange={(value) => {
          if (!busy) {
            setOpen(value);
            setAck(false);
          }
        }}
        title="导出词典 TXT"
        description={`${base.name} · 已保存版本 ${base.revision}`}
      >
        <ErrorBox message={failure ? errorText(failure) : undefined} />
        <p>
          {features
            ? '每个维度一个 UTF-8 TXT，保留标签与评级。下载 ZIP 包，dimensions.json 对照表记录编号文件对应的维度；解压后逐个 TXT 导入，并按对照表指定维度。'
            : '每行一个提示词，标签后接自然语言。图幅、评级、标签与描述的结构及描述内部换行会丢失，重新导入时需本地标签库识别。'}
        </p>
        <p className={ui.warning}>
          {features ? 'TXT 不保留词典名称、编号和版本。' : 'TXT 不能替代完整词典备份。'} JSON
          保留完整词典；CSV 保留条目结构、维度和评级。
        </p>
        <label className={ui.row}>
          <input
            type="checkbox"
            checked={ack}
            disabled={busy}
            onChange={(event) => setAck(event.target.checked)}
          />
          确认接受词典 TXT 导出的信息损失
        </label>
        <div className={ui.actions}>
          <Button primary disabled={disabled || !ack} onClick={() => void save('txt')}>
            {features ? '下载维度 TXT 压缩包' : '下载提示词 TXT'}
          </Button>
          <Button
            disabled={busy}
            onClick={() => {
              setOpen(false);
              setAck(false);
            }}
          >
            取消
          </Button>
        </div>
      </Modal>
    </>
  );
}

export function ImportRows({
  preview,
  disabled,
  action,
  onPreview,
}: {
  preview: ImportPreview;
  disabled: boolean;
  action: Action;
  onPreview: (value: ImportPreview) => void;
}) {
  const [cursor, setCursor] = useState<string | null>(null),
    [history, setHistory] = useState<(string | null)[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const identity = useRef({ signature: '', key: '' });
  const rows = useQuery({
    queryKey: ['prompt', 'import-rows', preview.preview_id, preview.digest, cursor],
    queryFn: ({ signal }) =>
      request<Page<ImportRow>>(
        'GET',
        `${P}/imports/${preview.preview_id}/rows?digest=${preview.digest}&limit=50${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
        undefined,
        { signal },
      ),
  });
  const valid = (row: ImportRow) => row.value !== null && row.issues.length === 0;
  const change = (number: number, checked: boolean) =>
    setSelected((before) =>
      checked ? [...new Set([...before, number])] : before.filter((n) => n !== number),
    );
  return (
    <details>
      <summary>选择有效行并重新预览</summary>
      <p>
        共 {preview.row_count} 条记录，{preview.selectable_count} 条可选择；已选 {selected.length}{' '}
        条。未选记录不会追加。CSV 编号按记录计（表头为 1），JSON 按条目顺序计，TXT 按原文行计。
      </p>
      <ErrorBox message={rows.error ? errorText(rows.error) : undefined} />
      <div className={ui.actions}>
        <Button
          disabled={disabled || !rows.data}
          onClick={() =>
            setSelected((before) => [
              ...new Set([...before, ...rows.data!.items.filter(valid).map((row) => row.row)]),
            ])
          }
        >
          选择本页有效行
        </Button>
        <Button disabled={disabled || !selected.length} onClick={() => setSelected([])}>
          清空行选择
        </Button>
      </div>
      <div className={ui.tableWrap}>
        <table className={ui.table}>
          <caption>冻结的导入记录；错误行不能选择</caption>
          <thead>
            <tr>
              <th>选择</th>
              <th>记录</th>
              <th>内容</th>
              <th>诊断</th>
            </tr>
          </thead>
          <tbody>
            {rows.data?.items.map((row) => (
              <tr key={row.row}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`选择导入记录 ${row.row}`}
                    disabled={disabled || !valid(row)}
                    checked={selected.includes(row.row)}
                    onChange={(event) => change(row.row, event.target.checked)}
                  />
                </td>
                <td>{row.row}</td>
                <td>
                  {row.value ? (
                    <>
                      {row.value.dimension && row.value.dimension + ': '}
                      {row.value.tag ?? row.value.tags?.join(', ')}
                      {row.value.natural_language && ' · ' + row.value.natural_language} ·{' '}
                      {row.value.rating ?? '评级未知'}
                    </>
                  ) : (
                    (row.original ?? '格式错误，请修正原文件')
                  )}
                </td>
                <td>{row.issues.length ? row.issues.map((issue) => issue.code).join('、') : '可选择'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={ui.actions}>
        <Button
          disabled={disabled || !history.length}
          onClick={() => {
            setCursor(history[history.length - 1]);
            setHistory(history.slice(0, -1));
          }}
        >
          上一页导入记录
        </Button>
        <span>第 {history.length + 1} 页</span>
        <Button
          disabled={disabled || !rows.data?.next_cursor}
          onClick={() => {
            setHistory([...history, cursor]);
            setCursor(rows.data!.next_cursor);
          }}
        >
          下一页导入记录
        </Button>
        <Button
          disabled={disabled || !selected.length || !!rows.error || rows.isFetching}
          onClick={() =>
            void action(async () => {
              const numbers = selected.slice().sort((a, b) => a - b),
                signature = JSON.stringify([preview.preview_id, preview.digest, numbers]);
              if (identity.current.signature !== signature)
                identity.current = { signature, key: crypto.randomUUID() };
              onPreview(
                await request<ImportPreview>(
                  'POST',
                  P + '/imports/select',
                  { preview_id: preview.preview_id, digest: preview.digest, rows: numbers },
                  { key: identity.current.key },
                ),
              );
            })
          }
        >
          生成所选行新预览
        </Button>
      </div>
    </details>
  );
}
