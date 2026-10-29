import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { allPages, request, waitOperation } from '../api/client';
import { projectPath } from '../api/endpoints';
import { errorText, errorCodeText } from '../api/errors';
import type {
  Accepted,
  ExportPreview,
  ExportReport,
  ExportView,
  Project,
  Scheme,
  ExportConfiguration,
} from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, Field, HoldButton, Modal, TaskProgress, ui } from '../ui';
import ExportRecovery from './ExportRecovery';
export default function Exports({ project }: { project: Project }) {
  const pid = project.project_id,
    action = useAction(),
    connection = useSession((s) => s.connection);
  const startKey = useRef(crypto.randomUUID());
  const [nl, setNl] = useState(false),
    [unreviewed, setUnreviewed] = useState(false),
    [json, setJson] = useState(false),
    [preview, setPreview] = useState<ExportPreview | null>(null),
    [report, setReport] = useState<ExportReport | null>(null);
  const [schemeId, setSchemeId] = useState<number | null>(null),
    [overrides, setOverrides] = useState(false),
    [acknowledged, setAcknowledged] = useState(false),
    [hardlinkAcknowledged, setHardlinkAcknowledged] = useState(false);
  const schemes = useQuery({
    queryKey: projectKey(pid, 'export-schemes'),
    queryFn: ({ signal }) =>
      allPages<Scheme<ExportConfiguration>>(`${projectPath(pid)}/export-schemes`, signal),
  });
  const selected = schemes.data?.find((s) => s.scheme_id === schemeId) ?? schemes.data?.[0];
  const flags = overrides
    ? { nl, unreviewed, json }
    : {
        nl: selected?.configuration.caption_config_json.include_nl ?? false,
        unreviewed: selected?.configuration.filter_json.review_statuses.includes('unreviewed') ?? false,
        json: selected?.configuration.output_config_json.include_json ?? false,
      };
  const exports = useQuery({
    queryKey: projectKey(pid, 'exports'),
    queryFn: ({ signal }) => allPages<ExportView>(`${projectPath(pid)}/exports`, signal),
    refetchInterval: connection === 'reauth' ? false : 5000,
  });
  const dirtyOptions = (setter: (v: boolean) => void, value: boolean) => {
    if (!overrides) {
      setNl(flags.nl);
      setUnreviewed(flags.unreviewed);
      setJson(flags.json);
    }
    setOverrides(true);
    setter(value);
    setPreview(null);
  };
  const ready = preview?.images.filter((i) => i.status === 'ready').length ?? 0;
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>TRAINING DATA / 03</div>
          <h1>导出数据集</h1>
          <p>预览最终标签，再生成图片与同名 TXT。每次导出写入项目内的新目录。</p>
        </div>
      </div>
      <ErrorBox
        message={
          action.error ? errorText(action.error) : exports.error ? errorText(exports.error) : undefined
        }
      />
      <ExportRecovery key={pid} pid={pid} />
      <section className={ui.section}>
        <div className={ui.between}>
          <h2>图片 + TXT</h2>
          <span className={ui.tag}>触发词 · {project.metadata.trigger_word || '尚未设置'}</span>
        </div>
        <div className={ui.stack}>
          <Field id="export-scheme" label="导出方案">
            <select
              id="export-scheme"
              value={selected?.scheme_id ?? ''}
              disabled={action.busy}
              onChange={(e) => {
                setSchemeId(Number(e.target.value));
                setOverrides(false);
                setPreview(null);
              }}
            >
              {schemes.data?.map((s) => (
                <option key={s.scheme_id} value={s.scheme_id}>
                  {s.name}
                  {s.executable ? '' : '（暂不能执行）'}
                </option>
              ))}
            </select>
          </Field>
          {selected?.configuration.output_config_json.image_mode === 'hardlink' && (
            <p className={ui.warning}>
              当前方案使用硬链接：修改导出图片也会影响项目原图。仅支持同卷，失败不会改为复制。
            </p>
          )}
          <label className={ui.row}>
            <input
              type="checkbox"
              disabled={action.busy}
              checked={flags.unreviewed}
              onChange={(e) => dirtyOptions(setUnreviewed, e.target.checked)}
            />
            包含未审核图片（默认只导出保留项）
          </label>
          <label className={ui.row}>
            <input
              type="checkbox"
              disabled={action.busy}
              checked={flags.nl}
              onChange={(e) => dirtyOptions(setNl, e.target.checked)}
            />
            追加自然语言描述
          </label>
          <label className={ui.row}>
            <input
              type="checkbox"
              disabled={action.busy}
              checked={flags.json}
              onChange={(e) => dirtyOptions(setJson, e.target.checked)}
            />
            附带 JSON 元数据
          </label>
          <div className={ui.between}>
            <span className={ui.muted}>剔除项与缺失文件会跳过。触发词位于每份标签开头。</span>
            <Button
              primary
              disabled={action.busy || !selected?.executable || !canWrite() || !project.metadata.trigger_word}
              onClick={() =>
                void action.run(async () => {
                  setPreview(null);
                  setAcknowledged(false);
                  setHardlinkAcknowledged(false);
                  startKey.current = crypto.randomUUID();
                  const accepted = await request<Accepted>('POST', `${projectPath(pid)}/exports/preview`, {
                    scheme_id: selected?.scheme_id,
                    ...(overrides
                      ? {
                          options: {
                            include_nl: flags.nl,
                            include_unreviewed: flags.unreviewed,
                            include_json: flags.json,
                          },
                        }
                      : {}),
                  });
                  setPreview(await waitOperation<ExportPreview>(accepted));
                })
              }
            >
              {action.busy ? '处理中…' : '生成导出预览'}
            </Button>
          </div>
        </div>
      </section>
      {preview && (
        <section className={ui.section}>
          <div className={ui.between}>
            <div>
              <h2>导出预览</h2>
              <p>
                {ready} 张可导出 · {preview.images.filter((i) => i.status === 'skipped').length} 张跳过 ·{' '}
                {preview.images.filter((i) => i.status === 'failed').length} 张失败
              </p>
            </div>
            <Button
              primary
              disabled={
                !ready ||
                action.busy ||
                !canWrite() ||
                (preview.trigger_validation.can_override && !acknowledged) ||
                (preview.output_config.image_mode === 'hardlink' && !hardlinkAcknowledged)
              }
              onClick={() =>
                void action.run(async () => {
                  await request<ExportView>(
                    'POST',
                    `${projectPath(pid)}/exports`,
                    {
                      preview_id: preview.preview_id,
                      digest: preview.digest,
                      ...(hardlinkAcknowledged ? { hardlink_ack_digest: preview.hardlink_ack_digest } : {}),
                      ...(acknowledged
                        ? { trigger_override_digest: preview.trigger_validation.override_digest }
                        : {}),
                    },
                    { key: startKey.current },
                  );
                  setPreview(null);
                  useSession.getState().notify('导出已开始，可在下方或任务进度查看结果');
                  await queryClient.invalidateQueries({ queryKey: projectKey(pid, 'exports') });
                })
              }
            >
              确认导出 {ready} 张
            </Button>
          </div>
          <p className={ui.muted}>
            图片导出方式：
            {preview.output_config.image_mode === 'hardlink' ? '硬链接（共享原图内容）' : '复制图片'}
          </p>
          <p className={ui.muted}>
            标签排序：
            {preview.sorter ? '本地金字塔排序 · 主体 → 动作表情 → 服装 → 构图 → 场景 → 光照' : '按标注层顺序'}
          </p>
          {preview.output_config.image_mode === 'hardlink' && (
            <label className={`${ui.row} ${ui.warning}`}>
              <input
                type="checkbox"
                checked={hardlinkAcknowledged}
                disabled={action.busy}
                onChange={(e) => setHardlinkAcknowledged(e.target.checked)}
              />
              我已了解修改导出图片也会影响项目原图，确认本次使用硬链接
            </label>
          )}
          {preview.trigger_validation.issues.some((i) => i.code === 'trigger:unverifiable') && (
            <p className={ui.warning}>词库覆盖或风格语法无法完整验证，不能确认触发词唯一性；请人工复核。</p>
          )}
          {preview.trigger_validation.can_override && (
            <label className={`${ui.row} ${ui.warning}`}>
              <input
                type="checkbox"
                checked={acknowledged}
                disabled={action.busy}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              已了解触发词与现有标签重合，继续导出
            </label>
          )}
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>图片</th>
                  <th>结果</th>
                  <th>最终标签 / 描述</th>
                </tr>
              </thead>
              <tbody>
                {preview.images.slice(0, 100).map((image) => (
                  <tr key={image.image_id}>
                    <td>{image.output_path ?? image.filename}</td>
                    <td>{image.status === 'ready' ? '可导出' : (image.reason ?? image.status)}</td>
                    <td>
                      {image.tags.join(', ')}
                      {image.natural_language && <p>{image.natural_language}</p>}
                      {!!image.sorting_counts?.unclassified && (
                        <p className={ui.muted}>
                          未分类 {image.sorting_counts.unclassified} 项，按原相对顺序放在末尾。
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.images.length > 100 && <p className={ui.muted}>显示前 100 项；确认后处理全部预览结果。</p>}
        </section>
      )}
      <section className={ui.stack}>
        <h2>导出记录</h2>
        {exports.data?.length ? (
          exports.data.map((row) => (
            <article className={ui.section} key={row.export_id}>
              <div className={ui.between}>
                <div>
                  <h3>
                    {row.status === 'cleaned'
                      ? '导出文件已清理'
                      : row.status === 'cleanup_partial'
                        ? '导出清理未完成'
                        : row.status === 'finished'
                          ? '导出完成'
                          : row.status === 'failed'
                            ? '导出失败'
                            : row.status === 'interrupted'
                              ? '导出中断'
                              : row.status === 'canceled'
                                ? '已取消'
                                : '导出中'}
                  </h3>
                  <span className={ui.mono}>{row.export_id}</span>
                </div>
                <div className={ui.row}>
                  {row.report_url && (
                    <Button
                      onClick={() =>
                        void action.run(async () =>
                          setReport(await request<ExportReport>('GET', row.report_url!)),
                        )
                      }
                    >
                      查看报告
                    </Button>
                  )}
                  {['pending', 'running'].includes(row.status) && (
                    <HoldButton
                      disabled={action.busy || !canWrite()}
                      onConfirm={() =>
                        void action.run(async () => {
                          await request(
                            'POST',
                            `${projectPath(pid)}/exports/${row.export_id}/cancel`,
                            {},
                            { version: row.version },
                          );
                          await queryClient.invalidateQueries({ queryKey: projectKey(pid, 'exports') });
                        })
                      }
                    >
                      取消导出
                    </HoldButton>
                  )}
                </div>
              </div>
              <TaskProgress
                done={row.exported + row.skipped + row.failed}
                total={row.selected}
                label="导出进度"
              />
              <p className={ui.muted}>
                成功 {row.exported} · 跳过 {row.skipped} · 失败 {row.failed}
              </p>
              {row.relative_dir && <p className={ui.caption}>项目目录 / {row.relative_dir}</p>}
              {row.image_mode === 'hardlink' && (
                <p className={ui.warning}>
                  硬链接导出，共享项目原图内容。
                  {['interrupted', 'canceled'].includes(row.status) &&
                    '临时目录可能保留共享图片，修改前请核对。'}
                </p>
              )}
            </article>
          ))
        ) : (
          <EmptyState icon="export" title="还没有导出记录" description="完成审核后，先生成导出预览。" />
        )}
      </section>
      <Modal
        open={Boolean(report)}
        onOpenChange={(open) => {
          if (!open) setReport(null);
        }}
        title="导出报告"
        description="以下结果来自已写入项目的导出报告。"
      >
        <div className={ui.stack}>
          <p>
            成功 {report?.counts.exported} · 跳过 {report?.counts.skipped} · 失败 {report?.counts.failed}
          </p>
          <div className={ui.caption}>项目目录 / {report?.directory}</div>
          <p>标签排序：{report?.sorter ? '本地金字塔排序' : '按标注层顺序'}</p>
          {!!report?.sort_summary?.unclassified_tag_occurrences && (
            <p className={ui.muted}>
              已导出图片中，{report.sort_summary.images_with_unclassified} 张含未分类标签，共{' '}
              {report.sort_summary.unclassified_tag_occurrences} 项（按图片计数），已保留并稳定放在末尾。
            </p>
          )}
          <p>图片导出方式：{report?.output_config?.image_mode === 'hardlink' ? '硬链接' : '复制图片'}</p>
          {report?.output_config?.image_mode === 'hardlink' && (
            <p className={ui.warning}>
              此导出与项目原图共享文件内容；修改任一侧的图片会影响另一侧。文件哈希是导出时的记录。
            </p>
          )}
          {Object.entries(report?.reason_counts ?? {}).map(([reason, count]) => (
            <p key={reason}>
              {reason.startsWith('export:hardlink_') ? errorCodeText(reason) : reason}：{count}
            </p>
          ))}
          <div className={ui.actions}>
            <Button onClick={() => setReport(null)}>关闭</Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
