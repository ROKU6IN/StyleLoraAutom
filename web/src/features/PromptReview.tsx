import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';
import { request } from '../api/client';
import { errorCodeText, errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, Field, ui } from '../ui';
import { CoverageReport } from './PromptProduction';

type Entry = {
  tags: string[];
  natural_language: string | null;
  aspect: string | null;
  rating: string | null;
};
export type ReviewDetail = {
  task_id: string;
  index: number;
  revision: number;
  status: string;
  reason: string | null;
  can_review: boolean;
  blocked_reason: string | null;
  entry: Entry;
  original_entry: Entry;
  model_entry: Entry | null;
  out_of_frame: string[];
  rating_evidence: string[];
  options: { tag_range: number[]; nl_enabled: boolean; aspect_hint: string | null };
  review: { reviewed_at: string; previous_reason: string | null; source: string } | null;
};
type Preview = {
  preview_id: string;
  digest: string;
  valid: boolean;
  issues: string[];
  entry: Entry;
  expected_revision: number;
  ready: boolean;
  previous_ready: boolean;
  selected: boolean;
  count: number;
  selection_changes: number;
  rating_lower_bound: string | null;
  coverage: Parameters<typeof CoverageReport>[0]['value'] | null;
};
const reasons: Record<string, string> = {
  review_required: '仍含待审核表达，请修改后再次复验。',
  forbidden_content: '含禁用风格、作者、质量、控制语法或负面内容。',
  invalid_contract: '不符合冻结的标签数量、首位人数标签、自然语言、图幅或画外内容约定。',
  rating_below_evidence: '评级不能低于已有证据或冻结关键词的下界。',
  coverage_drift: '修改后的主维标签必须保持原覆盖格子。',
  fenced: '所选评级超出此任务的冻结围栏。',
  excluded: '命中此任务冻结的排除条件。',
  duplicate: '标签集合与已有合格条目重复，请保留不同内容。',
};
function message(error: unknown) {
  return error instanceof Error && !('code' in error) ? error.message : errorText(error);
}
function Content({ value }: { value: Entry }) {
  return (
    <>
      <p>{value.tags.join(', ')}</p>
      <p>{value.natural_language ?? '无自然语言描述'}</p>
      <p className={ui.caption}>
        {value.aspect ?? '图幅未指定'} · {value.rating ?? '评级未知'}
      </p>
    </>
  );
}
export default function PromptReview() {
  const { taskId, index } = useParams();
  const detail = useQuery({
    queryKey: ['prompt', 'review', taskId, index],
    queryFn: ({ signal }) =>
      request<ReviewDetail>('GET', `/api/prompt-production/tasks/${taskId}/reviews/${index}`, undefined, {
        signal,
      }),
  });
  return detail.data ? (
    <ReviewEditor key={`${taskId}:${index}`} initial={detail.data} />
  ) : detail.error ? (
    <ErrorBox message={errorText(detail.error)} />
  ) : (
    <EmptyState title="正在读取待审核条目" description="读取此任务已保存的输入和模型输出。" />
  );
}

