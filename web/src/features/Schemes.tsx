import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { allPages, request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { errorText } from '../api/errors';
import type { Scheme, ImportConfiguration, ExportConfiguration, Source } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, Modal, ui } from '../ui';

type Configuration = ImportConfiguration | ExportConfiguration;
export default function Schemes({ pid }: { pid: string }) {
  const action = useAction(),
    guard = useDraftGuard();
  useSession((s) => s.connection);
  const [kind, setKind] = useState<'import' | 'export'>('import');
  const [edit, setEdit] = useState<{ base: Scheme | null; value: Configuration; key: string } | null>(null);
  const [deleting, setDeleting] = useState<{
    row: Scheme;
    others: Scheme[];
    replacement: number | null;
    key: string;
  } | null>(null);
  const path = `${projectPath(pid)}/${kind}-schemes`;
  const rows = useQuery({
    queryKey: projectKey(pid, `${kind}-schemes`),
    queryFn: ({ signal }) => allPages<Scheme>(path, signal),
  });
  const caps = useQuery({
    queryKey: ['exchange-capabilities'],
    queryFn: () =>
      request<{
        default_import: ImportConfiguration;
        default_export: ExportConfiguration;
        sorters: string[];
      }>('GET', '/api/exchange/capabilities'),
  });
  const sources = useQuery({
    queryKey: projectKey(pid, 'sources'),
    queryFn: ({ signal }) => allPages<Source>(`${projectPath(pid)}/sources`, signal),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['project', pid] });
  const save = async () => {
    if (!edit) return false;
    let saved = false;
    await action.run(async () => {
      await request(
        edit.base ? 'PUT' : 'POST',
        path + (edit.base ? `/${edit.base.scheme_id}` : ''),
        edit.value,
        { version: edit.base?.version, key: edit.key },
      );
      setEdit(null);
      await refresh();
      saved = true;
    });
    return saved;
  };
  const saveRef = useRef(save);
  saveRef.current = save;
  const register = guard.register;
  useEffect(() => {
    register({ dirty: !!edit, label: '方案', save: () => saveRef.current(), discard: () => setEdit(null) });
  }, [edit, register]);
  useEffect(() => () => register(null), [register]);
  const change = (value: Configuration) =>
    setEdit((old) => (old ? { ...old, value, key: crypto.randomUUID() } : null));
  const start = (row: Scheme | null) => {
    action.setError(null);
    setEdit({
      base: row,
      value: structuredClone(
        row?.configuration ?? (kind === 'import' ? caps.data!.default_import : caps.data!.default_export),
      ),
      key: crypto.randomUUID(),
    });
  };
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>EXCHANGE PRESETS</div>
          <h1>导入导出方案</h1>
          <p>保存项目专属配置。预览固定方案版本，修改后需重新预览。</p>
        </div>
      </div>
      <div className={ui.row}>
        <Button
          disabled={action.busy}
          primary={kind === 'import'}
          onClick={() =>
            guard.run(() => {
              setEdit(null);
              setKind('import');
              action.setError(null);
            })
          }
        >
          导入方案
        </Button>
        <Button
          disabled={action.busy}
          primary={kind === 'export'}
          onClick={() =>
            guard.run(() => {
              setEdit(null);
              setKind('export');
              action.setError(null);
            })
          }
        >
          导出方案
        </Button>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : rows.error
              ? errorText(rows.error)
              : caps.error
                ? errorText(caps.error)
                : undefined
        }
      />
      <section className={ui.section}>
        <div className={ui.between}>
          <h2>
            {kind === 'import' ? '导入' : '导出'}方案 · {rows.data?.length ?? 0}
          </h2>
          <Button disabled={!!edit || action.busy || !caps.data || !canWrite()} onClick={() => start(null)}>
            新建方案
          </Button>
        </div>
        {rows.data?.map((row) => (
          <div className={ui.between} key={row.scheme_id}>
            <div>
              <strong>{row.name}</strong> {row.is_default && <span className={ui.tag}>默认</span>}
              {!row.executable && (
                <p className={ui.warning}>暂不能执行：{row.unavailable_reasons.join('、')}</p>
              )}
            </div>
            <div className={ui.row}>
              <Button small disabled={!!edit || action.busy} onClick={() => start(row)}>
                编辑 {row.name}
              </Button>
              {kind === 'import' && !row.is_default && (
                <Button
                  small
                  disabled={!!edit || action.busy || !canWrite()}
                  onClick={() =>
                    void action.run(async () => {
                      const previous = rows.data!.find((r) => r.is_default)!;
                      await request('POST', `${path}/${row.scheme_id}/set-default`, {
                        expected_versions: {
                          [row.scheme_id]: row.version,
                          [previous.scheme_id]: previous.version,
                        },
                      });
                      await refresh();
                    })
                  }
                >
                  设为默认 {row.name}
                </Button>
              )}
              <Button
                small
                disabled={!!edit || action.busy || !canWrite() || rows.data!.length === 1}
                onClick={() =>
                  setDeleting({
                    row,
                    others: rows.data!.filter((r) => r.scheme_id !== row.scheme_id),
                    replacement: null,
                    key: crypto.randomUUID(),
                  })
                }
              >
                删除 {row.name}
              </Button>
            </div>
          </div>
        ))}
      </section>
      {edit && (
        <section className={ui.section}>
          <h2>{edit.base ? '编辑方案' : '新建方案'}</h2>
          <fieldset disabled={action.busy || !canWrite()} className={ui.stack}>
            <Field id="scheme-name" label="方案名称">
              <input
                id="scheme-name"
                value={edit.value.name}
                maxLength={512}
                onChange={(e) => change({ ...edit.value, name: e.target.value })}
              />
            </Field>
            {'reader' in edit.value ? (
              <ImportFields value={edit.value} change={change} />
            ) : (
              <ExportFields
                value={edit.value}
                change={change}
                sources={sources.data ?? []}
                pyramidAvailable={caps.data?.sorters?.includes('prompt_pyramid') ?? false}
              />
            )}
          </fieldset>
          <div className={ui.actions}>
            <Button
              disabled={action.busy}
              onClick={() => {
                setEdit(null);
                action.setError(null);
              }}
            >
              放弃修改
            </Button>
            <Button
              primary
              disabled={action.busy || !canWrite() || !edit.value.name.trim()}
              onClick={() => void save()}
            >
              保存方案
            </Button>
          </div>
          <p className={ui.muted}>发生版本冲突时草稿会保留；放弃修改后可读取最新版本。后处理链尚未开放。</p>
        </section>
      )}
      <Modal
        open={!!deleting}
        onOpenChange={(open) => {
          if (!open && !action.busy) setDeleting(null);
        }}
        title="删除方案"
        description="已经开始的任务继续使用保存的快照。删除后不能再选择此方案。"
      >
        {deleting && (
          <div className={ui.stack}>
            <p>删除「{deleting.row.name}」？</p>
            {kind === 'import' && deleting.row.is_default && (
              <Field id="replacement" label="替换默认方案">
                <select
                  id="replacement"
                  value={deleting.replacement ?? ''}
                  onChange={(e) =>
                    setDeleting({
                      ...deleting,
                      replacement: Number(e.target.value) || null,
                      key: crypto.randomUUID(),
                    })
                  }
                >
                  <option value="">请选择</option>
                  {deleting.others.map((r) => (
                    <option key={r.scheme_id} value={r.scheme_id}>
                      {r.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <Button
              primary
              disabled={
                action.busy ||
                !canWrite() ||
                (kind === 'import' && deleting.row.is_default && !deleting.replacement)
              }
              onClick={() =>
                void action.run(async () => {
                  const replacement = deleting.others.find((r) => r.scheme_id === deleting.replacement);
                  const versions = {
                    [deleting.row.scheme_id]: deleting.row.version,
                    ...(replacement ? { [replacement.scheme_id]: replacement.version } : {}),
                  };
                  await request(
                    'DELETE',
                    `${path}/${deleting.row.scheme_id}${replacement ? `?replacement_default_id=${replacement.scheme_id}` : ''}`,
                    undefined,
                    {
                      key: deleting.key,
                      version: deleting.row.version,
                      expectedVersions: kind === 'import' ? versions : undefined,
                    },
                  );
                  setDeleting(null);
                  await refresh();
                })
              }
            >
              确认删除方案
            </Button>
          </div>
        )}
      </Modal>
    </div>
  );
}

function ImportFields({
  value,
  change,
}: {
  value: ImportConfiguration;
  change: (v: ImportConfiguration) => void;
}) {
  const params = value.params_json;
  const set = (patch: Partial<typeof params>) => change({ ...value, params_json: { ...params, ...patch } });
  return (
    <>
      <Field id="scheme-parser" label="标注解析">
        <select
          id="scheme-parser"
          value={params.tokenizer_version}
          onChange={(e) => set({ tokenizer_version: e.target.value as typeof params.tokenizer_version })}
        >
          <option value="comma-v1">本地词库识别（逗号分段）</option>
          <option value="literal-tags-v1">首行全部作为标签（兼容模式）</option>
        </select>
      </Field>
      <p className={ui.muted}>
        词库识别会归一化已知别名；未知内容仅在有明确空行分段时作为描述。权重、转义和控制语法需要人工处理。
      </p>
      <Field id="scheme-encoding" label="文本编码">
        <select
          id="scheme-encoding"
          value={params.encoding}
          onChange={(e) => set({ encoding: e.target.value as typeof params.encoding })}
        >
          <option value="utf-8-sig">UTF-8（兼容 BOM）</option>
          <option value="utf-8">UTF-8</option>
          <option value="gbk">GBK</option>
          <option value="auto">UTF-8 优先，失败后尝试 GBK</option>
        </select>
      </Field>
      <p className={ui.muted}>
        编码回退仅在选择此选项时启用；带 BOM 的损坏 UTF-8 仍报错。GBK
        解码成功不保证原文编码正确，请核对预览中的中文。
      </p>
      <label className={ui.row}>
        <input
          type="checkbox"
          checked={params.recursive}
          onChange={(e) => set({ recursive: e.target.checked })}
        />
        包含子目录
      </label>
    </>
  );
}

function ExportFields({
  value,
  change,
  sources,
  pyramidAvailable,
}: {
  value: ExportConfiguration;
  change: (v: ExportConfiguration) => void;
  sources: Source[];
  pyramidAvailable: boolean;
}) {
  const caption = value.caption_config_json,
    output = value.output_config_json,
    filter = value.filter_json;
  const setCaption = (patch: Partial<typeof caption>) =>
    change({ ...value, caption_config_json: { ...caption, ...patch } });
  const setOutput = (patch: Partial<typeof output>) =>
    change({ ...value, output_config_json: { ...output, ...patch } });
  const labels = { manual: '手动', rule: '规则', llm: '语言模型', wd: 'WD', original: '原始' };
  return (
    <>
      <div className={ui.row}>
        {(['keep', 'unreviewed'] as const).map((status) => (
          <label key={status} className={ui.row}>
            <input
              type="checkbox"
              checked={filter.review_statuses.includes(status)}
              onChange={(e) =>
                change({
                  ...value,
                  filter_json: {
                    ...filter,
                    review_statuses: e.target.checked
                      ? [...filter.review_statuses, status]
                      : filter.review_statuses.filter((v) => v !== status),
                  },
                })
              }
            />
            {status === 'keep' ? '保留图片' : '未审核图片'}
          </label>
        ))}
      </div>
      <Field id="scheme-sources" label="来源范围（按 Ctrl 多选，空选表示全部）">
        <select
          id="scheme-sources"
          multiple
          value={(filter.source_ids ?? []).map(String)}
          onChange={(e) => {
            const ids = Array.from(e.target.selectedOptions, (o) => Number(o.value));
            change({ ...value, filter_json: { ...filter, source_ids: ids.length ? ids : null } });
          }}
        >
          {sources.map((s) => (
            <option key={s.source_id} value={s.source_id}>
              {s.name} · {s.source_id}
            </option>
          ))}
        </select>
      </Field>
      <h3>标签层与优先顺序</h3>
      {caption.layer_priority.map((layer, index) => (
        <div className={ui.row} key={layer}>
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={caption.tag_layers.includes(layer)}
              onChange={(e) =>
                setCaption({
                  tag_layers: e.target.checked
                    ? [...caption.tag_layers, layer]
                    : caption.tag_layers.filter((v) => v !== layer),
                })
              }
            />
            {labels[layer]}
          </label>
          <Button
            small
            disabled={index === 0}
            onClick={() => {
              const list = [...caption.layer_priority];
              [list[index - 1], list[index]] = [list[index]!, list[index - 1]!];
              setCaption({ layer_priority: list });
            }}
          >
            上移 {labels[layer]}
          </Button>
          <Button
            small
            disabled={index === 4}
            onClick={() => {
              const list = [...caption.layer_priority];
              [list[index], list[index + 1]] = [list[index + 1]!, list[index]!];
              setCaption({ layer_priority: list });
            }}
          >
            下移 {labels[layer]}
          </Button>
        </div>
      ))}
      <div className={ui.formGrid}>
        <Field id="tag-sort" label="标签排序">
          <select
            id="tag-sort"
            value={caption.tag_sort}
            onChange={(e) => setCaption({ tag_sort: e.target.value as typeof caption.tag_sort })}
          >
            <option value="layer_first">按标注层顺序（默认）</option>
            <option value="prompt_pyramid" disabled={!pyramidAvailable}>
              本地金字塔排序{pyramidAvailable ? '' : '（规则不可用）'}
            </option>
          </select>
        </Field>
        <Field id="wd-threshold" label="WD 最低置信度">
          <input
            id="wd-threshold"
            type="number"
            min="0"
            max="1"
            step="0.01"
            value={caption.wd_min_confidence}
            onChange={(e) => setCaption({ wd_min_confidence: e.target.valueAsNumber })}
          />
        </Field>
        <Field id="wd-null" label="WD 未提供置信度">
          <select
            id="wd-null"
            value={caption.wd_null_confidence}
            onChange={(e) => setCaption({ wd_null_confidence: e.target.value as 'include' | 'exclude' })}
          >
            <option value="exclude">排除</option>
            <option value="include">包含</option>
          </select>
        </Field>
        <Field id="blocklist-mode" label="作者与风格词命中">
          <select
            id="blocklist-mode"
            value={caption.blocklist_mode}
            onChange={(e) => setCaption({ blocklist_mode: e.target.value as 'warn' | 'remove' })}
          >
            <option value="warn">警告并保留</option>
            <option value="remove">移除标签；描述命中需人工处理</option>
          </select>
        </Field>
        <Field id="output-image-mode" label="图片导出方式">
          <select
            id="output-image-mode"
            value={output.image_mode}
            onChange={(e) => setOutput({ image_mode: e.target.value as typeof output.image_mode })}
          >
            <option value="copy">复制图片（默认）</option>
            <option value="hardlink">硬链接（共享原图内容）</option>
          </select>
        </Field>
        <Field id="output-filename" label="文件命名">
          <select
            id="output-filename"
            value={output.filename}
            onChange={(e) => setOutput({ filename: e.target.value as typeof output.filename })}
          >
            <option value="{image_id}{ext}">图片 ID + 扩展名</option>
            <option value="{stem}-{image_id}{ext}">项目文件名 + 图片 ID</option>
          </select>
        </Field>
        <Field id="output-directory" label="导出子目录">
          <input
            id="output-directory"
            placeholder="例如 training/data"
            value={output.subdirectory}
            onChange={(e) => setOutput({ subdirectory: e.target.value })}
          />
        </Field>
      </div>
      <p className={ui.muted}>目前检查完整标签及描述中的完整词组。更复杂的风格语法需人工复核。</p>
      {caption.tag_sort === 'prompt_pyramid' && (
        <p className={ui.muted}>
          触发词固定第一；内容按主体 → 动作表情 → 服装 → 构图 → 场景 →
          光照排序，只调整顺序。同类和未分类标签保持原相对顺序，未分类放在末尾；不推断多人物归属。
        </p>
      )}
      {output.image_mode === 'hardlink' && (
        <p className={ui.warning}>
          硬链接与项目原图共享文件内容，修改导出图片也会影响项目原图。仅限同卷且不使用后处理链；失败不会自动改为复制。TXT
          和 JSON 仍单独写入，每次导出前需确认。
        </p>
      )}
      <label className={ui.row}>
        <input
          type="checkbox"
          checked={caption.include_nl}
          onChange={(e) => setCaption({ include_nl: e.target.checked })}
        />
        追加自然语言（手动 → 语言模型 → 原始）
      </label>
      <label className={ui.row}>
        <input
          type="checkbox"
          checked={output.include_json}
          onChange={(e) => setOutput({ include_json: e.target.checked })}
        />
        导出 PNG 原始元数据 JSON
      </label>
      <label className={ui.row}>
        <input
          type="checkbox"
          checked={output.require_nonempty_tags}
          onChange={(e) => setOutput({ require_nonempty_tags: e.target.checked })}
        />
        要求存在触发词之外的标签
      </label>
      {(value.pipeline_json.length > 0 || (caption.tag_sort === 'prompt_pyramid' && !pyramidAvailable)) && (
        <p className={ui.warning}>此方案包含预留能力，当前无法执行。可创建一个新方案使用已支持的选项。</p>
      )}
    </>
  );
}
