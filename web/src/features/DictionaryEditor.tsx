import { useMemo, useState } from 'react';
import { Button, ErrorBox, Field, Modal, ui } from '../ui';
import {
  aspectNames,
  contentText,
  dictionaryDraftIssues,
  normalizeDraftTag,
  ratingNames,
  readDictionaryContent,
  type DictionaryContent,
  type DictionaryKind,
  type EntryDraft,
  type FeatureDraft,
  type Rating,
} from './dictionaryEditorData';

const pageSize = 20;
const isEntry = (row: FeatureDraft | EntryDraft): row is EntryDraft => 'tags' in row;
const label = (row: FeatureDraft | EntryDraft) => (isEntry(row) ? row.tags.join(', ') : row.tag);

export default function DictionaryEditor({
  kind,
  content,
  onChange,
  disabled,
}: {
  kind: DictionaryKind;
  content: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const value = useMemo(() => readDictionaryContent(kind, content), [kind, content]);
  const issues = useMemo(() => dictionaryDraftIssues(kind, content), [kind, content]);
  const [dimension, setDimension] = useState<string | null>(null),
    [selected, setSelected] = useState<number | null>(0),
    [query, setQuery] = useState(''),
    [page, setPage] = useState(0),
    [error, setError] = useState(''),
    [deletion, setDeletion] = useState<'row' | 'dimension' | null>(null),
    [undo, setUndo] = useState<{ before: string; after: string } | null>(null);
  const names = value?.kind === 'features' ? Object.keys(value.dimensions) : [];
  const activeDimension = dimension !== null && names.includes(dimension) ? dimension : names[0];
  const rows =
    value?.kind === 'features' ? (value.dimensions[activeDimension] ?? []) : (value?.entries ?? []);
  const current = selected === null ? undefined : rows[selected];
  const filtered = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) =>
      normalizeDraftTag(
        [
          label(row),
          isEntry(row) ? row.natural_language : '',
          row.rating ?? '',
          row.rating ? ratingNames[row.rating] : '未知',
        ].join(' '),
      ).includes(normalizeDraftTag(query)),
    );
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize)),
    currentPage = Math.min(page, pages - 1);
  const total =
    value?.kind === 'features'
      ? Object.values(value.dimensions).reduce((sum, list) => sum + list.length, 0)
      : rows.length;
  function write(next: DictionaryContent) {
    onChange(contentText(next));
    setError('');
  }
  function replaceRows(next: (FeatureDraft | EntryDraft)[]) {
    if (!value) return;
    write(
      value.kind === 'prompts'
        ? { ...value, entries: next as EntryDraft[] }
        : { ...value, dimensions: { ...value.dimensions, [activeDimension]: next as FeatureDraft[] } },
    );
  }
  function update(row: FeatureDraft | EntryDraft) {
    replaceRows(rows.map((old, index) => (index === selected ? row : old)));
  }
  function add(copy = false) {
    const row =
      copy && current
        ? structuredClone(current)
        : kind === 'prompts'
          ? { tags: [''], natural_language: null, aspect: null, rating: null }
          : { tag: '', rating: null };
    replaceRows([...rows, row]);
    setSelected(rows.length);
    setQuery('');
    setPage(Math.floor(rows.length / pageSize));
  }
  function move(offset: number) {
    if (selected === null) return;
    const next = [...rows],
      target = selected + offset;
    [next[selected], next[target]] = [next[target], next[selected]];
    replaceRows(next);
    setSelected(target);
    setPage(Math.floor(target / pageSize));
  }
  function remove() {
    if (!value) return;
    let next: DictionaryContent;
    if (deletion === 'dimension' && value.kind === 'features') {
      next = {
        ...value,
        dimensions: Object.fromEntries(
          Object.entries(value.dimensions).filter(([name]) => name !== activeDimension),
        ),
      };
      setDimension(null);
    } else {
      const remaining = rows.filter((_, index) => index !== selected);
      next =
        value.kind === 'prompts'
          ? { ...value, entries: remaining as EntryDraft[] }
          : { ...value, dimensions: { ...value.dimensions, [activeDimension]: remaining as FeatureDraft[] } };
    }
    setUndo({ before: content, after: contentText(next) });
    write(next);
    setSelected(null);
    setDeletion(null);
  }
  return (
    <div className={ui.stack}>
      <h3>{kind === 'features' ? '特征维度与候选' : '提示词条目编辑'}</h3>
      <p className={ui.caption}>
        修改先保留在草稿，点击“保存词典”才写入本地。未填写评级表示未知；不会自动判断内容等级。
      </p>
      <ErrorBox message={error} />
      {!!issues.length && (
        <div role="status" className={ui.warning}>
          <p>保存前请修正以下内容（最多显示 20 项）：</p>
          <ul>
            {issues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </div>
      )}
      {value && (
        <>
          {value.kind === 'features' && (
            <>
              <div className={ui.formGrid}>
                <Field id="dict-dimension" label="当前特征维度">
                  <select
                    id="dict-dimension"
                    value={activeDimension ?? ''}
                    disabled={disabled || !names.length}
                    onChange={(e) => {
                      setDimension(e.target.value);
                      setSelected(0);
                      setQuery('');
                      setPage(0);
                      setError('');
                    }}
                  >
                    {!names.length && <option value="">尚无维度</option>}
                    {names.map((name) => (
                      <option value={name} key={name}>
                        {name || '（未命名维度）'} · {value.dimensions[name].length} 条
                      </option>
                    ))}
                  </select>
                </Field>
                {activeDimension !== undefined && (
                  <Field id="dict-dimension-name" label="维度名称">
                    <input
                      id="dict-dimension-name"
                      value={activeDimension}
                      disabled={disabled}
                      maxLength={64}
                      onChange={(e) => {
                        const name = e.target.value;
                        if (names.some((old) => old !== activeDimension && old.trim() === name.trim())) {
                          setError('已有同名维度，未合并或覆盖；请使用其他名称。');
                          return;
                        }
                        write({
                          ...value,
                          dimensions: Object.fromEntries(
                            Object.entries(value.dimensions).map(([key, list]) => [
                              key === activeDimension ? name : key,
                              list,
                            ]),
                          ),
                        });
                        setDimension(name);
                      }}
                    />
                  </Field>
                )}
              </div>
              <div className={ui.actions}>
                <Button
                  disabled={disabled || names.length >= 20 || total >= 10000}
                  onClick={() => {
                    let number = 1;
                    while (names.includes('dimension_' + number)) number++;
                    const name = 'dimension_' + number;
                    write({
                      ...value,
                      dimensions: { ...value.dimensions, [name]: [{ tag: '', rating: null }] },
                    });
                    setDimension(name);
                    setSelected(0);
                    setQuery('');
                    setPage(0);
                  }}
                >
                  新增维度
                </Button>
                <Button
                  danger
                  disabled={disabled || activeDimension === undefined}
                  onClick={() => setDeletion('dimension')}
                >
                  删除当前维度
                </Button>
              </div>
            </>
          )}
          <Field
            id="dict-search"
            label="检索当前条目"
            hint="检索标签、描述或评级；仅影响列表显示，保存仍包含全部条目。"
          >
            <input
              id="dict-search"
              type="search"
              value={query}
              disabled={disabled}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(0);
              }}
            />
          </Field>
          <div className={ui.actions}>
            <Button
              disabled={disabled || total >= 10000 || (kind === 'features' && activeDimension === undefined)}
              onClick={() => add()}
            >
              {kind === 'features' ? '新增候选标签' : '新增提示词条目'}
            </Button>
            <span className={ui.caption}>
              词典共 {total} 条；当前匹配 {filtered.length} / {rows.length} 条。第 {currentPage + 1} / {pages}{' '}
              页
            </span>
            <Button disabled={disabled || currentPage === 0} onClick={() => setPage(currentPage - 1)}>
              条目上一页
            </Button>
            <Button disabled={disabled || currentPage + 1 === pages} onClick={() => setPage(currentPage + 1)}>
              条目下一页
            </Button>
          </div>
          <div className={ui.tableWrap}>
            <table className={ui.table} aria-label="词典条目列表">
              <thead>
                <tr>
                  <th>条目</th>
                  <th>内容</th>
                  <th>评级</th>
                  <th>编辑</th>
                </tr>
              </thead>
              <tbody>
                {filtered
                  .slice(currentPage * pageSize, (currentPage + 1) * pageSize)
                  .map(({ row, index }) => (
                    <tr key={index}>
                      <td>
                        {index + 1}
                        {selected === index ? ' · 当前' : ''}
                      </td>
                      <td>
                        {label(row).slice(0, 160) || '（待填写）'}
                        {label(row).length > 160 ? '…' : ''}
                      </td>
                      <td>{row.rating ? ratingNames[row.rating] : '未知'}</td>
                      <td>
                        <Button
                          small
                          disabled={disabled}
                          aria-pressed={selected === index}
                          onClick={() => setSelected(index)}
                        >
                          编辑第 {index + 1} 条
                        </Button>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {!filtered.length && <p>没有匹配条目；可清空检索或新增内容。</p>}
          {current && (
            <fieldset disabled={disabled} className={ui.stack}>
              <legend>
                编辑第 {selected! + 1} 条{kind === 'features' ? ` · ${activeDimension}` : ''}
              </legend>
              {isEntry(current) ? (
                <>
                  <Field
                    id="dict-entry-tags"
                    label="有序标签（每行一个）"
                    hint="保留输入顺序；保存时规范化大小写、下划线及重复标签。空行需删除。"
                  >
                    <textarea
                      id="dict-entry-tags"
                      rows={5}
                      value={current.tags.join('\n')}
                      onChange={(e) => update({ ...current, tags: e.target.value.split('\n') })}
                    />
                  </Field>
                  <Field id="dict-entry-nl" label="条目自然语言描述">
                    <textarea
                      id="dict-entry-nl"
                      rows={3}
                      maxLength={4096}
                      value={current.natural_language ?? ''}
                      onChange={(e) => update({ ...current, natural_language: e.target.value || null })}
                    />
                  </Field>
                  <Field id="dict-entry-aspect" label="条目图幅">
                    <select
                      id="dict-entry-aspect"
                      value={current.aspect ?? ''}
                      onChange={(e) =>
                        update({ ...current, aspect: (e.target.value || null) as EntryDraft['aspect'] })
                      }
                    >
                      <option value="">未指定</option>
                      {Object.entries(aspectNames).map(([value, name]) => (
                        <option key={value} value={value}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </Field>
                </>
              ) : (
                <Field
                  id="dict-feature-tag"
                  label="候选标签"
                  hint="一个候选填写一个纯标签；不能用逗号串联多个标签或加入权重。"
                >
                  <input
                    id="dict-feature-tag"
                    maxLength={200}
                    value={current.tag}
                    onChange={(e) => update({ ...current, tag: e.target.value })}
                  />
                </Field>
              )}
              <Field id="dict-entry-rating" label="条目评级">
                <select
                  id="dict-entry-rating"
                  value={current.rating ?? ''}
                  onChange={(e) => update({ ...current, rating: (e.target.value || null) as Rating | null })}
                >
                  <option value="">未知</option>
                  {Object.entries(ratingNames).map(([value, name]) => (
                    <option key={value} value={value}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
              <div className={ui.actions}>
                <Button disabled={total >= 10000} onClick={() => add(true)}>
                  复制当前条目
                </Button>
                <Button disabled={!!query.trim() || selected === 0} onClick={() => move(-1)}>
                  条目上移
                </Button>
                <Button disabled={!!query.trim() || selected === rows.length - 1} onClick={() => move(1)}>
                  条目下移
                </Button>
                <Button danger onClick={() => setDeletion('row')}>
                  删除当前条目
                </Button>
              </div>
              {!!query.trim() && <p className={ui.caption}>清空检索后可调整原始顺序。</p>}
            </fieldset>
          )}
        </>
      )}
      {undo?.after === content && (
        <Button
          disabled={disabled}
          onClick={() => {
            onChange(undo.before);
            setUndo(null);
            setSelected(0);
          }}
        >
          撤销最近一次删除
        </Button>
      )}
      <details open={!value}>
        <summary>高级 JSON 编辑</summary>
        <Field
          id="prompt-content"
          label={kind === 'features' ? '特征维度（JSON）' : '提示词条目（JSON）'}
          hint="与表单共用同一份草稿。结构错误或未知字段会保留原文，请修正后继续表单编辑。"
        >
          <textarea
            id="prompt-content"
            rows={10}
            value={content}
            disabled={disabled}
            onChange={(e) => {
              onChange(e.target.value);
              setSelected(null);
              setError('');
            }}
          />
        </Field>
      </details>
      <Modal
        open={deletion !== null}
        onOpenChange={(open) => {
          if (!open) setDeletion(null);
        }}
        title={deletion === 'dimension' ? '删除这个维度？' : '删除这个条目？'}
        description={
          deletion === 'dimension'
            ? `将从草稿中移除「${activeDimension}」及其中 ${rows.length} 条候选。保存词典后才写入本地。`
            : `将从草稿中移除第 ${selected === null ? '' : selected + 1} 条。保存词典后才写入本地。`
        }
      >
        <div className={ui.actions}>
          <Button onClick={() => setDeletion(null)}>保留内容</Button>
          <Button danger disabled={disabled} onClick={remove}>
            确认从草稿删除
          </Button>
        </div>
      </Modal>
    </div>
  );
}
