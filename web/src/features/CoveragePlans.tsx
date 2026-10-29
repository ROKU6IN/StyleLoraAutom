import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { allPages, request } from '../api/client';
import { errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, Field, ui } from '../ui';
import CoverageFormEditor from './CoverageFormEditor';
import { DeleteLibrary, PendingLibraryDeletions } from './LibraryDeletion';
import type { CoverageForm, EditorView } from './coverageEditorData';
import {
  coverageApplication,
  coveragePath as P,
  initialTemplate,
  type CoverageView,
  type SavedCoverage,
} from './coveragePlanData';

function message(error: unknown) {
  return error instanceof Error && !('code' in error) ? error.message : errorText(error);
}
function usePlans() {
  return useQuery({
    queryKey: ['prompt', 'coverage-plans'],
    queryFn: ({ signal }) => allPages<SavedCoverage>(P, signal),
  });
}
function Summary({ value }: { value: CoverageView }) {
  return (
    <p role="status">
      {value.name} · {value.primary_dimensions.join(' × ')} · {value.cell_count} 格 · 目标{' '}
      {value.target_count} 条 · 种子 {value.random_seed ?? '自动生成'}
    </p>
  );
}

export function CoveragePlanPicker({
  disabled,
  onApply,
  onBusy,
}: {
  disabled: boolean;
  onApply: (value: ReturnType<typeof coverageApplication>) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const plans = usePlans();
  const [selected, setSelected] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  return (
    <div className={ui.stack}>
      <div className={ui.row}>
        <Field id="saved-coverage" label="已保存的覆盖方案">
          <select
            id="saved-coverage"
            value={selected}
            disabled={disabled || busy}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="">选择本地方案</option>
            {plans.data?.map((row) => (
              <option key={row.coverage_id} value={row.coverage_id}>
                {row.name} · r{row.revision}
              </option>
            ))}
          </select>
        </Field>
        <Button
          disabled={disabled || busy || !selected}
          onClick={() => {
            setBusy(true);
            onBusy?.(true);
            setError(null);
            void request<SavedCoverage>('GET', P + '/' + selected)
              .then((value) => {
                onApply(coverageApplication(value));
                useSession.getState().notify('已载入覆盖规则、数量和种子，请重新预览采样');
              })
              .catch(setError)
              .finally(() => {
                setBusy(false);
                onBusy?.(false);
              });
          }}
        >
          载入并替换覆盖设置
        </Button>
        <Link to="/coverage-plans">管理覆盖方案</Link>
      </div>
      {!!(error || plans.error) && <ErrorBox message={message(error || plans.error)} />}
    </div>
  );
}

export default function CoveragePlans() {
  useSession((s) => s.connection);
  const plans = usePlans();
  const { register, run } = useDraftGuard();
  const [base, setBase] = useState<SavedCoverage | null>(null),
    [newId, setNewId] = useState(() => crypto.randomUUID());
  const [content, setContent] = useState(initialTemplate),
    [format, setFormat] = useState<'json' | 'yaml'>('json');
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const [preview, setPreview] = useState<{ signature: string; value: CoverageView } | null>(null);
  const [form, setForm] = useState<CoverageForm | null>(null),
    [formBase, setFormBase] = useState('');
  const [formPreview, setFormPreview] = useState<{ signature: string; value: EditorView } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const saveIdentity = useRef({ signature: '', key: '' });
  const baseText = base?.template ?? initialTemplate;
  const formSignature = JSON.stringify(form);
  const dirty = content !== baseText || format !== 'json' || (form !== null && formSignature !== formBase);
  const signature = JSON.stringify([format, content]);
  const apply = useCallback((value: SavedCoverage) => {
    setBase(value);
    setContent(value.template);
    setFormat('json');
    setPreview(null);
    setForm(null);
    setFormPreview(null);
    setError(null);
  }, []);
  const reset = useCallback(() => {
    setContent(baseText);
    setFormat('json');
    setPreview(null);
    setForm(null);
    setFormPreview(null);
  }, [baseText]);
  const save = useCallback(async () => {
    if (busy || !canWrite()) return false;
    setBusy(true);
    setError(null);
    try {
      const identifier = base?.coverage_id ?? newId;
      const edited = form ? await request<EditorView>('POST', P + '/editor/preview', form) : null;
      const document = edited ? { format: 'json', content: edited.template } : { format, content };
      const sig = JSON.stringify([identifier, base?.version, document]);
      if (saveIdentity.current.signature !== sig)
        saveIdentity.current = { signature: sig, key: crypto.randomUUID() };
      await request(
        base ? 'PUT' : 'POST',
        P + (base ? '/' + identifier : ''),
        base ? document : { ...document, coverage_id: identifier },
        { version: base?.version, key: saveIdentity.current.key },
      );
      const next = await request<SavedCoverage>('GET', P + '/' + identifier);
      // Another editor may have saved between our write and this read. The form
      // must reflect the same revision that supplies the next If-Match token.
      const reopened =
        edited && next.template !== edited.template
          ? await request<EditorView>('POST', P + '/editor', { format: 'json', content: next.template })
          : edited;
      apply(next);
      if (reopened) {
        setForm(reopened.form);
        setFormBase(JSON.stringify(reopened.form));
        setFormPreview({ signature: JSON.stringify(reopened.form), value: reopened });
      }
      void queryClient.invalidateQueries({ queryKey: ['prompt', 'coverage-plans'] });
      useSession.getState().notify('覆盖方案已保存到本地');
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [apply, base, busy, content, format, form, newId]);
  useEffect(() => {
    register({ dirty, label: '覆盖方案', save, discard: reset });
    return () => register(null);
  }, [dirty, register, reset, save]);
  const action = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const open = (identifier: string) =>
    run(() => void action(async () => apply(await request<SavedCoverage>('GET', P + '/' + identifier))));
  const deleted = (identifier: string) => {
    if (base?.coverage_id !== identifier) return;
    setBase(null);
    setNewId(crypto.randomUUID());
    setContent(initialTemplate);
    setFormat('json');
    setPreview(null);
    setForm(null);
    setFormPreview(null);
    setError(null);
  };
  return (
    <div className={ui.stack}>
      <header className={ui.between}>
        <div>
          <h1>覆盖方案</h1>
          <p className={ui.muted}>
            保存可复用的格子、比例约束、目标数量和随机种子。方案与项目独立；修改方案不会改变已创建的任务。
          </p>
        </div>
        <Link to="/prompt-production">返回提示词生产</Link>
      </header>
      {!!(error || plans.error) && <ErrorBox message={message(error || plans.error)} />}
      <section className={`${ui.section} ${ui.stack}`} aria-label="本地覆盖方案">
        <h2>本地方案</h2>
        <PendingLibraryDeletions
          kind="coverage"
          disabled={busy || dirty}
          onDeleted={deleted}
          onBusy={setBusy}
        />
        <div className={ui.actions}>
          <Button
            disabled={busy}
            onClick={() =>
              run(() => {
                setBase(null);
                setNewId(crypto.randomUUID());
                setContent(initialTemplate);
                setFormat('json');
                setPreview(null);
                setForm(null);
                setFormPreview(null);
                setError(null);
              })
            }
          >
            新建覆盖方案
          </Button>
          <Button disabled={busy} onClick={() => void plans.refetch()}>
            刷新方案列表
          </Button>
        </div>
        {plans.data?.length ? (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <caption>可复用的覆盖方案</caption>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>版本</th>
                  <th>格子</th>
                  <th>目标条数</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {plans.data.map((row) => (
                  <tr key={row.coverage_id}>
                    <td>{row.name}</td>
                    <td>r{row.revision}</td>
                    <td>{row.cell_count}</td>
                    <td>{row.target_count}</td>
                    <td>
                      <Button small disabled={busy} onClick={() => open(row.coverage_id)}>
                        编辑 {row.name}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="还没有覆盖方案"
            description="可以保存下方初始模板，或载入本地 JSON / YAML 文件。"
          />
        )}
      </section>
      <section className={`${ui.section} ${ui.stack}`} aria-label="覆盖方案编辑器">
        <h2>{base ? `编辑 ${base.name} · r${base.revision}` : '新方案'}</h2>
        <p className={ui.caption}>
          运行文件使用 JSON。模板仅包含 schema_version、name 和 plan；名称在 name
          中编辑。仅校验结构不代表词典候选能够满足约束，使用前仍需采样预览。
        </p>
        {form ? (
          <>
            <CoverageFormEditor
              form={form}
              layout={formPreview?.signature === formSignature ? formPreview.value.layout : undefined}
              disabled={busy || !canWrite()}
              onChange={setForm}
            />
            <div className={ui.actions}>
              <Button
                disabled={busy || !canWrite()}
                onClick={() =>
                  void action(async () => {
                    setFormPreview(null);
                    const value = await request<EditorView>('POST', P + '/editor/preview', form);
                    setForm(value.form);
                    setFormPreview({ signature: JSON.stringify(value.form), value });
                  })
                }
              >
                预览计划配额
              </Button>
              <Button
                disabled={busy || !canWrite()}
                onClick={() =>
                  void action(async () => {
                    const value = await request<EditorView>('POST', P + '/editor/preview', form);
                    setContent(value.template);
                    setFormat('json');
                    setForm(null);
                    setFormPreview(null);
                    setPreview({ signature: JSON.stringify(['json', value.template]), value });
                  })
                }
              >
                切换到模板文本
              </Button>
            </div>
          </>
        ) : (
          <>
            <Field id="coverage-format" label="模板格式">
              <select
                id="coverage-format"
                disabled={busy}
                value={format}
                onChange={(e) => setFormat(e.target.value as 'json' | 'yaml')}
              >
                <option value="json">JSON</option>
                <option value="yaml">YAML</option>
              </select>
            </Field>
            <Field
              id="coverage-content"
              label="覆盖方案模板"
              hint="UTF-8，最多 1 MiB。YAML 不支持锚点、别名、合并键、自定义类型或重复键。"
            >
              <textarea
                id="coverage-content"
                rows={22}
                value={content}
                disabled={busy}
                spellCheck={false}
                onChange={(e) => setContent(e.target.value)}
              />
            </Field>
          </>
        )}
        <div className={ui.actions}>
          {!form && (
            <Button
              disabled={busy || !canWrite()}
              onClick={() =>
                void action(async () => {
                  const value = await request<EditorView>('POST', P + '/editor', { format, content });
                  setForm(value.form);
                  setFormBase(JSON.stringify(value.form));
                  setFormPreview({ signature: JSON.stringify(value.form), value });
                  setPreview(null);
                })
              }
            >
              使用表单编辑
            </Button>
          )}
          {!form && (
            <Button
              disabled={busy || !canWrite()}
              onClick={() =>
                void action(async () => {
                  setPreview(null);
                  setPreview({
                    signature,
                    value: await request<CoverageView>('POST', P + '/preview', { format, content }),
                  });
                })
              }
            >
              校验模板
            </Button>
          )}
          <Button primary disabled={busy || !canWrite() || (!!base && !dirty)} onClick={() => void save()}>
            保存覆盖方案
          </Button>
          {base && (
            <>
              <DeleteLibrary
                resource={{
                  kind: 'coverage',
                  resource_id: base.coverage_id,
                  name: base.name,
                  revision: base.revision,
                  version: base.version,
                }}
                disabled={busy || dirty}
                onDeleted={deleted}
                onBusy={setBusy}
              />
              <Button disabled={busy} onClick={() => open(base.coverage_id)}>
                重新载入已保存版本
              </Button>
              <Button
                disabled={busy || dirty}
                onClick={() => {
                  setBase(null);
                  setNewId(crypto.randomUUID());
                  setPreview(null);
                }}
              >
                以此新建副本
              </Button>
            </>
          )}
        </div>
        {base && dirty && (
          <p className={ui.caption}>删除方案前，请先保存草稿，或重新载入已保存版本并明确放弃草稿。</p>
        )}
        {!form && preview?.signature === signature && (
          <>
            <Summary value={preview.value} />
            <p className={ui.notice}>模板结构校验通过，尚未保存。</p>
          </>
        )}
        {base && (
          <div className={ui.actions}>
            {(['json', 'yaml'] as const).map((kind) => (
              <Button
                key={kind}
                disabled={busy || dirty}
                onClick={() =>
                  void action(async () => {
                    const exported = await request<{ content: string; filename: string; media_type: string }>(
                      'GET',
                      P + '/' + base.coverage_id + '/export?format=' + kind,
                    );
                    const url = URL.createObjectURL(
                      new Blob([exported.content], { type: exported.media_type }),
                    );
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = exported.filename;
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                  })
                }
              >
                导出 {kind.toUpperCase()} 模板
              </Button>
            ))}
          </div>
        )}
        <Field id="coverage-file" label="选择本地覆盖模板">
          <input
            id="coverage-file"
            type="file"
            accept=".json,.yaml,.yml"
            disabled={busy}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </Field>
        <Button
          disabled={busy || !file || !canWrite()}
          onClick={() =>
            void action(async () => {
              if (file!.size > 1024 * 1024) throw new Error('模板文件不能超过 1 MiB。');
              const extension = file!.name.split('.').pop()?.toLowerCase();
              if (!['json', 'yaml', 'yml'].includes(extension ?? ''))
                throw new Error('请选择 JSON、YAML 或 YML 文件。');
              const uploaded = new TextDecoder('utf-8', { fatal: true }).decode(await file!.arrayBuffer());
              const value = await request<CoverageView>('POST', P + '/preview', {
                format: extension === 'json' ? 'json' : 'yaml',
                content: uploaded,
              });
              run(() => {
                setBase(null);
                setNewId(crypto.randomUUID());
                setContent(value.template);
                setFormat('json');
                setForm(null);
                setFormPreview(null);
                setPreview({ signature: JSON.stringify(['json', value.template]), value });
              });
            })
          }
        >
          载入文件为新方案草稿
        </Button>
        <p className={ui.caption}>
          载入只做校验并生成新草稿；点击保存后才写入本地方案库。导出文件不包含方案编号、修订历史或请求记录。
        </p>
      </section>
    </div>
  );
}
