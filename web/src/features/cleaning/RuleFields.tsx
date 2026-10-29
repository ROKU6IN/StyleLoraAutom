import { useState } from 'react';
import type { Rule } from './types';
import { splitTags } from './types';
import { Field, ui } from '../../ui';
import { TagSuggestions } from './TagSuggestions';
import styles from './cleaning.module.css';

export function RuleFields({
  value,
  onChange,
  disabled = false,
}: {
  value: Rule;
  onChange: (rule: Rule) => void;
  disabled?: boolean;
}) {
  // The parent remounts this editor when selecting a saved rule. Keep raw text so
  // commas and partially typed tags survive normalization of the request value.
  const [raw, setRaw] = useState(() => ({
    condition: value.condition?.tags.join(', ') ?? '',
    delete: value.delete?.tags.join(', ') ?? '',
    write: value.write?.tags.join(', ') ?? '',
  }));
  const [modes, setModes] = useState(() => ({
    condition: value.condition?.mode ?? 'any',
    delete: value.delete?.mode ?? 'any',
    write: value.write?.mode ?? 'any',
  }));
  return (
    <fieldset disabled={disabled} className={`${ui.stack} ${styles.ruleFields}`}>
      <legend>替换规则</legend>
      <p className={ui.muted}>
        条件检查原始标签；删除检查当前有效标签，成功后才写入。空条件表示不限。all 要求整组满足，any
        处理全部命中项。写入条件不满足时，已成功的删除仍然生效。
      </p>
      {(['condition', 'delete', 'write'] as const).map((name) => (
        <Field
          key={name}
          id={`rule-${name}`}
          label={{ condition: '条件标签', delete: '删除标签', write: '写入标签' }[name]}
        >
          <div className={styles.ruleRow}>
            <select
              aria-label={`${name} 模式`}
              value={modes[name]}
              onChange={(e) => {
                const mode = e.target.value as 'all' | 'any';
                setModes({ ...modes, [name]: mode });
                onChange({
                  ...value,
                  [name]: splitTags(raw[name]).length ? { mode, tags: splitTags(raw[name]) } : null,
                });
              }}
            >
              <option value="any">any · 任一</option>
              <option value="all">all · 全部</option>
            </select>
            <input
              id={`rule-${name}`}
              value={raw[name]}
              placeholder="逗号分隔标签"
              onChange={(e) => {
                const text = e.target.value;
                setRaw({ ...raw, [name]: text });
                const tags = splitTags(text);
                onChange({ ...value, [name]: tags.length ? { mode: modes[name], tags } : null });
              }}
            />
          </div>
          <details>
            <summary>从标签库选择</summary>
            <TagSuggestions
              id={`suggest-${name}`}
              disabled={disabled}
              onAdd={(tag) => {
                const tags = [...new Set([...splitTags(raw[name]), tag])];
                setRaw({ ...raw, [name]: tags.join(', ') });
                onChange({ ...value, [name]: { mode: modes[name], tags } });
              }}
            />
          </details>
        </Field>
      ))}
    </fieldset>
  );
}
