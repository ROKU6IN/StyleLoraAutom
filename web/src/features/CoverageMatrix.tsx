import { useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { errorText } from '../api/errors';
import { Button, ErrorBox, Field, ui } from '../ui';
import { aspectNames, ratingNames, type EntryDraft, type Rating } from './dictionaryEditorData';
import styles from './coverageMatrix.module.css';

export type Matrix = {
  enabled: boolean;
  primary_dimensions: string[];
  cell_values: Record<string, string[]>;
};
export type CoverageViewRef = { preview_id: string; digest: string; expires_at: string };
export type MatrixSource =
  | ({ kind: 'preview' } & CoverageViewRef)
  | { kind: 'task'; task_id: string; revision: number };
type Counts = { cell_targets: Record<string, number>; cell_counts: Record<string, number> };
type Detail = { index: number; entry: EntryDraft; cell: string; rating: Rating | null; verified: boolean };
type Page = { items: Detail[]; total: number; next_cursor: string | null };
const P = '/api/prompt-production';
const axisPageSize = 10;
export const matrixCell = (dimensions: string[], values: Record<string, string>) =>
  JSON.stringify(dimensions.map((name) => [name, values[name]]));
export function detailPath(source: MatrixSource, cell: string, cursor: string | null) {
  const params = new URLSearchParams({ cell, limit: '50' });
  if (cursor) params.set('cursor', cursor);
  if (source.kind === 'preview') {
    params.set('digest', source.digest);
    return `${P}/coverage/previews/${source.preview_id}/entries?${params}`;
  }
  params.set('expected_revision', String(source.revision));
  return `${P}/tasks/${source.task_id}/coverage/entries?${params}`;
}
function CellEntries({ source, cell, title }: { source: MatrixSource; cell: string; title: string }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1];
  const rows = useQuery({
    queryKey: ['prompt', 'coverage-entries', source, cell, cursor],
    queryFn: ({ signal }) => request<Page>('GET', detailPath(source, cell, cursor), undefined, { signal }),
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  return (
    <section aria-label="格子已选条目" className={ui.stack}>
      <h4>{title}</h4>
      <p>这里显示本次报告中已选入的条目，与格子的实际数量一致。待检查、失败和未入选候选不在此列表。</p>
      <ErrorBox message={rows.error ? errorText(rows.error) : undefined} />
      {rows.error && <p>预览失效时请重新预览；任务已变化时请刷新任务报告，不能继续沿用旧页。</p>}
      {rows.isFetching && <p role="status">正在读取格子明细…</p>}
      {rows.data && !rows.error && (
        <>
          <p role="status">
            本格共 {rows.data.total} 条 · 明细第 {cursors.length} 页
          </p>
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <caption>本格已选提示词</caption>
              <thead>
                <tr>
                  <th>结果序号</th>
                  <th>标签</th>
                  <th>描述</th>
                  <th>图幅</th>
                  <th>评级</th>
                </tr>
              </thead>
              <tbody>
                {rows.data.items.map((row) => (
                  <tr key={row.index}>
                    <td>{row.index + 1}</td>
                    <td>{row.entry.tags.join(', ')}</td>
                    <td>{row.entry.natural_language ?? '—'}</td>
                    <td>{row.entry.aspect ? aspectNames[row.entry.aspect] : '未指定'}</td>
                    <td>
                      {row.rating ? ratingNames[row.rating] : '未知'}
                      {!row.verified ? ' · 未验证' : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!rows.data.total && <p>本格尚未选入条目。</p>}
          <div className={ui.actions}>
            <Button
              disabled={rows.isFetching || cursors.length === 1}
              onClick={() => setCursors((old) => old.slice(0, -1))}
            >
              格子明细上一页
            </Button>
            <Button
              disabled={rows.isFetching || !rows.data.next_cursor}
              onClick={() => setCursors((old) => [...old, rows.data!.next_cursor])}
            >
              格子明细下一页
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

export default function CoverageMatrix({
  matrix,
  counts,
  source,
}: {
  matrix: Matrix;
  counts: Counts;
  source?: MatrixSource;
}) {
  const id = useId();
  const names = matrix.primary_dimensions;
  const [rowAxis, setRowAxis] = useState(names[0]),
    [columnAxis, setColumnAxis] = useState(names[1]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [rowPage, setRowPage] = useState(0),
    [columnPage, setColumnPage] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [released, setReleased] = useState(false),
    [releasing, setReleasing] = useState(false),
    [error, setError] = useState<unknown>(null);
  const otherAxis = names.find((name) => name !== rowAxis && name !== columnAxis);
  const filterValue = otherAxis
    ? Object.hasOwn(filters, otherAxis)
      ? filters[otherAxis]
      : matrix.cell_values[otherAxis][0]
    : '';
  const rowValues = matrix.cell_values[rowAxis] ?? [],
    columnValues = matrix.cell_values[columnAxis] ?? [];
  function axes(row: string, column: string) {
    setRowAxis(row);
    setColumnAxis(column);
    setRowPage(0);
    setColumnPage(0);
    setSelected(null);
  }
  function cellButton(key: string, title: string) {
    const included = Object.hasOwn(counts.cell_targets, key);
    const target = counts.cell_targets[key] ?? 0,
      actual = counts.cell_counts[key] ?? 0;
    const deficit = Math.max(0, target - actual),
      excess = Math.max(0, actual - target);
    const status = !included
      ? '已排除'
      : deficit
        ? `缺口 ${deficit}`
        : excess
          ? `超额 ${excess}`
          : '满足格子配额';
    return (
      <button
        type="button"
        className={`${styles.cell} ${!included ? styles.excluded : deficit || excess ? styles.deficit : styles.satisfied}`}
        disabled={!included || released || releasing}
        aria-pressed={selected === key}
        aria-label={`${title}：${included ? `目标 ${target}，实际 ${actual}，` : ''}${status}`}
        onClick={() => setSelected(key)}
      >
        {included && (
          <span>
            目标 {target} / 实际 {actual}
          </span>
        )}
        <strong>{status}</strong>
      </button>
    );
  }
  const title =
    selected === 'random'
      ? '随机池'
      : selected
        ? (JSON.parse(selected) as string[][]).map(([name, tag]) => `${name}: ${tag}`).join(' · ')
        : '';
  return (
    <section className={ui.stack} aria-label="覆盖矩阵">
      <h3>{matrix.enabled ? '覆盖矩阵' : '随机池覆盖'}</h3>
      <p className={ui.muted}>
        每格显示目标 / 实际数量，缺口单独标明。Tab 聚焦格子，Enter
        或空格打开明细。格子满足不代表整体比例约束也满足。
      </p>
      {matrix.enabled ? (
        <>
          <div className={ui.formGrid}>
            <Field id={id + '-rows'} label="矩阵行维度">
              <select
                id={id + '-rows'}
                value={rowAxis}
                onChange={(e) => {
                  const next = e.target.value;
                  axes(next, next === columnAxis ? rowAxis : columnAxis);
                }}
              >
                {names.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </Field>
            <Field id={id + '-columns'} label="矩阵列维度">
              <select
                id={id + '-columns'}
                value={columnAxis}
                onChange={(e) => {
                  const next = e.target.value;
                  axes(next === rowAxis ? columnAxis : rowAxis, next);
                }}
              >
                {names.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </Field>
            {otherAxis && (
              <Field id={id + '-filter'} label={`筛选维度 ${otherAxis}`}>
                <select
                  id={id + '-filter'}
                  value={filterValue}
                  onChange={(e) => {
                    setFilters((old) => ({ ...old, [otherAxis]: e.target.value }));
                    setSelected(null);
                  }}
                >
                  {matrix.cell_values[otherAxis].map((tag) => (
                    <option key={tag}>{tag}</option>
                  ))}
                </select>
              </Field>
            )}
          </div>
          <div className={ui.actions}>
            <span>
              行 {rowPage * axisPageSize + 1}–{Math.min((rowPage + 1) * axisPageSize, rowValues.length)} /{' '}
              {rowValues.length}；列 {columnPage * axisPageSize + 1}–
              {Math.min((columnPage + 1) * axisPageSize, columnValues.length)} / {columnValues.length}
            </span>
            <Button disabled={rowPage === 0} onClick={() => setRowPage(rowPage - 1)}>
              矩阵上一组行
            </Button>
            <Button
              disabled={(rowPage + 1) * axisPageSize >= rowValues.length}
              onClick={() => setRowPage(rowPage + 1)}
            >
              矩阵下一组行
            </Button>
            <Button disabled={columnPage === 0} onClick={() => setColumnPage(columnPage - 1)}>
              矩阵上一组列
            </Button>
            <Button
              disabled={(columnPage + 1) * axisPageSize >= columnValues.length}
              onClick={() => setColumnPage(columnPage + 1)}
            >
              矩阵下一组列
            </Button>
          </div>
          <div className={ui.tableWrap}>
            <table className={`${ui.table} ${styles.matrix}`}>
              <caption>
                {rowAxis} × {columnAxis}
                {otherAxis ? ` · ${otherAxis}: ${filterValue}` : ''}
              </caption>
              <thead>
                <tr>
                  <th scope="col">
                    {rowAxis} / {columnAxis}
                  </th>
                  {columnValues
                    .slice(columnPage * axisPageSize, (columnPage + 1) * axisPageSize)
                    .map((tag) => (
                      <th key={tag} scope="col">
                        {tag}
                      </th>
                    ))}
                </tr>
              </thead>
              <tbody>
                {rowValues.slice(rowPage * axisPageSize, (rowPage + 1) * axisPageSize).map((row) => (
                  <tr key={row}>
                    <th scope="row">{row}</th>
                    {columnValues
                      .slice(columnPage * axisPageSize, (columnPage + 1) * axisPageSize)
                      .map((column) => {
                        const values = {
                          [rowAxis]: row,
                          [columnAxis]: column,
                          ...(otherAxis ? { [otherAxis]: filterValue } : {}),
                        };
                        const key = matrixCell(names, values),
                          label = names.map((name) => `${name}: ${values[name]}`).join(' · ');
                        return <td key={column}>{cellButton(key, label)}</td>;
                      })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        cellButton('random', '随机池')
      )}
      {source?.kind === 'preview' && (
        <div className={ui.actions}>
          <span className={ui.muted}>
            格子明细保留 15 分钟，最多保留最近 3 次预览；释放不改变报告或任务。
          </span>
          <Button
            disabled={released || releasing}
            onClick={() => {
              setReleasing(true);
              setError(null);
              setSelected(null);
              void request('DELETE', `${P}/coverage/previews/${source.preview_id}?digest=${source.digest}`)
                .then(() => setReleased(true))
                .catch(setError)
                .finally(() => setReleasing(false));
            }}
          >
            释放本次格子明细
          </Button>
        </div>
      )}
      <ErrorBox message={error ? errorText(error) : undefined} />
      {released && <p role="status">格子明细已释放；报告仍可查看，需要明细时请重新预览。</p>}
      {selected && !released && (
        <>
          <Button onClick={() => setSelected(null)}>收起格子明细</Button>
          {source ? (
            <CellEntries
              key={JSON.stringify([source, selected])}
              source={source}
              cell={selected}
              title={title}
            />
          ) : (
            <p>此报告仅展示数量。保存后可到任务报告查看完整格子明细。</p>
          )}
        </>
      )}
    </section>
  );
}
