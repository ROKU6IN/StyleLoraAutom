import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { allPages, request, waitOperation } from '../api/client';
import { pickDirectory, projectPath } from '../api/endpoints';
import { errorText } from '../api/errors';
import type {
  Accepted,
  Grant,
  ImportPreview,
  ImportReport,
  Source,
  SourceItem,
  Scheme,
  ImportConfiguration,
} from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, Field, HoldButton, Modal, TaskProgress, ui } from '../ui';
import GenerationSources from './GenerationSources';

const importWarning = (code: string) =>
  ({ 'encoding:gbk_fallback': 'UTF-8 解码失败，已使用 GBK，请核对中文' })[code] ?? code;

export default function Sources({ pid }: { pid: string }) {
  const connection = useSession((s) => s.connection),
    action = useAction();
  const startKey = useRef(crypto.randomUUID());
  const [grant, setGrant] = useState<Grant | null>(null),
    [preview, setPreview] = useState<ImportPreview | null>(null),
    [report, setReport] = useState<ImportReport | null>(null),
    [name, setName] = useState('本地图片'),
    [encoding, setEncoding] = useState('utf-8-sig'),
    [recursive, setRecursive] = useState(true),
    [detail, setDetail] = useState<Source | null>(null);
  const [schemeId, setSchemeId] = useState<number | null>(null);
  const [overrides, setOverrides] = useState(false);
  const schemes = useQuery({
    queryKey: projectKey(pid, 'import-schemes'),
    queryFn: ({ signal }) =>
      allPages<Scheme<ImportConfiguration>>(`${projectPath(pid)}/import-schemes`, signal),
  });
  const selected =
    schemes.data?.find((s) => s.scheme_id === schemeId) ?? schemes.data?.find((s) => s.is_default);
  const sources = useQuery({
    queryKey: projectKey(pid, 'sources'),
    queryFn: ({ signal }) => allPages<Source>(`${projectPath(pid)}/sources`, signal),
    refetchInterval: connection === 'reauth' ? false : 5000,
  });
  const rows = useQuery({
    queryKey: projectKey(pid, 'source-items', detail?.source_id),
    queryFn: ({ signal }) =>
      allPages<SourceItem>(`${projectPath(pid)}/sources/${detail!.source_id}/items`, signal),
    enabled: detail !== null,
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['project', pid] });
  const summary = useQuery({
    queryKey: projectKey(pid, 'import-summary', detail?.source_id),
    queryFn: ({ signal }) =>
      request<ImportReport>('GET', `${projectPath(pid)}/imports/${detail!.source_id}/summary`, undefined, {
        signal,
      }),
    enabled: detail?.type === 'import',
  });
  const importDetails = new Map(summary.data?.items.map((item) => [item.item_id, item]));
  const control = (source: Source, command: string) =>
    void action.run(async () => {
      const result = await request<Source | Accepted>(
        'POST',
        `${projectPath(pid)}/sources/${source.source_id}/control`,
        { action: command },
        { version: source.version },
      );
      if ('status_url' in result) useSession.getState().notify('任务已受理，可在任务进度查看状态');
      await refresh();
    });
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>COLLECTION / 02</div>
          <h1>来源与导入</h1>
          <p>导入本地图片与标注，或准备生成源并预览完整提示词交接。</p>
        </div>
      </div>
      <ErrorBox
        message={
          action.error ? errorText(action.error) : sources.error ? errorText(sources.error) : undefined
        }
      />
      <section className={ui.section}>
        <div className={ui.between}>
          <h2>导入本地目录</h2>
          <span className={ui.mono}>01 选择 → 02 预览 → 03 导入</span>
        </div>
        <div className={ui.formGrid}>
          <Field id="import-scheme" label="导入方案">
            <select
              id="import-scheme"
              disabled={action.busy}
              value={selected?.scheme_id ?? ''}
              onChange={(e) => {
                setSchemeId(Number(e.target.value));
                setOverrides(false);
                setPreview(null);
              }}
            >
              {schemes.data?.map((s) => (
                <option key={s.scheme_id} value={s.scheme_id}>
                  {s.name}
                  {s.is_default ? '（默认）' : ''}
                  {s.executable ? '' : '（暂不能执行）'}
                </option>
              ))}
            </select>
          </Field>
          <Field id="source-name" label="来源名称">
            <input
              id="source-name"
              disabled={action.busy}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                startKey.current = crypto.randomUUID();
              }}
              maxLength={512}
            />
          </Field>
          <Field id="encoding" label="TXT 编码">
            <select
              id="encoding"
              disabled={action.busy}
              value={overrides ? encoding : (selected?.configuration.params_json.encoding ?? encoding)}
              onChange={(e) => {
                if (!overrides) setRecursive(selected?.configuration.params_json.recursive ?? true);
                setOverrides(true);
                setEncoding(e.target.value);
                setPreview(null);
              }}
            >
              <option value="utf-8-sig">UTF-8（兼容 BOM）</option>
              <option value="utf-8">UTF-8</option>
              <option value="gbk">GBK</option>
              <option value="auto">UTF-8 优先，失败后尝试 GBK</option>
            </select>
          </Field>
        </div>
        {(overrides ? encoding : selected?.configuration.params_json.encoding) === 'auto' && (
          <p className={ui.muted}>
            带 BOM 的损坏 UTF-8 仍报错。GBK 解码成功不保证原文编码正确，请核对预览中的中文。
          </p>
        )}
        <div className={ui.between}>
          <label className={ui.row}>
            <input
              type="checkbox"
              disabled={action.busy}
              checked={overrides ? recursive : (selected?.configuration.params_json.recursive ?? recursive)}
              onChange={(e) => {
                if (!overrides) setEncoding(selected?.configuration.params_json.encoding ?? 'utf-8-sig');
                setOverrides(true);
                setRecursive(e.target.checked);
                setPreview(null);
              }}
            />
            包含子目录
          </label>
          <div className={ui.row}>
            <span className={ui.muted}>{grant?.display_name ?? '尚未选择目录'}</span>
            <Button
              disabled={action.busy || !canWrite()}
              onClick={() =>
                void action.run(async () => {
                  setPreview(null);
                  setReport(null);
                  setGrant(await pickDirectory('import_root', pid));
                })
              }
            >
              选择图片目录
            </Button>
            <Button
              primary
              disabled={!grant || !selected?.executable || action.busy || !canWrite()}
              onClick={() =>
                void action.run(async () => {
                  if (!grant) return;
                  setPreview(null);
                  setReport(null);
                  const accepted = await request<Accepted>('POST', `${projectPath(pid)}/imports/preview`, {
                    root_handle: grant.grant_id,
                    scheme_id: selected?.scheme_id,
                    ...(overrides ? { options: { encoding, recursive } } : {}),
                  });
                  startKey.current = crypto.randomUUID();
                  setPreview(await waitOperation<ImportPreview>(accepted));
                })
              }
            >
              {action.busy ? '处理中…' : '扫描并预览'}
            </Button>
          </div>
        </div>
      </section>
      {preview && (
        <section className={ui.section}>
          <div className={ui.between}>
            <div>
              <h2>导入预览 · {preview.selected} 张</h2>
              <p className={ui.muted}>已冻结文件内容与目录清单。文件发生变化时，请重新扫描。</p>
            </div>
            <Button
              primary
              disabled={action.busy || !preview.can_import || !name.trim() || !canWrite()}
              onClick={() =>
                void action.run(async () => {
                  const accepted = await request<Accepted>(
                    'POST',
                    `${projectPath(pid)}/imports`,
                    {
                      preview_id: preview.preview_id,
                      digest: preview.digest,
                      name: name.trim(),
                    },
                    { key: startKey.current },
                  );
                  setPreview(null);
                  useSession.getState().notify('导入已开始，任务进度可暂停或取消');
                  setReport(await waitOperation<ImportReport>(accepted));
                  await refresh();
                })
              }
            >
              确认导入
            </Button>
          </div>
          {preview.blocking_errors > 0 && (
            <p role="alert" className={ui.warning}>
              {preview.blocking_errors} 项解析或文件错误，请修正后重新扫描。
            </p>
          )}
          <p className={ui.muted}>
            {preview.catalog_revision ? '已固定本地词库版本。' : '兼容模式：首行全部作为标签。'}
          </p>
          {preview.encoding_fallback_count > 0 && (
            <p className={ui.warning}>
              共 {preview.encoding_fallback_count} 项使用 GBK 回退（含未显示项），请核对中文后再导入。
            </p>
          )}
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>相对路径</th>
                  <th>预检</th>
                  <th>实际编码</th>
                  <th>解析预览</th>
                </tr>
              </thead>
              <tbody>
                {preview.diagnostics.map((entry) => (
                  <tr key={entry.path}>
                    <td>{entry.path}</td>
                    <td>
                      {entry.error ??
                        (entry.warnings?.length ? entry.warnings.map(importWarning).join('、') : '通过')}
                    </td>
                    <td>{entry.encoding ?? '—'}</td>
                    <td>
                      {entry.error &&
                        entry.segments
                          ?.filter((t) => t.reason)
                          .map((t, index) => (
                            <p key={`error-${index}`} className={ui.warning}>
                              {t.raw} · {t.reason}
                            </p>
                          ))}
                      {entry.originals?.map((row, index) => (
                        <p key={index}>
                          {typeof row.content === 'string'
                            ? row.content
                            : row.content.map((t) => t.tag).join(', ')}
                        </p>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.manifest.entries.length > 100 && (
            <p className={ui.muted}>
              最多显示 100 项（错误优先）；导入将处理全部 {preview.selected}{' '}
              项。标签/描述显示节选，原始证据完整保存。
            </p>
          )}
        </section>
      )}
      {report && (
        <div role="status" className={ui.notice}>
          导入结束：成功 {report.imported}，跳过 {report.skipped}，失败 {report.failed}，待处理{' '}
          {report.pending}。 <Link to={`/p/${pid}/workbench`}>前往工作台 →</Link>
        </div>
      )}
      <GenerationSources key={pid} pid={pid} />
      <section className={ui.stack}>
        <div className={ui.between}>
          <h2>数据来源</h2>
          <span className={ui.muted}>{sources.data?.length ?? 0} 个来源</span>
        </div>
        {sources.data?.length ? (
          <div className={ui.cards}>
            {sources.data.map((source) => (
              <article className={ui.section} key={source.source_id}>
                <div className={ui.between}>
                  <h3>{source.name}</h3>
                  <span className={ui.tag}>
                    {(
                      {
                        draft: '草稿',
                        running: '运行中',
                        paused: '已暂停',
                        done: '已完成',
                        canceled: '已取消',
                      } as Record<string, string>
                    )[source.status] ?? source.status}
                  </span>
                </div>
                <TaskProgress
                  done={source.counts.done + source.counts.failed + source.counts.skipped}
                  total={Object.values(source.counts).reduce((a, b) => a + b, 0)}
                  label={`来源 #${source.source_id}`}
                />
                <p className={ui.muted}>
                  {source.type === 'nai' ? '已生成' : '已导入'} {source.counts.done} · 失败{' '}
                  {source.counts.failed} · 跳过 {source.counts.skipped}
                </p>
                <div className={ui.row}>
                  <Button small onClick={() => setDetail(source)}>
                    查看明细
                  </Button>
                  {source.status === 'running' && (
                    <Button
                      small
                      disabled={action.busy || !canWrite()}
                      onClick={() => control(source, 'pause')}
                    >
                      暂停
                    </Button>
                  )}
                  {['paused', 'draft'].includes(source.status) &&
                    !(source.type === 'nai' && source.status === 'draft') && (
                      <Button
                        small
                        disabled={action.busy || !canWrite()}
                        onClick={() => control(source, 'resume')}
                      >
                        {source.type === 'nai' ? '继续生成' : '继续导入'}
                      </Button>
                    )}
                  {source.type === 'import' && ['paused', 'done'].includes(source.status) && (
                    <Button
                      small
                      disabled={action.busy || !canWrite()}
                      onClick={() =>
                        void action.run(async () => {
                          const root = await pickDirectory('import_root', pid);
                          const accepted = await request<Accepted>(
                            'POST',
                            `${projectPath(pid)}/sources/${source.source_id}/import-root`,
                            { root_handle: root.grant_id },
                            { version: source.version },
                          );
                          await waitOperation(accepted);
                          await refresh();
                        })
                      }
                    >
                      重新绑定目录
                    </Button>
                  )}
                  {['running', 'paused', 'draft'].includes(source.status) && (
                    <HoldButton
                      disabled={action.busy || !canWrite()}
                      onConfirm={() => control(source, 'cancel')}
                    >
                      取消来源
                    </HoldButton>
                  )}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon="folder"
            title="还没有数据来源"
            description="选择图片目录开始导入，或在上方保存生成源草稿。"
          />
        )}
      </section>
      <Modal
        open={Boolean(detail)}
        onOpenChange={(open) => {
          if (!open) setDetail(null);
        }}
        title={detail?.name ?? '来源明细'}
        description="显示来源条目的状态与失败原因。失败条目不会被静默重试。"
        wide
      >
        <ErrorBox
          message={rows.error ? errorText(rows.error) : summary.error ? errorText(summary.error) : undefined}
        />
        {detail?.type === 'import' && summary.isFetching && <p className={ui.muted}>正在读取导入编码记录…</p>}
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th>序号</th>
                <th>状态</th>
                <th>图片 ID</th>
                <th>错误</th>
                {detail?.type === 'import' && <th>实际编码 / 提示</th>}
              </tr>
            </thead>
            <tbody>
              {rows.data?.slice(0, 500).map((row) => (
                <tr key={row.item_id}>
                  <td>{row.seq}</td>
                  <td>{row.status}</td>
                  <td>{row.image_id ?? '—'}</td>
                  <td>{row.error ?? '—'}</td>
                  {detail?.type === 'import' && (
                    <td>
                      {importDetails.get(row.item_id)?.encoding ?? '—'}
                      {importDetails.get(row.item_id)?.warnings.map((warning) => (
                        <p key={warning} className={ui.muted}>
                          {importWarning(warning)}
                        </p>
                      ))}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={ui.muted}>共 {rows.data?.length ?? 0} 项，页面显示前 500 项。</p>
        <div className={ui.actions}>
          <Button onClick={() => setDetail(null)}>关闭</Button>
        </div>
      </Modal>
    </div>
  );
}
