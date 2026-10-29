import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import { errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { queryClient } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, Field, HoldButton, ui } from '../../ui';
import { LibraryImport } from './LibraryImport';
import { RuleFields } from './RuleFields';
import { emptyRule, type SavedRule, type Library } from './types';

export default function RulesPage() {
  const library = useQuery({ queryKey: ['rules'], queryFn: () => request<Library>('GET', '/api/rules') });
  const [edit, setEdit] = useState<{ row: SavedRule; base: Library; key: string } | null>(null);
  const action = useAction();
  useSession((s) => s.connection);
  const save = async (rows: SavedRule[], base: Library, key: string) => {
    await request(
      'PUT',
      '/api/rules',
      { schema_version: 1, kind: 'replacement_rules', rules: rows },
      { version: base.version, key },
    );
    setEdit(null);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['rules'] }),
      queryClient.invalidateQueries({ queryKey: ['catalog'] }),
    ]);
  };
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>REPLACEMENT RULES</div>
          <h1>替换规则库</h1>
          <p>保存可复用的单条规则。在数据工作台选择图片后，逐条预览、应用。</p>
        </div>
      </div>
      <section className={ui.section}>
        <div className={ui.between}>
          <h2>我的规则 · {library.data?.rules.length ?? 0}</h2>
          <Button
            disabled={!library.data || action.busy || !!edit || !canWrite()}
            onClick={() => {
              action.setError(null);
              setEdit({
                row: { id: crypto.randomUUID(), name: '', rule: emptyRule() },
                base: library.data!,
                key: crypto.randomUUID(),
              });
            }}
          >
            新建规则
          </Button>
        </div>
        <ErrorBox
          message={
            action.error ? errorText(action.error) : library.error ? errorText(library.error) : undefined
          }
        />
        {library.data?.rules.map((row) => (
          <div key={row.id} className={ui.between}>
            <strong>{row.name}</strong>
            <div className={ui.row}>
              <Button
                small
                disabled={action.busy || !!edit}
                onClick={() => {
                  action.setError(null);
                  setEdit({ row, base: library.data!, key: crypto.randomUUID() });
                }}
              >
                编辑 {row.name}
              </Button>
              <HoldButton
                disabled={action.busy || !!edit || !canWrite()}
                onConfirm={() =>
                  void action.run(() =>
                    save(
                      library.data!.rules.filter((r) => r.id !== row.id),
                      library.data!,
                      crypto.randomUUID(),
                    ),
                  )
                }
              >
                删除 {row.name}
              </HoldButton>
            </div>
          </div>
        ))}
        {edit && (
          <section className={ui.stack}>
            <Field id="rule-name" label="规则名称">
              <input
                id="rule-name"
                value={edit.row.name}
                disabled={action.busy}
                onChange={(e) =>
                  setEdit({ ...edit, row: { ...edit.row, name: e.target.value }, key: crypto.randomUUID() })
                }
              />
            </Field>
            <RuleFields
              key={edit.row.id}
              value={edit.row.rule}
              disabled={action.busy}
              onChange={(rule) => setEdit({ ...edit, row: { ...edit.row, rule }, key: crypto.randomUUID() })}
            />
            <div className={ui.actions}>
              <Button disabled={action.busy} onClick={() => setEdit(null)}>
                取消规则编辑
              </Button>
              <Button
                primary
                disabled={
                  action.busy ||
                  !canWrite() ||
                  !edit.row.name.trim() ||
                  (!edit.row.rule.delete && !edit.row.rule.write)
                }
                onClick={() =>
                  void action.run(() =>
                    save(
                      [...edit.base.rules.filter((r) => r.id !== edit.row.id), edit.row],
                      edit.base,
                      edit.key,
                    ),
                  )
                }
              >
                保存规则
              </Button>
            </div>
          </section>
        )}
      </section>
      <LibraryImport rules />
    </div>
  );
}
