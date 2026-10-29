import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import { projectPath } from '../../api/endpoints';
import { errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { queryClient, projectKey } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, Field, Modal, ui } from '../../ui';
import { RuleFields } from './RuleFields';
import {
  emptyRule,
  splitTags,
  displayTags,
  type BatchPreview,
  type Library,
  type Scope,
  type TagHit,
} from './types';

export function BatchDialog({
  pid,
  kind,
  scope,
  onClose,
}: {
  pid: string;
  kind: 'bulk' | 'rule' | 'stats';
  scope: Scope;
  onClose: () => void;
}) {
  const [rule, setRule] = useState(emptyRule),
    [chosen, setChosen] = useState(''),
    [editorKey, setEditorKey] = useState(0);
  const [add, setAdd] = useState(''),
    [remove, setRemove] = useState('');
  const [preview, setPreview] = useState<BatchPreview | null>(null);
  const retryKey = useRef(crypto.randomUUID());
  const action = useAction();
  useSession((s) => s.connection);
  const library = useQuery({
    queryKey: ['rules'],
    queryFn: () => request<Library>('GET', '/api/rules'),
    enabled: kind === 'rule',
  });
  const stats = useQuery({
    queryKey: projectKey(pid, 'tag-stats', scope),
    queryFn: () =>
      request<{
        total_images: number;
        unique_tags: number;
        truncated: boolean;
        items: (TagHit & { tag: string; count: number })[];
      }>('POST', `${projectPath(pid)}/annotations/stats`, { scope }),
    enabled: kind === 'stats',
  });
  const invalidate = () => {
    setPreview(null);
    action.setError(null);
    retryKey.current = crypto.randomUUID();
  };
  const path = `${projectPath(pid)}${kind === 'rule' ? '/rules' : '/annotations/manual/bulk'}`;
  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open && !action.busy) onClose();
      }}
      wide
      title={kind === 'rule' ? '替换规则预览' : kind === 'bulk' ? '批量标签编辑' : '标签频次统计'}
      description="操作范围来自打开窗口时的选择；一次最多 5000 张。"
    >
      {kind === 'stats' ? (
        <>
          <p>
            {stats.data?.total_images ?? '…'} 张图片 · {stats.data?.unique_tags ?? '…'} 个有效标签。
            {stats.data?.truncated && '仅显示频次最高的 1000 项。'}
          </p>
          <ErrorBox message={stats.error ? errorText(stats.error) : undefined} />
          <div className={ui.scroll}>
            <table>
              <thead>
                <tr>
                  <th>标签</th>
                  <th>翻译</th>
                  <th>图片数</th>
                </tr>
              </thead>
              <tbody>
                {stats.data?.items.map((t) => (
                  <tr key={t.tag}>
                    <td>{t.tag}</td>
                    <td>{t.translation ?? '—'}</td>
                    <td>{t.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div className={ui.stack}>
          {kind === 'rule' ? (
            <>
              <Field id="saved-rule" label="使用已保存规则">
                <select
                  id="saved-rule"
                  disabled={action.busy}
                  value={chosen}
                  onChange={(e) => {
                    const id = e.target.value;
                    setChosen(id);
                    setRule(library.data?.rules.find((r) => r.id === id)?.rule ?? emptyRule());
                    setEditorKey((k) => k + 1);
                    invalidate();
                  }}
                >
                  <option value="">临时规则</option>
                  {library.data?.rules.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </select>
              </Field>
              <RuleFields
                key={editorKey}
                value={rule}
                disabled={action.busy}
                onChange={(r) => {
                  setRule(r);
                  invalidate();
                }}
              />
            </>
          ) : (
            <fieldset disabled={action.busy} className={ui.stack}>
              <legend>标签增删</legend>
              <Field id="bulk-add" label="批量新增标签">
                <input
                  id="bulk-add"
                  value={add}
                  onChange={(e) => {
                    setAdd(e.target.value);
                    invalidate();
                  }}
                />
              </Field>
              <Field id="bulk-remove" label="批量删除标签">
                <input
                  id="bulk-remove"
                  value={remove}
                  onChange={(e) => {
                    setRemove(e.target.value);
                    invalidate();
                  }}
                />
              </Field>
            </fieldset>
          )}
          <ErrorBox message={action.error ? errorText(action.error) : undefined} />
          {preview && (
            <section aria-label="变更预览" className={ui.stack}>
              <strong>
                范围 {preview.total} 张 · 将更改 {preview.changed_count} 张
              </strong>
              {kind === 'rule' && (
                <p className={ui.muted}>
                  规则命中 {preview.matched_count} 张；使用 WD 条件回退 {preview.wd_fallback_count}{' '}
                  张；无原始标签 {preview.missing_original_count} 张。默认查询范围排除剔除项。
                </p>
              )}
              <div className={ui.scroll}>
                <table>
                  <thead>
                    <tr>
                      <th>图片</th>
                      <th>之前</th>
                      <th>之后</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.examples.map((row) => (
                      <tr key={row.image_id}>
                        <td>#{row.image_id}</td>
                        <td>{displayTags(row.before)}</td>
                        <td>{displayTags(row.after)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className={ui.muted}>
                展示前 {preview.examples.length} 个样本。确认使用完整冻结读集；冲突时整批拒绝，请重新预览。
              </p>
            </section>
          )}
          <div className={ui.actions}>
            <Button disabled={action.busy} onClick={onClose}>
              取消
            </Button>
            <Button
              disabled={
                action.busy ||
                !canWrite() ||
                (kind === 'rule'
                  ? !rule.delete && !rule.write
                  : !splitTags(add).length && !splitTags(remove).length)
              }
              onClick={() =>
                void action.run(async () => {
                  setPreview(null);
                  retryKey.current = crypto.randomUUID();
                  setPreview(
                    await request<BatchPreview>(
                      'POST',
                      `${path}/preview`,
                      kind === 'rule'
                        ? { rule, scope }
                        : { scope, add: splitTags(add), remove: splitTags(remove) },
                    ),
                  );
                })
              }
            >
              生成变更预览
            </Button>
            <Button
              primary
              disabled={!preview || action.busy || !canWrite()}
              onClick={() =>
                void action.run(async () => {
                  if (!preview) return;
                  await request(
                    'POST',
                    `${path}/apply`,
                    { preview_id: preview.preview_id, digest: preview.digest },
                    { version: preview.version, key: retryKey.current },
                  );
                  await queryClient.invalidateQueries({ queryKey: ['project', pid] });
                  useSession
                    .getState()
                    .notify(`已处理 ${preview.total} 张，更改 ${preview.changed_count} 张`);
                  onClose();
                })
              }
            >
              确认应用
            </Button>
          </div>
        </div>
      )}
      {kind === 'stats' && (
        <div className={ui.actions}>
          <Button onClick={onClose}>关闭</Button>
        </div>
      )}
    </Modal>
  );
}