export function ReviewEditor({ initial }: { initial: ReviewDetail }) {
  useSession((s) => s.connection);
  const { register, run } = useDraftGuard();
  const [base, setBase] = useState(initial);
  const [tags, setTags] = useState(initial.entry.tags.join('\n')),
    [nl, setNl] = useState(initial.entry.natural_language ?? '');
  const [aspect, setAspect] = useState(initial.entry.aspect ?? ''),
    [rating, setRating] = useState(initial.entry.rating ?? '');
  const [preview, setPreview] = useState<Preview | null>(null),
    [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const identity = useRef({ signature: '', key: '' });
  const path = '/api/prompt-production/tasks/' + base.task_id;
  const baseline = JSON.stringify([
    base.entry.tags.join('\n'),
    base.entry.natural_language ?? '',
    base.entry.aspect ?? '',
    base.entry.rating ?? '',
  ]);
  const changed = baseline !== JSON.stringify([tags, nl, aspect, rating]);
  const dirty = changed || !!preview || ack;
  const apply = useCallback((value: ReviewDetail) => {
    setBase(value);
    setTags(value.entry.tags.join('\n'));
    setNl(value.entry.natural_language ?? '');
    setAspect(value.entry.aspect ?? '');
    setRating(value.entry.rating ?? '');
    setPreview(null);
    setAck(false);
  }, []);
  const reset = useCallback(() => apply(base), [apply, base]);
  const save = useCallback(async () => {
    if (busy || !canWrite() || !base.can_review) return false;
    if (!preview?.valid || !ack) {
      setError(new Error('请先通过复验，并确认内容与评级后再保存。'));
      return false;
    }
    setBusy(true);
    setError(null);
    try {
      const body = { preview_id: preview.preview_id, digest: preview.digest, acknowledge: true };
      const signature = JSON.stringify([base.task_id, body]);
      if (identity.current.signature !== signature)
        identity.current = { signature, key: crypto.randomUUID() };
      await request('POST', path + '/reviews', body, { key: identity.current.key });
      apply(await request<ReviewDetail>('GET', path + '/reviews/' + base.index));
      void queryClient.invalidateQueries({ queryKey: ['prompt'] });
      useSession.getState().notify('人工复验已保存，任务结果已重新计算');
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [ack, apply, base, busy, path, preview]);
  useEffect(() => {
    register({ dirty, label: '提示词复验草稿', save, discard: reset });
    return () => register(null);
  }, [dirty, register, reset, save]);
  const change = (fn: () => void) => {
    fn();
    setPreview(null);
    setAck(false);
  };
  const action = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={ui.stack}>
      <header className={ui.between}>
        <div>
          <h1>提示词人工复验 · 第 {base.index + 1} 项</h1>
          <p>修改后使用任务冻结的规则重新检查；复验不调用模型。</p>
        </div>
        <Link to={'/prompt-production?task=' + base.task_id}>返回任务结果</Link>
      </header>
      <ErrorBox message={error ? message(error) : undefined} />
      {!base.can_review && (
        <p role="status" className={ui.notice}>
          {base.review
            ? '此条目已完成人工复验，当前为只读记录。'
            : errorCodeText(base.blocked_reason ?? 'prompt:review_requires_done')}
        </p>
      )}
      {base.review && (
        <p>
          复验时间：{base.review.reviewed_at} · 原状态：待检查 · 原原因：
          {base.review.previous_reason ?? '未记录'}
        </p>
      )}
      <section className={`${ui.section} ${ui.stack}`} aria-label="审核依据">
        <h2>原始内容与待审核输出</h2>
        <details open>
          <summary>原始输入</summary>
          <Content value={base.original_entry} />
        </details>
        {base.model_entry ? (
          <details open>
            <summary>待审核内容</summary>
            <Content value={base.model_entry} />
          </details>
        ) : (
          <p className={ui.warning}>此条目没有保留可供审核的模型输出，以下草稿取自原始输入。</p>
        )}
        <p>已有评级证据：{base.rating_evidence.join('、') || '未知'}；最终评级还会检查冻结关键词下界。</p>
        {!!base.out_of_frame.length && (
          <p>原输出声明的画外内容：{base.out_of_frame.join('、')}。本次不能重新加入标签或描述。</p>
        )}
      </section>
      <section className={`${ui.section} ${ui.stack}`} aria-label="人工复验编辑器">
        <h2>{base.review ? '已采纳内容' : '编辑待审核内容'}</h2>
        <p className={ui.caption}>
          每行一个标签，保留顺序；首位为人数标签或 no humans，数量需为 {base.options.tag_range.join('–')}。
          {base.options.nl_enabled ? '需提供英文自然语言描述。' : '此任务关闭自然语言，请保持描述为空。'}{' '}
          已确定的图幅与覆盖格子必须保持。
        </p>
        <Field id="review-tags" label="内容标签（每行一个）">
          <textarea
            id="review-tags"
            rows={8}
            value={tags}
            disabled={busy || !base.can_review}
            onChange={(e) => change(() => setTags(e.target.value))}
          />
        </Field>
        <Field id="review-nl" label="自然语言描述">
          <textarea
            id="review-nl"
            rows={4}
            value={nl}
            disabled={busy || !base.can_review}
            onChange={(e) => change(() => setNl(e.target.value))}
          />
        </Field>
        <div className={ui.row}>
          <Field id="review-aspect" label="图幅">
            <select
              id="review-aspect"
              value={aspect}
              disabled={busy || !base.can_review}
              onChange={(e) => change(() => setAspect(e.target.value))}
            >
              <option value="">请选择</option>
              <option value="landscape">横图</option>
              <option value="portrait">竖图</option>
              <option value="square">方图</option>
            </select>
          </Field>
          <Field id="review-rating" label="人工确认评级">
            <select
              id="review-rating"
              value={rating}
              disabled={busy || !base.can_review}
              onChange={(e) => change(() => setRating(e.target.value))}
            >
              <option value="">请选择</option>
              {['general', 'sensitive', 'questionable', 'explicit'].map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className={ui.actions}>
          <Button
            disabled={busy || !base.can_review || !canWrite()}
            onClick={() =>
              void action(async () => {
                setPreview(null);
                setAck(false);
                if (!aspect || !rating) throw new Error('请明确选择图幅和人工确认评级。');
                const entry = {
                  tags: tags
                    .split('\n')
                    .map((tag) => tag.trim())
                    .filter(Boolean),
                  natural_language: nl.trim() || null,
                  aspect,
                  rating,
                };
                setPreview(
                  await request<Preview>('POST', path + '/reviews/preview', {
                    expected_revision: base.revision,
                    index: base.index,
                    entry,
                  }),
                );
              })
            }
          >
            复验修改并预览结果
          </Button>
          <Button
            disabled={busy}
            onClick={() =>
              run(
                () =>
                  void action(async () =>
                    apply(await request<ReviewDetail>('GET', path + '/reviews/' + base.index)),
                  ),
              )
            }
          >
            重新载入当前记录
          </Button>
        </div>
        {preview && (
          <div className={ui.stack} aria-label="人工复验预览">
            <p role="status">{preview.valid ? '复验通过，尚未保存。' : '复验未通过，原记录保持不变。'}</p>
            {preview.issues.map((reason) => (
              <p key={reason} className={ui.warning}>
                {reasons[reason] ?? reason}
              </p>
            ))}
            <details>
              <summary>查看规范化后的内容</summary>
              <Content value={preview.entry} />
            </details>
            {preview.valid && (
              <>
                <p>
                  保存后：{preview.ready ? '任务就绪，可导出冻结提示词' : '任务仍有数量或覆盖缺口'}；最终选中{' '}
                  {preview.count} / {preview.coverage?.requested} 条，本项
                  {preview.selected ? '进入最终集合' : '暂未进入最终集合'}，最终集合变动{' '}
                  {preview.selection_changes} 项。
                </p>
                <p className={ui.caption}>
                  仍有缺口时可导出已合格部分或手动追加词典。不会自动补位、调用模型、追加词典，也不会改写已导出的文件或已交接的生成源。
                </p>
                {preview.coverage && (
                  <details>
                    <summary>查看重新计算的配额与比例</summary>
                    <CoverageReport value={preview.coverage} />
                  </details>
                )}
                <label className={ui.row}>
                  <input
                    type="checkbox"
                    checked={ack}
                    disabled={busy}
                    onChange={(e) => setAck(e.target.checked)}
                  />
                  我已检查内容与评级，确认采用修改并重新计算任务结果
                </label>
                <Button primary disabled={busy || !ack || !canWrite()} onClick={() => void save()}>
                  确认保存复验结果
                </Button>
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
