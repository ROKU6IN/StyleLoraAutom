import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { allPages, request } from '../api/client';
import { errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';

export const ASPECTS = ['landscape', 'portrait', 'square'] as const;
export type Sizes = Record<(typeof ASPECTS)[number], { width: number; height: number }>;
export const SMALL_SIZES: Sizes = {
  landscape: { width: 768, height: 512 },
  portrait: { width: 512, height: 768 },
  square: { width: 640, height: 640 },
};
export type Preset = {
  preset_id: string;
  name: string;
  sizes: Sizes;
  revision: number;
  version: string;
  builtin: boolean;
  builtin_key: string | null;
};
const P = '/api/presets';
const names = { landscape: '横向', portrait: '竖向', square: '方形' };
type SizeDraft = Record<(typeof ASPECTS)[number], { width: string; height: string }>;
const sizeDraft = (sizes: Sizes): SizeDraft =>
  Object.fromEntries(
    ASPECTS.map((key) => [
      key,
      {
        width: String(sizes[key].width),
        height: String(sizes[key].height),
      },
    ]),
  ) as SizeDraft;
export function presetPayload(name: string, sizes: SizeDraft) {
  if (!name.trim() || name.trim().length > 200) throw new Error('请填写不超过 200 字符的预设名称。');
  const result = {} as Sizes;
  for (const aspect of ASPECTS) {
    const values = [sizes[aspect].width, sizes[aspect].height];
    if (
      values.some(
        (value) =>
          !/^\d+$/.test(value) || Number(value) < 64 || Number(value) > 16384 || Number(value) % 64 !== 0,
      )
    )
      throw new Error('宽高须为 64–16384 之间的整数，并且是 64 的倍数。');
    const [width, height] = values.map(Number);
    if (
      (aspect === 'landscape' && width <= height) ||
      (aspect === 'portrait' && width >= height) ||
      (aspect === 'square' && width !== height)
    )
      throw new Error('横向须宽大于高，竖向须高大于宽，方形须宽高相等。');
    result[aspect] = { width, height };
  }
  return { name: name.trim(), sizes: result };
}
const dimensions = (preset: Preset) =>
  ASPECTS.map(
    (aspect) => `${names[aspect]} ${preset.sizes[aspect].width}×${preset.sizes[aspect].height}`,
  ).join(' · ');
const inventory = ({ signal }: { signal: AbortSignal }) => allPages<Preset>(P, signal);

export function PresetPicker({
  disabled,
  action,
  onSelect,
}: {
  disabled: boolean;
  action: (work: () => Promise<void>) => Promise<void>;
  onSelect: (sizes: Sizes) => void;
}) {
  const presets = useQuery({ queryKey: ['presets'], queryFn: inventory });
  return (
    <>
      <Field
        id="generation-preset"
        label="填入尺寸预设"
        hint="将三组实际像素复制到当前草稿；保存后与预设脱钩。默认使用内置小尺寸，修改或删除预设不会更新已有来源。"
      >
        <select
          id="generation-preset"
          value=""
          disabled={disabled || !presets.data}
          onChange={(event) => {
            const chosen = presets.data?.find((row) => row.preset_id === event.target.value);
            if (!chosen) return;
            void action(async () => {
              const current = await request<Preset>('GET', P + '/' + chosen.preset_id);
              if (current.version !== chosen.version) {
                await queryClient.invalidateQueries({ queryKey: ['presets'] });
                throw new Error('尺寸预设已变化，列表已刷新，请核对后重新选择。');
              }
              onSelect(structuredClone(current.sizes));
            });
          }}
        >
          <option value="">选择尺寸</option>
          {presets.data?.map((row) => (
            <option key={row.preset_id} value={row.preset_id}>
              {row.name}
              {row.builtin ? '（内置）' : ''} · {dimensions(row)}
            </option>
          ))}
        </select>
      </Field>
      <ErrorBox message={presets.error ? errorText(presets.error) : undefined} />
      <Link to="/settings/presets">管理尺寸预设</Link>
    </>
  );
}

export default function GenerationPresets() {
  useSession((state) => state.connection);
  const { register, run } = useDraftGuard();
  const presets = useQuery({ queryKey: ['presets'], queryFn: inventory });
  const [base, setBase] = useState<Preset | null>(null),
    [name, setName] = useState('');
  const [sizes, setSizes] = useState(() => sizeDraft(SMALL_SIZES));
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState<Preset | null>(null),
    [ack, setAck] = useState(false);
  const identity = useRef({ signature: '', key: '' }),
    deleteKey = useRef('');
  const dirty =
    name !== (base?.name ?? '') ||
    JSON.stringify(sizes) !== JSON.stringify(sizeDraft(base?.sizes ?? SMALL_SIZES));
  const apply = useCallback((value: Preset | null) => {
    setBase(value);
    setName(value?.name ?? '');
    setSizes(sizeDraft(value?.sizes ?? SMALL_SIZES));
    setError(null);
    setNotice('');
    identity.current = { signature: '', key: '' };
  }, []);
  const save = useCallback(async () => {
    if (busy || base?.builtin || !canWrite()) return false;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const body = presetPayload(name, sizes),
        signature = JSON.stringify([base?.preset_id, base?.version, body]);
      if (identity.current.signature !== signature)
        identity.current = { signature, key: crypto.randomUUID() };
      const result = await request<Preset>(
        base ? 'PUT' : 'POST',
        P + (base ? '/' + base.preset_id : ''),
        body,
        { version: base?.version, key: identity.current.key },
      );
      apply(result);
      setNotice(`尺寸预设已保存，版本 ${result.revision}。已有来源不受影响。`);
      await queryClient.invalidateQueries({ queryKey: ['presets'] });
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [apply, base, busy, name, sizes]);
  useEffect(() => {
    register({ dirty, label: '尺寸预设', save, discard: () => apply(base) });
    return () => register(null);
  }, [apply, base, dirty, register, save]);
  const open = (id: string) =>
    run(() => {
      if (!id) {
        apply(null);
        return;
      }
      setBusy(true);
      setError(null);
      void request<Preset>('GET', P + '/' + id)
        .then(apply)
        .catch(setError)
        .finally(() => setBusy(false));
    });
  const remove = async () => {
    if (!deleting || !ack || busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    try {
      await request('DELETE', P + '/' + deleting.preset_id, undefined, {
        version: deleting.version,
        key: deleteKey.current,
      });
      apply(null);
      setDeleting(null);
      setAck(false);
      setNotice('尺寸预设已删除；已有来源中的实际像素保持不变。');
      await queryClient.invalidateQueries({ queryKey: ['presets'] });
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>GENERATION / PIXEL PRESETS</div>
          <h1>尺寸预设</h1>
          <p>维护横、竖、方三组实际像素，在生成源草稿中显式选用。预设跨项目使用。</p>
        </div>
      </div>
      <ErrorBox
        message={!deleting && error ? errorText(error) : presets.error ? errorText(presets.error) : undefined}
      />
      {notice && <p role="status">{notice}</p>}
      <section className={ui.section}>
        <div className={ui.actions}>
          <Button disabled={busy} onClick={() => open('')}>
            新建尺寸预设
          </Button>
          <Button
            disabled={busy || !base}
            onClick={() =>
              run(() => {
                const copy = base!;
                apply(null);
                setName(copy.name + ' 副本');
                setSizes(sizeDraft(copy.sizes));
              })
            }
          >
            复制为自定义预设
          </Button>
          <Button disabled={busy || !base} onClick={() => open(base!.preset_id)}>
            重新载入尺寸预设
          </Button>
        </div>
        <Field id="preset-open" label="打开尺寸预设">
          <select
            id="preset-open"
            value={base?.preset_id ?? ''}
            disabled={busy}
            onChange={(event) => open(event.target.value)}
          >
            <option value="">新预设</option>
            {presets.data?.map((row) => (
              <option key={row.preset_id} value={row.preset_id}>
                {row.name}
                {row.builtin ? '（内置，只读）' : ` · 版本 ${row.revision} · ${row.preset_id.slice(0, 8)}`}
              </option>
            ))}
          </select>
        </Field>
        {base?.builtin && (
          <p className={ui.notice}>内置预设不可修改或删除，可复制后编辑。默认使用内置“小”。</p>
        )}
        <fieldset disabled={busy || !!base?.builtin || !canWrite()}>
          <legend>三种图幅尺寸</legend>
          <Field id="preset-name" label="预设名称">
            <input
              id="preset-name"
              value={name}
              maxLength={200}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          {ASPECTS.map((aspect) => (
            <div key={aspect} className={ui.formGrid}>
              {(['width', 'height'] as const).map((axis) => (
                <Field
                  key={axis}
                  id={`preset-${aspect}-${axis}`}
                  label={`${names[aspect]}${axis === 'width' ? '宽度' : '高度'}`}
                >
                  <input
                    id={`preset-${aspect}-${axis}`}
                    type="number"
                    step={64}
                    min={64}
                    max={16384}
                    value={sizes[aspect][axis]}
                    onChange={(event) =>
                      setSizes((previous) => ({
                        ...previous,
                        [aspect]: { ...previous[aspect], [axis]: event.target.value },
                      }))
                    }
                  />
                </Field>
              ))}
            </div>
          ))}
        </fieldset>
        <p className={ui.caption}>
          宽高均为 64–16384 的整数且是 64
          的倍数。横向宽大于高，竖向高大于宽，方形宽高相等。预设校验不代表生成模型支持该尺寸；启动时仍检查后端能力。
        </p>
        <div className={ui.actions}>
          <Button
            primary
            disabled={busy || !dirty || !!base?.builtin || !canWrite()}
            onClick={() => void save()}
          >
            保存尺寸预设
          </Button>
          <Button
            danger
            disabled={busy || dirty || !base || base.builtin || !canWrite()}
            onClick={() => {
              setDeleting(base);
              setAck(false);
              setError(null);
              deleteKey.current = crypto.randomUUID();
            }}
          >
            删除尺寸预设
          </Button>
        </div>
        {dirty && (
          <p className={ui.caption}>当前修改尚未保存；切换或离页时需处理草稿，删除前先保存或明确放弃。</p>
        )}
      </section>
      <Modal
        open={!!deleting}
        onOpenChange={(value) => {
          if (!value && !busy) {
            setDeleting(null);
            setAck(false);
          }
        }}
        title="删除尺寸预设"
        description="只移除产品库里的这份预设，已有来源中的实际像素不会改变。"
      >
        <ErrorBox message={error ? errorText(error) : undefined} />
        {deleting && (
          <>
            <p>
              {deleting.name} · 版本 {deleting.revision}
            </p>
            <p>{dimensions(deleting)}</p>
            <p className={ui.caption}>{deleting.preset_id}</p>
          </>
        )}
        <label className={ui.row}>
          <input
            type="checkbox"
            checked={ack}
            disabled={busy}
            onChange={(event) => setAck(event.target.checked)}
          />
          确认删除这份自定义尺寸预设
        </label>
        <div className={ui.actions}>
          <HoldButton disabled={busy || !ack || !canWrite()} onConfirm={() => void remove()}>
            按住删除预设
          </HoldButton>
          <Button
            disabled={busy}
            onClick={() => {
              setDeleting(null);
              setAck(false);
            }}
          >
            保留预设
          </Button>
        </div>
      </Modal>
    </div>
  );
}
