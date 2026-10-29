import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import type { Page } from '../../api/types';
import type { TagHit } from './types';
import { Button, Field, ui } from '../../ui';

export function TagSuggestions({
  onAdd,
  disabled,
  id = 'tag-suggestion',
}: {
  onAdd: (tag: string) => void;
  disabled?: boolean;
  id?: string;
}) {
  const [text, setText] = useState(''),
    [query, setQuery] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setQuery(text), 250);
    return () => clearTimeout(t);
  }, [text]);
  const hits = useQuery({
    queryKey: ['catalog', 'suggest', query],
    queryFn: ({ signal }) =>
      request<Page<TagHit> & { available: boolean }>(
        'GET',
        `/api/tags/search?q=${encodeURIComponent(query)}&limit=8`,
        undefined,
        { signal },
      ),
    enabled: Boolean(query.trim()),
  });
  return (
    <div className={ui.stack}>
      <Field id={id} label="从本地标签库补全">
        <input
          id={id}
          value={text}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          placeholder="标签、别名或中文翻译"
        />
      </Field>
      {query === text && query.trim() && (
        <div className={ui.row} aria-label="标签候选">
          {hits.data?.items.map((hit) => (
            <Button
              key={hit.canonical}
              small
              disabled={disabled}
              onClick={() => {
                if (hit.canonical) onAdd(hit.canonical);
                setText('');
              }}
            >
              {hit.canonical}
              {hit.translation ? ` · ${hit.translation}` : ''}
            </Button>
          ))}
          {hits.data && !hits.data.items.length && (
            <span className={ui.muted}>
              {hits.data.available ? '本地库未找到候选；仍可直接编辑标签。' : '尚未导入本地标签库。'}
            </span>
          )}
          {hits.isError && <span className={ui.muted}>暂时无法读取标签库。</span>}
        </div>
      )}
    </div>
  );
}
