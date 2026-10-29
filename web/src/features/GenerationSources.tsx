import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams, Link } from 'react-router';
import { allPages, request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { AppError, errorCodeText, errorText } from '../api/errors';
import type { Source } from '../api/types';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';
import { PresetPicker } from './GenerationPresets';
import GenerationSchemaForm, {
  configObject,
  parameterIssues,
  supportedSchema,
  type FormCapabilities,
} from './GenerationSchemaForm';
import GenerationCosts from './GenerationCosts';
import GenerationRecovery from './GenerationRecovery';
import GenerationLauncherCheck from './GenerationLauncherCheck';

type Config = {
  schema_version: number;
  backend: string;
  profile: Record<string, unknown>;
  sizes: Record<string, { width: number; height: number }>;
  default_aspect: string;
};
type GenerationSource = Source & {
  config_json: Config;
  style_prompt: string;
  negative_prompt: string;
  target_count: number;
  cost_policy: string;
  locked: boolean;
  runtime: { pause_reason: string | null };
};
type Task = { task_id: string; revision: number; ready: boolean; requested: number };
type Projection = { source_digest: string; seeds_digest: string; count: number };
type Handoff = Projection & {
  preview_id: string;
  digest: string;
  source_id: number;
  source_version: string;
  can_start: boolean;
  blockers: string[];
  trigger_validation: { status: string; issues: { code: string }[] };
  cost_policy: string;
  aspect_counts: Record<string, number>;
  examples: {
    seq: number;
    positive: string;
    negative: string;
    aspect: string;
    width: number;
    height: number;
  }[];
  examples_truncated: boolean;
};
type Capabilities = {
  default_cost_policy?: string;
  generation: boolean;
  reason: string | null;
  size_presets: Record<string, Config['sizes']>;
};

function message(value: unknown) {
  return value instanceof Error && !('code' in value) ? value.message : errorText(value);
}
export function generationDraft(
  name: string,
  count: string,
  style: string,
  negative: string,
  cost: string,
  config: string,
) {
  const target = Number(count);
  if (!name.trim() || !Number.isInteger(target) || target < 1 || target > 10000)
    throw new Error('请填写来源名称与 1–10000 的目标数量。');
  let parsed: unknown;
  try {
    parsed = JSON.parse(config);
  } catch {
    throw new Error('生成配置不是有效的 JSON。');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('生成配置需为对象。');
  return {
    name: name.trim(),
    target_count: target,
    style_prompt: style,
    negative_prompt: negative,
    cost_policy: cost,
    config: parsed,
  };
}

export default function GenerationSources({ pid }: { pid: string }) {
  const [allowDialog, setAllowDialog] = useState(false);
  const [allowChecked, setAllowChecked] = useState(false);
  const [params] = useSearchParams();
  const { register, run } = useDraftGuard();
  const [base, setBase] = useState<GenerationSource | null>(null),
    [name, setName] = useState(''),
    [count, setCount] = useState('100'),
    [style, setStyle] = useState(''),
    [negative, setNegative] = useState(''),
    [costChoice, setCost] = useState<string | null>(null),
    [configText, setConfig] = useState<string | null>(null),
    [taskId, setTaskId] = useState(params.get('task') ?? ''),
    [preview, setPreview] = useState<Handoff | null>(null),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState('');
  const saveKey = useRef({ signature: '', key: '' }),
    startKey = useRef({ signature: '', key: '' });
  const path = projectPath(pid) + '/sources';
  const capabilities = useQuery({
    queryKey: ['generation', 'capabilities'],
    queryFn: ({ signal }) =>
      request<Capabilities>('GET', '/api/generation/capabilities', undefined, { signal }),
  });
  const defaultCost = capabilities.data?.default_cost_policy ?? 'ask';
  const cost = costChoice ?? defaultCost;
  const backend = configObject(configText ?? '')?.backend ?? base?.config_json.backend ?? 'nai_direct';
  const backendCapabilities = useQuery({
    queryKey: ['backend', backend, 'form-capabilities'],
    queryFn: ({ signal }) =>
      request<FormCapabilities>('GET', `/api/backends/${backend}/capabilities`, undefined, { signal }),
    enabled: backend === 'nai_direct' || backend === 'nai_launcher',
    retry: false,
  });
  const descriptor = backendCapabilities.data?.backend === backend ? backendCapabilities.data : undefined;
  const initialText =
    descriptor?.form?.version === 1 ? JSON.stringify(descriptor.form.default_config, null, 2) : '';
  const config = configText ?? initialText;
  const parsedConfig = configObject(config);
  const formIssues =
    descriptor?.form?.version === 1 && supportedSchema(descriptor.config_schema) && parsedConfig
      ? parameterIssues(descriptor.config_schema, parsedConfig)
      : {};
  const serverIssues = Object.fromEntries(
    error instanceof AppError
      ? error.issues.map((issue) => [
          issue.field.replace(/^body\./, ''),
          `后端拒绝此字段（${issue.code}），请核对参数。`,
        ])
      : [],
  );
  const sources = useQuery({
    queryKey: projectKey(pid, 'generation-sources'),
    queryFn: ({ signal }) => allPages<GenerationSource>(path, signal),
  });
  const tasks = useQuery({
    queryKey: ['prompt', 'handoff-tasks'],
    queryFn: ({ signal }) => allPages<Task>('/api/prompt-production/tasks', signal),
    refetchInterval: 3000,
  });
  const selected = tasks.data?.find((t) => t.task_id === taskId);
  const baseConfig = base ? JSON.stringify(base.config_json, null, 2) : initialText;
  const dirty =
    name !== (base?.name ?? '') ||
    count !== String(base?.target_count ?? 100) ||
    style !== (base?.style_prompt ?? '') ||
    negative !== (base?.negative_prompt ?? '') ||
    cost !== (base?.cost_policy ?? defaultCost) ||
    config !== baseConfig;
  const locked = !!base && (base.locked || base.status !== 'draft');
  const editDraft = () => {
    if (costChoice === null) setCost(cost);
    if (config) setConfig(config);
    setError(null);
    setPreview(null);
    setConfirmed(false);
  };
  const apply = useCallback((value: GenerationSource | null) => {
    setBase(value);
    setName(value?.name ?? '');
    setCount(String(value?.target_count ?? 100));
    setStyle(value?.style_prompt ?? '');
    setNegative(value?.negative_prompt ?? '');
    setCost(value?.cost_policy ?? null);
    setConfig(value ? JSON.stringify(value.config_json, null, 2) : null);
    setError(null);
    setPreview(null);
    setConfirmed(false);
  }, []);
  const refresh = useCallback(() => queryClient.invalidateQueries({ queryKey: ['project', pid] }), [pid]);
  const save = useCallback(async () => {
    if (busy || locked || !canWrite()) return false;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const body = generationDraft(name, count, style, negative, cost, config);
      if (
        descriptor?.form?.version === 1 &&
        supportedSchema(descriptor.config_schema) &&
        Object.keys(parameterIssues(descriptor.config_schema, body.config)).length
      )
        throw new Error('请先修正生成参数表单中的错误。');
      const signature = JSON.stringify([pid, base?.source_id, base?.version, body]);
      if (saveKey.current.signature !== signature) saveKey.current = { signature, key: crypto.randomUUID() };
      const value = await request<GenerationSource>(
        base ? 'PUT' : 'POST',
        path + (base ? '/' + base.source_id : ''),
        body,
        { version: base?.version, key: saveKey.current.key },
      );
      apply(value);
      await refresh();
      setNotice('生成源草稿已保存；尚未锁定或提交生成。');
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [apply, base, busy, config, cost, count, descriptor, locked, name, negative, path, pid, refresh, style]);
  useEffect(() => {
    register({ dirty, label: '生成源草稿', save, discard: () => apply(base) });
    return () => register(null);
  }, [apply, base, dirty, register, save]);
  const act = async (fn: () => Promise<void>) => {
    if (busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await fn();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const open = (id: string) =>
    run(
      () => void act(async () => apply(id ? await request<GenerationSource>('GET', path + '/' + id) : null)),
    );
  const projection = (value: Handoff): Projection => ({
    source_digest: value.source_digest,
    seeds_digest: value.seeds_digest,
    count: value.count,
  });
  const start = async () => {
    if (!preview) return;
    const targetPath = path + '/' + preview.source_id;
    const body = { ...projection(preview), preview_id: preview.preview_id, digest: preview.digest };
    const signature = JSON.stringify(body);
    if (startKey.current.signature !== signature) startKey.current = { signature, key: crypto.randomUUID() };
    try {
      const result = await request<GenerationSource & { reconciled: boolean }>(
        'POST',
        targetPath + '/start',
        body,
        { version: preview.source_version, key: startKey.current.key },
      );
      apply(result);
      await refresh();
      setNotice(
        result.reconciled
          ? '已核对既有源内容，没有重复派发。'
          : '提示词已完整写入并锁定来源，请查看来源状态。',
      );
    } catch (failure) {
      // A lost reply must not initiate another model/sampling/generation request.
      try {
        const result = await request<GenerationSource & { reconciled: boolean }>(
          'POST',
          targetPath + '/reconcile-start',
          projection(preview),
        );
        if (result.reconciled) {
          apply(result);
          await refresh();
          setNotice('已核对完整锁定内容，没有重复派发。');
          return;
        }
      } catch {
        /* Keep the original error and the same request identity for manual reconciliation. */
      }
      throw failure;
    }
  };
  const validPreview = preview && !dirty && base?.source_id === preview.source_id;
  return (
    <section className={ui.section} aria-label="生成源草稿与提示词交接">
      <h2>生成源草稿与提示词交接</h2>
      <p className={ui.notice}>
        保存草稿和交接预览仅在本地处理。
        {!capabilities.data?.generation && '请先在 NovelAI 直连设置中配置 Token，才能启动生成。'}
      </p>
      <Link to="/settings/nai-direct">配置 NovelAI 直连</Link>
      <ErrorBox
        message={
          error
            ? message(error)
            : sources.error
              ? errorText(sources.error)
              : tasks.error
                ? errorText(tasks.error)
                : capabilities.error
                  ? errorText(capabilities.error)
                  : undefined
        }
      />
      {notice && <p role="status">{notice}</p>}
      <Field id="generation-existing" label="打开生成源">
        <select
          id="generation-existing"
          disabled={busy}
          value={base?.source_id ?? ''}
          onChange={(e) => open(e.target.value)}
        >
          <option value="">新建草稿</option>
          {sources.data
            ?.filter((s) => s.type === 'nai')
            .map((s) => (
              <option key={s.source_id} value={s.source_id}>
                {s.name} · #{s.source_id}
              </option>
            ))}
        </select>
      </Field>
      <fieldset disabled={busy || locked || !canWrite()}>
        <legend>源参数</legend>
        <div className={ui.formGrid}>
          <Field id="generation-name" label="生成源名称">
            <input
              id="generation-name"
              value={name}
              onChange={(e) => {
                editDraft();
                setName(e.target.value);
              }}
            />
          </Field>
          <Field id="generation-count" label="生成目标数量">
            <input
              id="generation-count"
              type="number"
              min="1"
              max="10000"
              value={count}
              onChange={(e) => {
                editDraft();
                setCount(e.target.value);
              }}
            />
          </Field>
          <Field id="generation-cost" label="非零费用策略">
            <select
              id="generation-cost"
              value={cost}
              onChange={(e) => {
                if (e.target.value === 'allow') {
                  setAllowChecked(false);
                  setAllowDialog(true);
                } else {
                  editDraft();
                  setCost(e.target.value);
                }
              }}
            >
              <option value="deny">禁止非零费用</option>
              <option value="ask">每个运行周期询问</option>
              <option value="allow">允许非零费用</option>
            </select>
          </Field>
        </div>
        <Field id="generation-style" label="画风提示词">
          <textarea
            id="generation-style"
            rows={2}
            value={style}
            onChange={(e) => {
              editDraft();
              setStyle(e.target.value);
            }}
          />
        </Field>
        <Field id="generation-negative" label="生成负面词">
          <textarea
            id="generation-negative"
            rows={2}
            value={negative}
            onChange={(e) => {
              editDraft();
              setNegative(e.target.value);
            }}
          />
        </Field>
        <PresetPicker
          disabled={busy || locked || !canWrite()}
          action={act}
          onSelect={(sizes) => {
            let parsed: Config;
            try {
              parsed = JSON.parse(config) as Config;
              if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
            } catch {
              throw new Error('请先修正生成配置 JSON。');
            }
            const next = { ...parsed, sizes };
            if (
              descriptor?.form?.version === 1 &&
              supportedSchema(descriptor.config_schema) &&
              Object.keys(parameterIssues(descriptor.config_schema, next)).some((field) =>
                field.startsWith('config.sizes'),
              )
            )
              throw new Error('此尺寸预设超出当前后端能力范围，原配置已保留。');
            setConfig(JSON.stringify(next, null, 2));
            setError(null);
            setPreview(null);
            setConfirmed(false);
          }}
        />
        {descriptor && parsedConfig ? (
          <GenerationSchemaForm
            capabilities={descriptor}
            value={parsedConfig}
            serverIssues={serverIssues}
            onChange={(value) => {
              setConfig(JSON.stringify(value, null, 2));
              setError(null);
              setPreview(null);
              setConfirmed(false);
            }}
          />
        ) : (
          <p role="status">
            {backendCapabilities.isLoading
              ? '正在读取后端参数描述…'
              : '参数表单暂不可用，请核对高级 JSON。已有配置已保留。'}
          </p>
        )}
        <details>
          <summary>高级 JSON 配置</summary>
          <Field
            id="generation-config"
            label="生成配置（JSON）"
            hint="与表单共用同一份配置。修改不会自动删除未知字段；保存时由后端最终校验。"
          >
            <textarea
              id="generation-config"
              rows={12}
              value={config}
              onChange={(e) => {
                setConfig(e.target.value);
                setError(null);
                setPreview(null);
                setConfirmed(false);
              }}
            />
          </Field>
          {!parsedConfig && <p role="alert">请填写有效的生成配置 JSON 对象。</p>}
        </details>
        {Object.entries(serverIssues)
          .filter(
            ([field]) =>
              !descriptor?.form?.labels[field] ||
              !parsedConfig ||
              descriptor.form.version !== 1 ||
              !supportedSchema(descriptor.config_schema),
          )
          .map(([field, detail]) => (
            <p key={field} role="alert">
              {field}: {detail}
            </p>
          ))}
      </fieldset>
      {Object.keys(formIssues).length > 0 && <p role="alert">请修正标出的参数后保存；原输入已保留。</p>}
      <Button disabled={busy || locked || !dirty || !config || !canWrite()} onClick={() => void save()}>
        保存生成源草稿
      </Button>
      {locked && (
        <p className={ui.notice}>
          此源已锁定或结束；生成内容与参数不可改写。状态：{base.status}。
          {base.runtime?.pause_reason && errorCodeText(base.runtime.pause_reason)}
        </p>
      )}
      {base && (
        <GenerationCosts
          key={base.source_id}
          pid={pid}
          sourceId={base.source_id}
          backend={base.config_json.backend === 'nai_direct' ? 'nai_direct' : 'nai_launcher'}
          disabled={busy || dirty}
          onChanged={async () => {
            apply(await request<GenerationSource>('GET', path + '/' + base.source_id));
            await refresh();
          }}
        />
      )}
      {base && base.config_json.backend !== 'nai_direct' && (
        <GenerationLauncherCheck
          key={`launcher-${pid}-${base.source_id}`}
          pid={pid}
          sourceId={base.source_id}
          sourceVersion={base.version}
          disabled={busy || dirty}
        />
      )}
      {base && (
        <GenerationRecovery
          key={`recovery-${base.source_id}`}
          direct={base.config_json.backend === 'nai_direct'}
          pid={pid}
          sourceId={base.source_id}
          disabled={busy || dirty}
        />
      )}
      <Field id="generation-task" label="已就绪的提示词任务">
        <select
          id="generation-task"
          disabled={busy || locked}
          value={taskId}
          onChange={(e) => {
            setTaskId(e.target.value);
            setPreview(null);
            setConfirmed(false);
          }}
        >
          <option value="">选择已完成且就绪的任务</option>
          {tasks.data
            ?.filter((t) => t.ready)
            .map((t) => (
              <option key={t.task_id} value={t.task_id}>
                {t.task_id.slice(0, 8)} · {t.requested} 条
              </option>
            ))}
        </select>
      </Field>
      <p>
        <Link to="/prompt-production">前往提示词生产</Link>
        。交接会读取完整结果，不限于浏览器当前页；缺少图幅的条目使用源默认图幅。
      </p>
      <Button
        disabled={busy || locked || dirty || !base || !selected?.ready || !canWrite()}
        onClick={() =>
          void act(async () => {
            setPreview(null);
            setConfirmed(false);
            setPreview(
              await request<Handoff>(
                'POST',
                path + '/' + base!.source_id + '/handoff-preview',
                { task_id: selected!.task_id, expected_task_revision: selected!.revision },
                { version: base!.version },
              ),
            );
          })
        }
      >
        预览提示词交接
      </Button>
      {validPreview && (
        <div aria-label="提示词交接预览">
          <p>
            完整交接 {preview.count} 条；
            {Object.entries(preview.aspect_counts)
              .map(([aspect, total]) => `${aspect}: ${total}`)
              .join('，')}
            。
          </p>
          <p>费用策略：{preview.cost_policy}；尚未取得估价，不代表免费。</p>
          {preview.blockers.map((code) => (
            <p className={ui.warning} key={code}>
              {errorCodeText(code)}
            </p>
          ))}
          {preview.trigger_validation.issues.map((issue) => (
            <p className={ui.warning} key={issue.code}>
              {errorCodeText(issue.code)}
            </p>
          ))}
          <details>
            <summary>查看发送内容和尺寸样例（最多 100 条）</summary>
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>完整正向提示词</th>
                    <th>负面词</th>
                    <th>像素</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.examples.map((row) => (
                    <tr key={row.seq}>
                      <td>{row.seq}</td>
                      <td>{row.positive}</td>
                      <td>{row.negative}</td>
                      <td>
                        {row.width} × {row.height}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <label className={ui.check}>
            <input
              type="checkbox"
              disabled={!preview.can_start || busy}
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            确认锁定来源并启动生成，按所选费用策略处理
          </label>
          <Button
            primary
            disabled={busy || !preview.can_start || !confirmed || !canWrite()}
            onClick={() => void act(start)}
          >
            确认交接并启动
          </Button>
        </div>
      )}
      {allowDialog && (
        <Modal
          open
          title="允许草稿中的非零费用"
          description="请核对后续生成的费用策略。"
          onOpenChange={setAllowDialog}
        >
          <p>此策略允许后续按 Anlas 估算直接提交，不再由本产品逐周期询问；保存草稿和启动生成仍需分别操作。</p>
          <Button onClick={() => setAllowDialog(false)}>暂不选择</Button>
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={allowChecked}
              onChange={(e) => setAllowChecked(e.target.checked)}
            />
            我理解费用范围并同意
          </label>
          <HoldButton
            disabled={!allowChecked}
            onConfirm={() => {
              editDraft();
              setCost('allow');
              setAllowDialog(false);
            }}
          >
            按住选择允许费用
          </HoldButton>
        </Modal>
      )}
    </section>
  );
}
