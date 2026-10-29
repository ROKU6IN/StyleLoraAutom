import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import { projectPath } from '../../api/endpoints';
import type { AnnotationView, ImageItem } from '../../api/types';
import { errorText, isConflict } from '../../api/errors';
import { useDraftGuard } from '../../hooks/drafts';
import { queryClient, projectKey } from '../../state/queryClient';
import { useSession, canWrite } from '../../state/session';
import { Button, ErrorBox, Field, Modal, ui } from '../../ui';
import { draftChanged, fromAnnotation, manualBody, rebaseDraft, tagDelta, type Draft } from './draft';
import styles from './workbench.module.css';
import { TagSuggestions } from '../cleaning/TagSuggestions';
import { splitTags } from '../cleaning/types';
import { ImageRating } from './ImageRating';
import { VerifyImageFile } from './VerifyImageFile';
export function Editor({ pid, image }: { pid: string; image: ImageItem }) {
  const id = image.image_id;
  const fetched = useQuery({
    queryKey: projectKey(pid, 'annotation', id),
    queryFn: ({ signal }) =>
      request<AnnotationView>('GET', `${projectPath(pid)}/annotations/${id}`, undefined, { signal }),
  });
  const [base, setBase] = useState<AnnotationView | null>(null),
    [draft, setDraft] = useState<Draft | null>(null),
    [saving, setSaving] = useState(false),
    [error, setError] = useState<unknown>(null),
    [fresh, setFresh] = useState<AnnotationView | null>(null);
  const { register } = useDraftGuard();
  const mounted = useRef(true);
  useSession((s) => s.connection);
  const dirty = Boolean(base && draft && draftChanged(draft, base));
  useEffect(() => {
    if (fetched.data && !dirty && !saving) {
      setBase(fetched.data);
      setDraft(fromAnnotation(fetched.data));
    }
  }, [fetched.data, dirty, saving]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      register(null);
    };
  }, [register]);
  const save = useCallback(async () => {
    if (!base || !draft || saving || !canWrite()) return false;
    if (!draftChanged(draft, base)) return true;
    setSaving(true);
    setError(null);
    try {
      const result = await request<{ annotation: AnnotationView }>(
        'PUT',
        `${projectPath(pid)}/annotations/${id}/manual`,
        manualBody(draft, base),
        { version: base.version },
      );
      if (mounted.current) {
        setBase(result.annotation);
        setDraft(fromAnnotation(result.annotation));
        queryClient.setQueryData(projectKey(pid, 'annotation', id), result.annotation);
        void queryClient.invalidateQueries({ queryKey: projectKey(pid, 'images') });
        useSession.getState().notify('标注已保存');
      }
      return true;
    } catch (failure) {
      if (mounted.current) {
        setError(failure);
        if (isConflict(failure)) {
          try {
            setFresh(await request<AnnotationView>('GET', `${projectPath(pid)}/annotations/${id}`));
          } catch (next) {
            setError(next);
          }
        }
      }
      return false;
    } finally {
      if (mounted.current) setSaving(false);
    }
  }, [base, draft, id, pid, saving]);
  useEffect(() => {
    register({
      dirty,
      save,
      discard: () => {
        if (base) setDraft(fromAnnotation(base));
      },
    });
  }, [dirty, save, base, register]);
  useEffect(() => {
    const listener = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        void save();
      }
    };
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, [save]);
  const change = (update: Partial<Draft>) => setDraft((old) => (old ? { ...old, ...update } : old));
  const delta = base && draft ? tagDelta(draft, base) : null;
  return (
    <aside className={styles.editor} aria-label="标注编辑器">
      <div className={ui.between}>
        <span className={ui.eyebrow}>ANNOTATION</span>
        <span role="status" className={ui.muted}>
          {saving ? '保存中…' : dirty ? '草稿未保存' : '已同步'}
        </span>
      </div>
      <h2 title={image.filename}>{image.filename}</h2>
      <p className={ui.muted}>
        {image.width} × {image.height} · #{id} ·{' '}
        {image.has_alpha === null ? '透明信息未知' : image.has_alpha ? '含透明通道' : '无透明通道'}
      </p>
      <ErrorBox message={error ? errorText(error) : fetched.error ? errorText(fetched.error) : undefined} />
      <ImageRating pid={pid} image={image} />
      <VerifyImageFile key={`${pid}:${id}`} pid={pid} image={image} />
      {draft && base ? (
        <>
          <Field
            id="tags"
            label={`标签 · ${base.tags.length}`}
            hint="以逗号或换行分隔。保存为人工层，原始层保留。"
          >
            <textarea
              id="tags"
              rows={8}
              value={draft.tags}
              disabled={saving}
              onChange={(e) => change({ tags: e.target.value })}
            />
          </Field>
          <TagSuggestions
            disabled={saving}
            onAdd={(tag) => change({ tags: [...new Set([...splitTags(draft.tags), tag])].join(', ') })}
          />
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={draft.writeNl}
              disabled={saving}
              onChange={(e) => change({ writeNl: e.target.checked })}
            />
            同时写入人工自然语言
          </label>
          <Field id="natural-language" label="自然语言描述">
            <textarea
              id="natural-language"
              rows={4}
              value={draft.nl}
              disabled={!draft.writeNl || saving}
              onChange={(e) => change({ nl: e.target.value })}
            />
          </Field>
          <div className={ui.between}>
            <span className={ui.muted}>
              {delta && dirty ? `+${delta.add.length} / −${delta.remove.length}` : 'Ctrl + S 保存'}
            </span>
            <Button primary disabled={!dirty || saving || !canWrite()} onClick={() => void save()}>
              {saving ? '保存中…' : '保存标注'}
            </Button>
          </div>
          <details className={styles.layers}>
            <summary>查看标注层与版本引用</summary>
            {Object.entries(base.layers).map(([name, value]) => (
              <div className={styles.layer} key={name}>
                <strong>{name}</strong>
                <p>
                  {value
                    ? typeof value.content === 'string'
                      ? value.content
                      : (value.content as { tag: string }[]).map((t) => t.tag).join(', ')
                    : '—'}
                </p>
              </div>
            ))}
          </details>
        </>
      ) : (
        <p className={ui.muted}>正在读取标注…</p>
      )}
      <Modal
        open={Boolean(fresh)}
        onOpenChange={(open) => {
          if (!open) setFresh(null);
        }}
        title="标注已被修改"
        description="草稿已保留。检查远端内容后，可显式合并标签增删，再重新保存。自然语言若勾选写入，将使用当前草稿。"
      >
        {fresh && draft && base && (
          <div className={ui.stack}>
            <p>
              <strong>远端标签</strong>
              <br />
              {fresh.tags.map((t) => t.tag).join(', ') || '（空）'}
            </p>
            <p>
              <strong>远端描述</strong>
              <br />
              {fresh.natural_language || '（空）'}
            </p>
            <p>
              <strong>你的草稿</strong>
              <br />
              {draft.tags}
            </p>
            <div className={ui.actions}>
              <Button onClick={() => setFresh(null)}>继续保留草稿</Button>
              <Button
                primary
                onClick={() => {
                  setDraft(rebaseDraft(draft, base, fresh));
                  setBase(fresh);
                  queryClient.setQueryData(projectKey(pid, 'annotation', id), fresh);
                  setFresh(null);
                  setError(null);
                }}
              >
                合并到最新版本
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </aside>
  );
}
