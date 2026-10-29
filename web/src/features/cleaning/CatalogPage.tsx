import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import type { Page } from '../../api/types';
import { errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { queryClient } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, Field, ui } from '../../ui';
import { LibraryImport } from './LibraryImport';
import type { CatalogStatus, TagHit } from './types';

export default function CatalogPage() {
  const status = useQuery({
    queryKey: ['catalog', 'status'],
    queryFn: () => request<CatalogStatus>('GET', '/api/tags/catalog-status'),
  });
  const [query, setQuery] = useState(''),
    [search, setSearch] = useState('');
  const [edit, setEdit] = useState<{ tag: string; value: string; version: string; key: string } | null>(null);
  const hits = useQuery({
    queryKey: ['catalog', 'search', search],
    queryFn: () =>
      request<Page<TagHit> & { version: string }>(
        'GET',
        `/api/tags/search?q=${encodeURIComponent(search)}&limit=100`,
      ),
  });
  const action = useAction();
  useSession((s) => s.connection);
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>LOCAL TAG LIBRARY</div>
          <h1>本地标签库</h1>
          <p>补全与中文翻译只提供编辑参考；更新资料不会改写项目已有标注。</p>
        </div>
      </div>
      <ErrorBox message={status.error ? errorText(status.error) : undefined} />
      <section className={ui.section}>
        <h2>资料覆盖情况</h2>
        <p>
          {status.data?.tag_count ?? 0} 个规范标签 · {status.data?.alias_count ?? 0} 个别名 ·{' '}
          {status.data?.translation_count ?? 0} 项翻译
        </p>
        <p className={ui.muted}>
          {status.data?.coverage === 'provider_declared_complete'
            ? '导入文件声明为完整库，此声明未经外部核验。'
            : status.data?.available
              ? '当前为局部词库，未命中不代表标签无效。'
              : '尚无词库，标签是否已知暂不可验证。'}
        </p>
      </section>
      <LibraryImport />
      <section className={ui.section}>
        <h2>查询与维护翻译</h2>
        <div className={ui.row}>
          <Field id="catalog-query" label="查询标签或中文">
            <input id="catalog-query" value={query} onChange={(e) => setQuery(e.target.value)} />
          </Field>
          <Button onClick={() => setSearch(query)}>查询</Button>
          <Button
            disabled={!status.data || !query.trim()}
            onClick={() =>
              setEdit({ tag: query, value: '', version: status.data!.version, key: crypto.randomUUID() })
            }
          >
            为输入标签编辑翻译
          </Button>
        </div>
        <ErrorBox message={hits.error ? errorText(hits.error) : undefined} />
        {hits.data?.has_more && <p className={ui.muted}>显示前 100 项，请输入关键词缩小范围。</p>}
        <div className={ui.scroll}>
          <table>
            <thead>
              <tr>
                <th>规范标签</th>
                <th>翻译</th>
                <th>分类</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {hits.data?.items.map((hit) => (
                <tr key={hit.canonical}>
                  <td>{hit.canonical}</td>
                  <td>{hit.translation ?? '—'}</td>
                  <td>{hit.category ?? '—'}</td>
                  <td>
                    <Button
                      small
                      onClick={() => {
                        setEdit({
                          tag: hit.canonical!,
                          value: hit.translation ?? '',
                          version: hits.data.version,
                          key: crypto.randomUUID(),
                        });
                        action.setError(null);
                      }}
                    >
                      编辑翻译
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {edit && (
          <section className={ui.stack}>
            <h3>翻译：{edit.tag}</h3>
            <Field id="translation" label="中文翻译（留空表示删除）">
              <input
                id="translation"
                value={edit.value}
                disabled={action.busy}
                onChange={(e) => setEdit({ ...edit, value: e.target.value, key: crypto.randomUUID() })}
              />
            </Field>
            <ErrorBox message={action.error ? errorText(action.error) : undefined} />
            <div className={ui.actions}>
              <Button disabled={action.busy} onClick={() => setEdit(null)}>
                取消翻译编辑
              </Button>
              <Button
                primary
                disabled={action.busy || !canWrite()}
                onClick={() =>
                  void action.run(async () => {
                    await request(
                      'PUT',
                      '/api/translations',
                      { tag: edit.tag, value: edit.value },
                      { version: edit.version, key: edit.key },
                    );
                    setEdit(null);
                    await queryClient.invalidateQueries({ queryKey: ['catalog'] });
                  })
                }
              >
                保存翻译
              </Button>
            </div>
          </section>
        )}
      </section>
    </div>
  );
}
