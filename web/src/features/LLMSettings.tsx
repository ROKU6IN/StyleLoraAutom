import { useCallback, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { allPages, request, waitOperation } from '../api/client';
import { errorText } from '../api/errors';
import type { Accepted } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite } from '../state/session';
import { Button, ErrorBox, Field, ui } from '../ui';

type Protocol = 'openai_chat' | 'openai_responses' | 'anthropic_messages';
type Model = {
  name: string;
  supports_images: boolean;
  capability_source: string;
  tested_at: string | null;
  response_aliases: string[];
};
type Config = { base_url: string; protocol: Protocol; supports_json_schema: boolean; models: Model[] };
type Token = { token_ref: string; has_secret: boolean; effective_available: boolean; origin: string | null };
type Endpoint = Config & { endpoint_id: string; version: string; tokens: Token[] };
type Roles = {
  roles: Record<string, { endpoint_id: string; model: string } | null>;
  availability: Record<string, boolean>;
  version: string;
};
const protocols = {
  openai_chat: 'OpenAI Chat Completions',
  openai_responses: 'OpenAI Responses',
  anthropic_messages: 'Anthropic Messages',
};
const roleNames: Record<string, string> = {
  prompt: '提示词组合',
  prompt_nsfw: '提示词组合 · NSFW',
  vision: '图片描述',
  vision_nsfw: '图片描述 · NSFW',
  rating: '图片分级',
};
const refresh = () => queryClient.invalidateQueries({ queryKey: ['llm'] });
const configOf = (value: Config): Config => ({
  base_url: value.base_url,
  protocol: value.protocol,
  supports_json_schema: value.supports_json_schema,
  models: value.models,
});

function Editor({ original, done }: { original: Endpoint | null; done: () => void }) {
  const [draft, setDraft] = useState<Config>(
    original
      ? configOf(original)
      : { base_url: '', protocol: 'openai_chat', supports_json_schema: false, models: [] },
  );
  const action = useAction();
  const setError = action.setError;
  const { register } = useDraftGuard();
  const save = useCallback(async () => {
    try {
      await request(
        original ? 'PUT' : 'POST',
        original ? `/api/llm/endpoints/${original.endpoint_id}` : '/api/llm/endpoints',
        draft,
        { version: original?.version },
      );
      await refresh();
      done();
      return true;
    } catch (error) {
      setError(error);
      return false;
    }
  }, [draft, original, done, setError]);
  useEffect(() => {
    register({ dirty: true, label: 'LLM 连接配置', save, discard: done });
    return () => register(null);
  }, [register, save, done]);
  return (
    <section className={ui.section} aria-label="连接编辑器">
      <h2>{original ? '编辑连接' : '新增连接'}</h2>
      <Field
        id="llm-url"
        label="API 基础地址"
        hint="包含版本前缀，例如 https://api.openai.com/v1；保存不会请求此地址。"
      >
        <input
          id="llm-url"
          value={draft.base_url}
          onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
        />
      </Field>
      <Field id="llm-protocol" label="协议">
        <select
          id="llm-protocol"
          value={draft.protocol}
          onChange={(e) => setDraft({ ...draft, protocol: e.target.value as Protocol })}
        >
          {Object.entries(protocols).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </Field>
      <label className={ui.row}>
        <input
          type="checkbox"
          checked={draft.supports_json_schema}
          onChange={(e) => setDraft({ ...draft, supports_json_schema: e.target.checked })}
        />
        服务支持原生 JSON Schema 输出
      </label>
      <h3>模型</h3>
      {draft.models.map((model, index) => (
        <div key={index} className={ui.stack}>
          <Field id={`llm-model-${index}`} label={`模型 ${index + 1}`}>
            <input
              id={`llm-model-${index}`}
              value={model.name}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  models: draft.models.map((m, i) =>
                    i === index
                      ? { ...m, name: e.target.value, capability_source: 'manual', tested_at: null }
                      : m,
                  ),
                })
              }
            />
          </Field>
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={model.supports_images}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  models: draft.models.map((m, i) =>
                    i === index
                      ? {
                          ...m,
                          supports_images: e.target.checked,
                          capability_source: 'manual',
                          tested_at: null,
                        }
                      : m,
                  ),
                })
              }
            />
            手动声明支持图片
          </label>
          <Field
            id={`llm-alias-${index}`}
            label="允许的响应模型别名"
            hint="用英文逗号分隔；响应模型必须与选择或别名一致。"
          >
            <input
              id={`llm-alias-${index}`}
              value={model.response_aliases.join(',')}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  models: draft.models.map((m, i) =>
                    i === index
                      ? {
                          ...m,
                          response_aliases: e.target.value
                            .split(',')
                            .map((v) => v.trim())
                            .filter(Boolean),
                        }
                      : m,
                  ),
                })
              }
            />
          </Field>
          <Button
            quiet
            onClick={() => setDraft({ ...draft, models: draft.models.filter((_, i) => i !== index) })}
          >
            移除模型 {index + 1}
          </Button>
        </div>
      ))}
      <Button
        quiet
        onClick={() =>
          setDraft({
            ...draft,
            models: [
              ...draft.models,
              {
                name: '',
                supports_images: false,
                capability_source: 'manual',
                tested_at: null,
                response_aliases: [],
              },
            ],
          })
        }
      >
        添加模型
      </Button>
      <ErrorBox message={action.error ? errorText(action.error) : undefined} />
      <div className={ui.actions}>
        <Button onClick={done}>取消编辑</Button>
        <Button
          primary
          disabled={!canWrite() || action.busy || !draft.base_url}
          onClick={() =>
            void action.run(async () => {
              await save();
            })
          }
        >
          保存连接
        </Button>
      </div>
    </section>
  );
}

