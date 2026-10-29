import { useMemo, useState } from 'react';
import { Button, ErrorBox, Field, Modal, ui } from '../ui';
import { matrixCell } from './CoverageMatrix';
import {
  editorCells,
  lines,
  renameDimension,
  layoutIssueText,
  type CoverageForm,
  type PlanForm,
  type Layout,
  type ConstraintForm,
} from './coverageEditorData';
import styles from './coverageMatrix.module.css';

function WeightGrid({
  plan,
  layout,
  onChange,
}: {
  plan: PlanForm;
  layout?: Layout;
  onChange: (plan: PlanForm) => void;
}) {
  const grid = useMemo(() => editorCells(plan), [plan]);
  const [axes, setAxes] = useState([0, 1]),
    [filter, setFilter] = useState('');
  const [rowPage, setRowPage] = useState(0),
    [colPage, setColPage] = useState(0),
    [selected, setSelected] = useState<string | null>(null);
  if (!grid)
    return (
      <p className={ui.warning}>请先填写 2–3 个不同主维度及非空、不重复的取值，格子总数不能超过 10000。</p>
    );
  const names = grid.names;
  const rowAxis = names[axes[0]] ?? names[0],
    colAxis = names[axes[1]] ?? names[1];
  const other = names.find((name) => name !== rowAxis && name !== colAxis);
  const slice = other ? (grid.values[other].includes(filter) ? filter : grid.values[other][0]) : '';
  const rows = grid.values[rowAxis],
    cols = grid.values[colAxis];
  const rp = Math.min(rowPage, Math.ceil(rows.length / 10) - 1),
    cp = Math.min(colPage, Math.ceil(cols.length / 10) - 1);
  const validKeys = new Set(grid.keys),
    active = layout ? new Set(layout.active_cells) : null;
  const stale = Object.keys(plan.cell_weights).filter((key) => !validKeys.has(key));
  const current = selected && validKeys.has(selected) ? selected : null;
  const changeAxes = (row: number, column: number) => {
    setAxes([row, column]);
    setFilter('');
    setRowPage(0);
    setColPage(0);
    setSelected(null);
  };
  return (
    <section className={ui.stack} aria-label="格子权重编辑">
      <h3>格子权重</h3>
      <p>
        权重是各格分配剩余名额的相对比例；每个有效格先分配一条。修改后重新预览配额，不能直接指定某格的固定条数。
      </p>
      {!!stale.length && (
        <div className={ui.warning}>
          <p>有 {stale.length} 项权重对应的格子已不存在，原值仍保留。请核对维度修改后再清理。</p>
          <Button
            onClick={() =>
              onChange({
                ...plan,
                cell_weights: Object.fromEntries(
                  Object.entries(plan.cell_weights).filter(([key]) => validKeys.has(key)),
                ),
              })
            }
          >
            清理已失效格子权重
          </Button>
        </div>
      )}
      <div className={ui.formGrid}>
        <Field id="weight-row" label="权重行维度">
          <select
            id="weight-row"
            value={names.indexOf(rowAxis)}
            onChange={(e) => {
              const i = Number(e.target.value);
              changeAxes(i, i === axes[1] ? axes[0] : axes[1]);
            }}
          >
            {names.map((name, i) => (
              <option key={name} value={i}>
                {name}
              </option>
            ))}
          </select>
        </Field>
        <Field id="weight-col" label="权重列维度">
          <select
            id="weight-col"
            value={names.indexOf(colAxis)}
            onChange={(e) => {
              const i = Number(e.target.value);
              changeAxes(i === axes[0] ? axes[1] : axes[0], i);
            }}
          >
            {names.map((name, i) => (
              <option key={name} value={i}>
                {name}
              </option>
            ))}
          </select>
        </Field>
        {other && (
          <Field id="weight-slice" label={`权重筛选 ${other}`}>
            <select
              id="weight-slice"
              value={slice}
              onChange={(e) => {
                setFilter(e.target.value);
                setSelected(null);
              }}
            >
              {grid.values[other].map((tag) => (
                <option key={tag}>{tag}</option>
              ))}
            </select>
          </Field>
        )}
      </div>
      <div className={ui.actions}>
        <span>
          共 {grid.keys.length} 格；行第 {rp + 1} / {Math.ceil(rows.length / 10)} 页，列第 {cp + 1} /{' '}
          {Math.ceil(cols.length / 10)} 页
        </span>
        <Button disabled={rp === 0} onClick={() => setRowPage(rp - 1)}>
          权重上一组行
        </Button>
        <Button disabled={(rp + 1) * 10 >= rows.length} onClick={() => setRowPage(rp + 1)}>
          权重下一组行
        </Button>
        <Button disabled={cp === 0} onClick={() => setColPage(cp - 1)}>
          权重上一组列
        </Button>
        <Button disabled={(cp + 1) * 10 >= cols.length} onClick={() => setColPage(cp + 1)}>
          权重下一组列
        </Button>
      </div>
      <div className={ui.tableWrap}>
        <table className={`${ui.table} ${styles.matrix}`}>
          <caption>选择格子调整权重</caption>
          <thead>
            <tr>
              <th scope="col">
                {rowAxis} / {colAxis}
              </th>
              {cols.slice(cp * 10, (cp + 1) * 10).map((tag) => (
                <th key={tag} scope="col">
                  {tag}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(rp * 10, (rp + 1) * 10).map((row) => (
              <tr key={row}>
                <th scope="row">{row}</th>
                {cols.slice(cp * 10, (cp + 1) * 10).map((col) => {
                  const key = matrixCell(names, {
                    [rowAxis]: row,
                    [colAxis]: col,
                    ...(other ? { [other]: slice } : {}),
                  });
                  const excluded = active && !active.has(key);
                  return (
                    <td key={col}>
                      <button
                        type="button"
                        className={`${styles.cell} ${excluded ? styles.excluded : styles.pending}`}
                        aria-pressed={current === key}
                        aria-label={`编辑权重 ${JSON.parse(key)
                          .map(([name, tag]: string[]) => `${name}: ${tag}`)
                          .join(' · ')}`}
                        onClick={() => setSelected(key)}
                      >
                        <span>权重 {plan.cell_weights[key] ?? '1（默认）'}</span>
                        <strong>
                          {excluded
                            ? '已排除'
                            : layout?.cell_targets
                              ? `计划配额 ${layout.cell_targets[key]}`
                              : '配额待预览'}
                        </strong>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {current && (
        <div className={ui.stack}>
          <p>
            当前格子：
            {(JSON.parse(current) as string[][]).map(([name, tag]) => `${name}: ${tag}`).join(' · ')}
          </p>
          <Field
            id="cell-weight"
            label="当前格子权重"
            hint="1–100000 的整数；空值需修正，恢复默认可移除自定义权重。"
          >
            <input
              id="cell-weight"
              inputMode="numeric"
              value={plan.cell_weights[current] ?? '1'}
              onChange={(e) =>
                onChange({ ...plan, cell_weights: { ...plan.cell_weights, [current]: e.target.value } })
              }
            />
          </Field>
          <div className={ui.actions}>
            <Button
              onClick={() =>
                onChange({
                  ...plan,
                  cell_weights: Object.fromEntries(
                    Object.entries(plan.cell_weights).filter(([key]) => key !== current),
                  ),
                })
              }
            >
              恢复当前格子默认权重
            </Button>
            <Button
              disabled={plan.exclude.length >= 100}
              onClick={() =>
                onChange({
                  ...plan,
                  exclude: [
                    ...plan.exclude,
                    { all_tags: (JSON.parse(current) as string[][]).map(([, tag]) => tag), none_tags: [] },
                  ],
                })
              }
            >
              按当前格子标签新增排除条件
            </Button>
          </div>
          <p className={ui.muted}>
            排除按标签集合匹配，可能同时影响其他含有相同标签的格子或候选；可在下方核对、修改或删除排除条件。
          </p>
        </div>
      )}
    </section>
  );
}

export default function CoverageFormEditor({
  form,
  layout,
  disabled,
  onChange,
}: {
  form: CoverageForm;
  layout?: Layout;
  disabled: boolean;
  onChange: (form: CoverageForm) => void;
}) {
  const plan = form.plan;
  const [error, setError] = useState(''),
    [removeDimension, setRemoveDimension] = useState<number | null>(null);
  const [constraintIndex, setConstraintIndex] = useState(0),
    [predicateIndex, setPredicateIndex] = useState(0);
  const ci = Math.min(constraintIndex, Math.max(0, plan.constraints.length - 1)),
    pi = Math.min(predicateIndex, Math.max(0, plan.exclude.length - 1));
  const constraint = plan.constraints[ci],
    predicate = plan.exclude[pi];
  const update = (next: PlanForm) => {
    onChange({ ...form, plan: next });
    setError('');
  };
  const updateConstraint = (next: ConstraintForm) =>
    update({ ...plan, constraints: plan.constraints.map((row, i) => (i === ci ? next : row)) });
  return (
    <fieldset disabled={disabled} className={ui.stack}>
      <legend>覆盖方案表单</legend>
      <ErrorBox message={error} />
      <Field id="plan-name" label="方案名称">
        <input
          id="plan-name"
          maxLength={200}
          value={form.name}
          onChange={(e) => onChange({ ...form, name: e.target.value })}
        />
      </Field>
      <div className={ui.formGrid}>
        <Field id="plan-count" label="方案目标数量" hint="1–10000；覆盖时不能少于有效格子数。">
          <input
            id="plan-count"
            inputMode="numeric"
            value={plan.target_count}
            onChange={(e) => update({ ...plan, target_count: e.target.value })}
          />
        </Field>
        <Field
          id="plan-seed"
          label="方案随机种子"
          hint="可留空；精确支持 0–18446744073709551615。采样页仍只接受浏览器可精确表示的种子。"
        >
          <input
            id="plan-seed"
            inputMode="numeric"
            value={plan.random_seed}
            onChange={(e) => update({ ...plan, random_seed: e.target.value })}
          />
        </Field>
      </div>
      <h3>主维度与取值</h3>
      {plan.primary_dimensions.map((name, index) => (
        <div key={index} className={ui.stack}>
          <Field id={`plan-dimension-${index}`} label={`主维度 ${index + 1} 名称`}>
            <input
              id={`plan-dimension-${index}`}
              value={name}
              maxLength={64}
              onChange={(e) => {
                try {
                  update(renameDimension(plan, index, e.target.value));
                } catch (failure) {
                  setError((failure as Error).message);
                }
              }}
            />
          </Field>
          <Field id={`plan-values-${index}`} label={`主维度 ${index + 1} 取值（每行一个）`}>
            <textarea
              id={`plan-values-${index}`}
              rows={4}
              value={(plan.cell_values[name] ?? []).join('\n')}
              onChange={(e) =>
                update({ ...plan, cell_values: { ...plan.cell_values, [name]: lines(e.target.value) } })
              }
            />
          </Field>
          {plan.primary_dimensions.length === 3 && (
            <Button danger onClick={() => setRemoveDimension(index)}>
              删除主维度 {index + 1}
            </Button>
          )}
        </div>
      ))}
      <Button
        disabled={plan.primary_dimensions.length >= 3}
        onClick={() => {
          let name = 'dimension';
          while (plan.primary_dimensions.includes(name)) name += '_new';
          update({
            ...plan,
            primary_dimensions: [...plan.primary_dimensions, name],
            cell_values: { ...plan.cell_values, [name]: [''] },
          });
        }}
      >
        新增第三维度
      </Button>
      <p className={ui.muted}>
        新增、删除维度或修改取值不会悄悄删除旧权重。失效权重需核对后显式清理；重命名维度会保留对应格子的权重。
      </p>
      <WeightGrid
        key={JSON.stringify(plan.primary_dimensions)}
        plan={plan}
        layout={layout}
        onChange={update}
      />
      <h3>整体比例与数量约束</h3>
      <p>
        这些上下界检查最终整批标签；比例填写 0–1。比例与数量同时设置时取更严格的限制，空值表示不设该界限。
      </p>
      <div className={ui.actions}>
        <Button
          disabled={plan.constraints.length >= 100}
          onClick={() => {
            update({
              ...plan,
              constraints: [
                ...plan.constraints,
                {
                  dimension: 'count',
                  tag: '',
                  minimum_ratio: '',
                  maximum_ratio: '',
                  minimum_count: '',
                  maximum_count: '',
                },
              ],
            });
            setConstraintIndex(plan.constraints.length);
          }}
        >
          新增整体约束
        </Button>
        {!!constraint && (
          <Button
            danger
            onClick={() => update({ ...plan, constraints: plan.constraints.filter((_, i) => i !== ci) })}
          >
            删除当前整体约束
          </Button>
        )}
      </div>
      {constraint && (
        <>
          <Field id="constraint-select" label="当前整体约束">
            <select
              id="constraint-select"
              value={ci}
              onChange={(e) => setConstraintIndex(Number(e.target.value))}
            >
              {plan.constraints.map((row, i) => (
                <option key={i} value={i}>
                  {i + 1} · {row.dimension}: {row.tag || '待填写标签'}
                </option>
              ))}
            </select>
          </Field>
          <div className={ui.formGrid}>
            <Field id="constraint-dim" label="约束分类维度">
              <input
                id="constraint-dim"
                value={constraint.dimension}
                maxLength={64}
                onChange={(e) => updateConstraint({ ...constraint, dimension: e.target.value })}
              />
            </Field>
            <Field id="constraint-tag" label="约束标签">
              <input
                id="constraint-tag"
                value={constraint.tag}
                maxLength={200}
                onChange={(e) => updateConstraint({ ...constraint, tag: e.target.value })}
              />
            </Field>
            {(
              [
                ['minimum_ratio', '最小比例'],
                ['maximum_ratio', '最大比例'],
                ['minimum_count', '最少条数'],
                ['maximum_count', '最多条数'],
              ] as const
            ).map(([key, label]) => (
              <Field key={key} id={'constraint-' + key} label={label}>
                <input
                  id={'constraint-' + key}
                  inputMode="decimal"
                  value={constraint[key]}
                  onChange={(e) => updateConstraint({ ...constraint, [key]: e.target.value })}
                />
              </Field>
            ))}
          </div>
        </>
      )}
      <h3>排除条件</h3>
      <p>候选同时包含“必须包含”的全部标签，且不包含“不得包含”的任一标签时排除；任意一项条件命中即排除。</p>
      <div className={ui.actions}>
        <Button
          disabled={plan.exclude.length >= 100}
          onClick={() => {
            update({ ...plan, exclude: [...plan.exclude, { all_tags: [], none_tags: [] }] });
            setPredicateIndex(plan.exclude.length);
          }}
        >
          新增排除条件
        </Button>
        {predicate && (
          <Button
            danger
            onClick={() => update({ ...plan, exclude: plan.exclude.filter((_, i) => i !== pi) })}
          >
            删除当前排除条件
          </Button>
        )}
      </div>
      {predicate && (
        <>
          <Field id="predicate-select" label="当前排除条件">
            <select
              id="predicate-select"
              value={pi}
              onChange={(e) => setPredicateIndex(Number(e.target.value))}
            >
              {plan.exclude.map((_, i) => (
                <option key={i} value={i}>
                  条件 {i + 1}
                </option>
              ))}
            </select>
          </Field>
          {!predicate.all_tags.length && !predicate.none_tags.length && (
            <p className={ui.warning}>两个标签列表都为空会排除全部候选和格子，请填写条件。</p>
          )}
          {(['all_tags', 'none_tags'] as const).map((key) => (
            <Field
              key={key}
              id={'predicate-' + key}
              label={key === 'all_tags' ? '排除：必须包含（每行一个）' : '排除：不得包含（每行一个）'}
            >
              <textarea
                id={'predicate-' + key}
                rows={3}
                value={predicate[key].join('\n')}
                onChange={(e) =>
                  update({
                    ...plan,
                    exclude: plan.exclude.map((row, i) =>
                      i === pi ? { ...row, [key]: lines(e.target.value) } : row,
                    ),
                  })
                }
              />
            </Field>
          ))}
        </>
      )}
      <Field
        id="plan-no-humans"
        label="无人物时不抽取的维度（每行一个）"
        hint="仅影响 no humans 特征组合；不允许压制主维度。留空表示没有此兼容规则。"
      >
        <textarea
          id="plan-no-humans"
          rows={3}
          value={plan.no_humans_excludes.join('\n')}
          onChange={(e) => update({ ...plan, no_humans_excludes: lines(e.target.value) })}
        />
      </Field>
      {layout && (
        <div role="status">
          <p>
            有效格子 {layout.active_cells.length} 个；
            {layout.seed_required
              ? '种子未固定，不预先给出随机余数分配。'
              : layout.cell_targets
                ? '已按冻结种子计算计划配额。'
                : '当前条件无法计算有效配额。'}
          </p>
          {layout.issues.map((issue) => (
            <p className={ui.warning} key={issue}>
              {layoutIssueText[issue] ?? issue}
            </p>
          ))}
          {Object.entries(layout.constraint_bounds).map(([tag, bounds]) => (
            <p key={tag}>
              {tag}：最终数量要求 {bounds[0]}–{bounds[1]} 条
            </p>
          ))}
          <p>配额预览不读取词典候选，不能证明实际数据能满足约束。保存并载入采样页后仍需预览覆盖。</p>
        </div>
      )}
      {!layout && <p className={ui.warning}>表单已有修改，计划配额需重新预览。</p>}
      <Modal
        open={removeDimension !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveDimension(null);
        }}
        title="删除这个主维度？"
        description="将从方案草稿移除该维度和取值。旧格子权重保留供核对；其他约束和排除条件不会自动改写。"
      >
        <div className={ui.actions}>
          <Button onClick={() => setRemoveDimension(null)}>保留主维度</Button>
          <Button
            danger
            disabled={disabled}
            onClick={() => {
              const name = plan.primary_dimensions[removeDimension!];
              update({
                ...plan,
                primary_dimensions: plan.primary_dimensions.filter((_, i) => i !== removeDimension),
                cell_values: Object.fromEntries(
                  Object.entries(plan.cell_values).filter(([key]) => key !== name),
                ),
              });
              setRemoveDimension(null);
            }}
          >
            确认删除主维度
          </Button>
        </div>
      </Modal>
    </fieldset>
  );
}
