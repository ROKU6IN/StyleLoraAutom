import { useRef, useState } from 'react';
import { request } from '../../api/client';
import { AppError, errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { queryClient } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, Field, ui } from '../../ui';
import type { LibraryPreview } from './types';

export function LibraryImport({ rules = false }: { rules?: boolean }) {
  const [kind, setKind] = useState('catalog'),
    [format, setFormat] = useState(rules ? 'tagcleaner' : 'json');
  const [file, setFile] = useState<File | null>(null),
    [preview, setPreview] = useState<LibraryPreview | null>(null);
  const retryKey = useRef(crypto.randomUUID());
  const action = useAction();
  useSession((s) => s.connection);
  const path = rules ? '/api/rules/imports' : '/api/tags/imports';
  const reset = () => {
    setPreview(null);
    action.setError(null);
    retryKey.current = crypto.randomUUID();
  };
  return (
    <section className={ui.section}>
      <h2>{rules ? '迁入规则库' : '导入本地标签资料'}</h2>
      <p className={ui.muted}>
        UTF-8 文件，最多 10 MiB。规范标签库和规则库将整体替换；翻译合并，空翻译跳过。
      </p>
      <fieldset className={ui.stack} disabled={action.busy}>
        <legend>选择导入资料</legend>
        {!rules && (
          <Field id="library-kind" label="资料类型">
            <select
              id="library-kind"
              value={kind}
              onChange={(e) => {
                setKind(e.target.value);
                setFormat('json');
                reset();
              }}
            >
              <option value="catalog">规范标签与别名</option>
              <option value="translations">中文翻译</option>
            </select>
          </Field>
        )}
        <Field id="library-format" label="文件格式">
          <select
            id="library-format"
            value={format}
            onChange={(e) => {
              setFormat(e.target.value);
              reset();
            }}
          >
            <option value="json">JSON</option>
            {rules && <option value="tagcleaner">TagCleaner 规则 JSON</option>}
            {kind === 'translations' && !rules && <option value="tsv">TSV · 标签与翻译两列</option>}
          </select>
        </Field>
        <Field id="library-file" label="本地资料文件">
          <input
            id="library-file"
            type="file"
            accept=".json,.tsv,.txt"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              reset();
            }}
          />
        </Field>
      </fieldset>
      <ErrorBox message={action.error ? errorText(action.error) : undefined} />
      {preview && (
        <section aria-label="资料导入预览" className={ui.stack}>
          <strong>
            {preview.mode === 'replace' ? '整体替换' : '合并'} {preview.count} 项 · 跳过 {preview.skipped} 项
          </strong>
          {!!preview.issues.length && (
            <p className={ui.warning}>
              迁移后，条件使用项目原始标签，删除使用当前有效标签；与 TagCleaner
              旧执行语义不同。请逐条预览后应用。
            </p>
          )}
          <details>
            <summary>查看前 {preview.examples.length} 项</summary>
            <pre className={ui.scroll}>{JSON.stringify(preview.examples, null, 2)}</pre>
          </details>
        </section>
      )}
      <div className={ui.actions}>
        <Button
          disabled={!file || action.busy || !canWrite()}
          onClick={() =>
            void action.run(async () => {
              reset();
              if (!file) return;
              if (file.size > 10 * 1024 * 1024) throw new AppError(413, 'catalog:too_large');
              const body = new FormData();
              body.append('file', file);
              body.append('kind', rules ? 'rules' : kind);
              body.append('format', format);
              setPreview(await request<LibraryPreview>('POST', `${path}/preview`, body));
            })
          }
        >
          预览资料导入
        </Button>
        <Button
          primary
          disabled={!preview || action.busy || !canWrite()}
          onClick={() =>
            void action.run(async () => {
              if (!preview) return;
              await request(
                'POST',
                `${path}/commit`,
                { preview_id: preview.preview_id, digest: preview.digest },
                { version: preview.version, key: retryKey.current },
              );
              setPreview(null);
              await Promise.all([
                queryClient.invalidateQueries({ queryKey: ['catalog'] }),
                queryClient.invalidateQueries({ queryKey: ['rules'] }),
              ]);
              useSession.getState().notify('本地资料已更新');
            })
          }
        >
          确认导入资料
        </Button>
      </div>
    </section>
  );
}