function EndpointCard({ endpoint, edit }: { endpoint: Endpoint; edit: () => void }) {
  const [secret, setSecret] = useState('');
  const [notice, setNotice] = useState('');
  const action = useAction();
  const path = `/api/llm/endpoints/${endpoint.endpoint_id}`;
  const writeToken = (ref?: string) =>
    void action.run(async () => {
      try {
        await request(
          ref ? 'PUT' : 'POST',
          path + '/tokens' + (ref ? '/' + ref : ''),
          { value: secret },
          { version: endpoint.version },
        );
        setNotice('密钥已保存。');
      } finally {
        setSecret('');
        await refresh();
      }
    });
  const diagnose = (model?: string) =>
    void action.run(async () => {
      setNotice('检查正在运行，可在“任务进度”中取消。');
      try {
        const task = await request<Accepted>(
          'POST',
          path + (model ? '/models/test-image' : '/models/probe'),
          model ? { model } : undefined,
          { version: endpoint.version },
        );
        await waitOperation(task);
        setNotice(model ? '固定图片识别通过，图片能力已更新。' : '模型列表已更新，已有手动设置已保留。');
      } catch (error) {
        setNotice('检查未完成，请查看下方结果。');
        throw error;
      } finally {
        await refresh();
      }
    });
  return (
    <section className={ui.section} aria-label={`连接 ${endpoint.base_url}`}>
      <h2>{endpoint.base_url}</h2>
      <p>{protocols[endpoint.protocol]}</p>
      <div className={ui.row}>
        <Button onClick={edit} disabled={action.busy}>
          编辑连接
        </Button>
        <Button disabled={!canWrite() || action.busy} onClick={() => diagnose()}>
          查询模型列表
        </Button>
        <Button
          danger
          disabled={!canWrite() || action.busy || endpoint.tokens.length > 0}
          onClick={() =>
            void action.run(async () => {
              await request('DELETE', path, undefined, { version: endpoint.version });
              await refresh();
            })
          }
        >
          删除连接
        </Button>
      </div>
      <p className={ui.muted}>
        诊断会访问此地址，图片检查会发送程序生成的几何图案。删除连接前请解除角色绑定并移除密钥。
      </p>
      <Field
        id={`secret-${endpoint.endpoint_id}`}
        label="新密钥"
        hint="只在当前输入框暂存，提交后清空。结果不确定时先刷新状态，勿重复写入。"
      >
        <input
          id={`secret-${endpoint.endpoint_id}`}
          type="password"
          autoComplete="new-password"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
      </Field>
      <Button disabled={!secret || !canWrite() || action.busy} onClick={() => writeToken()}>
        添加密钥
      </Button>
      {endpoint.tokens.map((token, i) => (
        <div className={ui.row} key={token.token_ref}>
          <span>
            密钥 {i + 1} · {token.effective_available ? '可用' : '不可用'} ·{' '}
            {token.origin === 'environment' ? '环境变量覆盖' : token.has_secret ? '本机加密存储' : '未保存'}
          </span>
          <Button
            quiet
            disabled={!secret || !canWrite() || action.busy}
            onClick={() => writeToken(token.token_ref)}
          >
            替换密钥 {i + 1}
          </Button>
          <Button
            quiet
            disabled={!canWrite() || action.busy}
            onClick={() =>
              void action.run(async () => {
                const result = await request<Token>(
                  'DELETE',
                  path + '/tokens/' + token.token_ref,
                  undefined,
                  { version: endpoint.version },
                );
                setNotice(
                  result.origin === 'environment'
                    ? '本机密钥与关联已移除；环境变量仍由系统管理。'
                    : '密钥已移除。',
                );
                await refresh();
              })
            }
          >
            移除密钥 {i + 1}
          </Button>
        </div>
      ))}
      {endpoint.models.map((model) => (
        <div key={model.name} className={ui.row}>
          <span>
            {model.name} · {model.supports_images ? '支持图片' : '未声明图片能力'} ·{' '}
            {model.capability_source === 'tested'
              ? '图片测试通过'
              : model.capability_source === 'models'
                ? '模型列表'
                : '手动配置'}
          </span>
          <Button quiet disabled={!canWrite() || action.busy} onClick={() => diagnose(model.name)}>
            检查图片能力：{model.name}
          </Button>
        </div>
      ))}
      {notice && <p role="status">{notice}</p>}
      <ErrorBox message={action.error ? errorText(action.error) : undefined} />
    </section>
  );
}

