import { useCallback, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { canWrite, useSession } from '../state/session';
import { queryClient } from '../state/queryClient';
import { Button, ErrorBox, Field, ui } from '../ui';

type Policy = {
  keywords: { tag: string; rating: string }[];
  blocked_terms: string[];
  hash: string;
  version: string;
};
export function parseKeywords(text: string) {
  return text
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line) => {
      const parts = line.split('\t').map((s) => s.trim());
      if (
        parts.length !== 2 ||
        !parts[0] ||
        !['general', 'sensitive', 'questionable', 'explicit'].includes(parts[1])
      )
        throw new Error(
          '每行需使用“标签 + 制表符 + 评级”，评级只能是 general、sensitive、questionable、explicit。',
        );
      return { tag: parts[0], rating: parts[1] };
    });
}
const keywordText = (p: Policy) => p.keywords.map((k) => `${k.tag}\t${k.rating}`).join('\n');
export default function VisionPolicy() {
  const fetched = useQuery({
    queryKey: ['llm', 'vision-policy'],
    queryFn: ({ signal }) => request<Policy>('GET', '/api/llm/vision-policy', undefined, { signal }),
  });
  const [base, setBase] = useState<Policy | null>(null),
    [keywords, setKeywords] = useState(''),
    [blocked, setBlocked] = useState('');
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  useSession((s) => s.connection);
  const { register } = useDraftGuard();
  const dirty = Boolean(
    base && (keywords !== keywordText(base) || blocked !== base.blocked_terms.join('\n')),
  );
  useEffect(() => {
    if (fetched.data && !dirty && !busy) {
      setBase(fetched.data);
      setKeywords(keywordText(fetched.data));
      setBlocked(fetched.data.blocked_terms.join('\n'));
    }
  }, [fetched.data, dirty, busy]);
  const save = useCallback(async () => {
    if (!base || busy || !canWrite()) return false;
    setBusy(true);
    setError(null);
    try {
      const next = await request<Policy>(
        'PUT',
        '/api/llm/vision-policy',
        {
          keywords: parseKeywords(keywords),
          blocked_terms: blocked
            .split(/\r?\n/)
            .map((s) => s.trim())
            .filter(Boolean),
        },
        { version: base.version },
      );
      queryClient.setQueryData(['llm', 'vision-policy'], next);
      setBase(next);
      setKeywords(keywordText(next));
      setBlocked(next.blocked_terms.join('\n'));
      useSession.getState().notify('视觉关键词已保存');
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [base, busy, keywords, blocked]);
  useEffect(() => {
    register({
      dirty,
      label: '视觉关键词',
      save,
      discard: () => {
        if (base) {
          setKeywords(keywordText(base));
          setBlocked(base.blocked_terms.join('\n'));
        }
      },
    });
    return () => register(null);
  }, [dirty, save, base, register]);
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>VISION / POLICY</div>
          <h1>视觉关键词</h1>
          <p>全局配置按内容版本保存，已创建的任务继续使用其冻结版本。</p>
        </div>
      </div>
      <ErrorBox
        message={
          error
            ? error instanceof Error && !(error as { code?: string }).code
              ? error.message
              : errorText(error)
            : fetched.error
              ? errorText(fetched.error)
              : undefined
        }
      />
      <section className={ui.section}>
        <h2>分级与输出检查</h2>
        <p>
          分级关键词使用规范化后的精确标签匹配，多个命中取最高级别。默认词表为空，不会擅自补充分级词。导入时只运行本地关键词检查。
        </p>
        <Field id="vision-keywords" label="分级关键词（每行：标签、制表符、评级）">
          <textarea
            id="vision-keywords"
            rows={10}
            value={keywords}
            disabled={busy || !base}
            onChange={(e) => setKeywords(e.target.value)}
          />
        </Field>
        <Field id="vision-blocked" label="风格任务输出禁用词（每行一个）">
          <textarea
            id="vision-blocked"
            rows={8}
            value={blocked}
            disabled={busy || !base}
            onChange={(e) => setBlocked(e.target.value)}
          />
        </Field>
        <p className={ui.muted}>
          输出检查仅覆盖字面词和少量待复核表达，不能代替人工语义审核。重新分级可在项目的视觉描述任务页执行。
        </p>
        <Button primary disabled={!dirty || busy || !canWrite()} onClick={() => void save()}>
          保存视觉关键词
        </Button>
        {base && <p className={ui.caption}>内容摘要：{base.hash}</p>}
        {Boolean(error) && (
          <Button
            quiet
            disabled={busy}
            onClick={async () => {
              try {
                const latest = await request<Policy>('GET', '/api/llm/vision-policy');
                setBase(latest);
                setError(null);
              } catch (failure) {
                setError(failure);
              }
            }}
          >
            读取最新版本并保留草稿
          </Button>
        )}
      </section>
    </div>
  );
}