export default function LLMSettings() {
  const endpoints = useQuery({
    queryKey: ['llm', 'endpoints'],
    queryFn: ({ signal }) => allPages<Endpoint>('/api/llm/endpoints', signal),
  });
  const roles = useQuery({
    queryKey: ['llm', 'roles'],
    queryFn: () => request<Roles>('GET', '/api/llm/roles'),
  });
  const templates = useQuery({
    queryKey: ['llm', 'templates'],
    queryFn: () => request<{ items: { id: string; available: boolean }[] }>('GET', '/api/llm/templates'),
  });
  const [editor, setEditor] = useState<{ original: Endpoint | null } | null>(null);
  const done = useCallback(() => setEditor(null), []);
  const action = useAction();
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>GLOBAL / LLM</div>
          <h1>LLM 连接与角色</h1>
          <p>全局连接保存在本机，密钥由 Windows 当前用户加密保护。</p>
        </div>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : endpoints.error
              ? errorText(endpoints.error)
              : roles.error
                ? errorText(roles.error)
                : templates.error
                  ? errorText(templates.error)
                  : undefined
        }
      />
      {editor ? (
        <Editor key={editor.original?.endpoint_id ?? 'new'} original={editor.original} done={done} />
      ) : (
        <Button primary disabled={!canWrite()} onClick={() => setEditor({ original: null })}>
          新增连接
        </Button>
      )}
      {!editor &&
        endpoints.data?.map((endpoint) => (
          <EndpointCard
            key={endpoint.endpoint_id}
            endpoint={endpoint}
            edit={() => setEditor({ original: endpoint })}
          />
        ))}
      <section className={ui.section} aria-label="角色绑定">
        <h2>角色绑定</h2>
        <p>
          选择后立即保存。视觉任务在项目侧栏启动；分级角色可使用文本或图片模型。clean 角色保留，暂不执行。
        </p>
        {roles.data &&
          Object.entries(roleNames).map(([role, name]) => (
            <Field
              key={role}
              id={`role-${role}`}
              label={name}
              hint={roles.data!.availability[role] ? '凭据可用' : '未配置或凭据不可用'}
            >
              <select
                id={`role-${role}`}
                disabled={!canWrite() || action.busy || !!editor}
                value={roles.data!.roles[role] ? JSON.stringify(roles.data!.roles[role]) : ''}
                onChange={(e) => {
                  const binding = e.target.value
                    ? (JSON.parse(e.target.value) as Roles['roles'][string])
                    : null;
                  void action.run(async () => {
                    await request(
                      'PUT',
                      '/api/llm/roles',
                      { roles: { ...roles.data!.roles, [role]: binding } },
                      { version: roles.data!.version },
                    );
                    await refresh();
                  });
                }}
              >
                <option value="">未绑定</option>
                {endpoints.data?.flatMap((ep) =>
                  ep.models
                    .filter((m) => !['vision', 'vision_nsfw'].includes(role) || m.supports_images)
                    .map((model) => (
                      <option
                        key={ep.endpoint_id + '/' + model.name}
                        value={JSON.stringify({ endpoint_id: ep.endpoint_id, model: model.name })}
                      >
                        {model.name} · {ep.base_url}
                      </option>
                    )),
                )}
              </select>
            </Field>
          ))}
      </section>
      <section className={ui.section}>
        <h2>运行模板</h2>
        {templates.data?.items.map((template) => (
          <p key={template.id}>
            {template.id} · {template.available ? '已装载并校验' : '文档尚未提供'}
          </p>
        ))}
      </section>
    </div>
  );
}
