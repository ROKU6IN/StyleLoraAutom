import { normalizeDraftTag } from './dictionaryEditorData';
import type { CoverageView } from './coveragePlanData';

export type ConstraintForm = {
  dimension: string;
  tag: string;
  minimum_ratio: string;
  maximum_ratio: string;
  minimum_count: string;
  maximum_count: string;
};
export type PredicateForm = { all_tags: string[]; none_tags: string[] };
export type PlanForm = {
  schema_version: 1;
  algorithm_version: 'coverage-v1';
  primary_dimensions: string[];
  cell_values: Record<string, string[]>;
  cell_weights: Record<string, string>;
  exclude: PredicateForm[];
  no_humans_excludes: string[];
  target_count: string;
  random_seed: string;
  constraints: ConstraintForm[];
};
export type CoverageForm = { schema_version: 1; name: string; plan: PlanForm };
export type Layout = {
  active_cells: string[];
  cell_targets: Record<string, number> | null;
  issues: string[];
  constraint_bounds: Record<string, number[]>;
  seed_required: boolean;
};
export type EditorView = CoverageView & { form: CoverageForm; layout: Layout };
export const lines = (value: string) => (value === '' ? [] : value.split('\n'));

// Used only to render and identify cells; quotas and validation stay on the server.
export function editorCells(plan: PlanForm) {
  const names = plan.primary_dimensions;
  if (
    names.length < 2 ||
    names.length > 3 ||
    new Set(names).size !== names.length ||
    names.some((name) => !name.trim())
  )
    return null;
  const values = Object.fromEntries(
    names.map((name) => [name, (plan.cell_values[name] ?? []).map(normalizeDraftTag)]),
  );
  if (
    Object.values(values).some(
      (tags) => !tags.length || tags.some((tag) => !tag) || new Set(tags).size !== tags.length,
    )
  )
    return null;
  if (Object.values(values).reduce((total, tags) => total * tags.length, 1) > 10000) return null;
  let combinations: string[][] = [[]];
  for (const name of names)
    combinations = combinations.flatMap((previous) => values[name].map((tag) => [...previous, tag]));
  return {
    names,
    values,
    keys: combinations.map((tags) => JSON.stringify(names.map((name, index) => [name, tags[index]]))),
  };
}

export function renameDimension(plan: PlanForm, index: number, name: string): PlanForm {
  name = name.trim();
  if (plan.primary_dimensions.some((old, i) => i !== index && old === name))
    throw new Error('维度名称不能重复，未覆盖原维度。');
  const old = plan.primary_dimensions[index],
    names = plan.primary_dimensions.map((value, i) => (i === index ? name : value));
  const weights = Object.fromEntries(
    Object.entries(plan.cell_weights).map(([key, weight]) => {
      try {
        const pairs = JSON.parse(key) as string[][];
        if (
          Array.isArray(pairs) &&
          pairs.length === plan.primary_dimensions.length &&
          pairs.every(
            (pair, i) => Array.isArray(pair) && pair.length === 2 && pair[0] === plan.primary_dimensions[i],
          )
        )
          return [
            JSON.stringify(pairs.map(([dimension, tag]) => [dimension === old ? name : dimension, tag])),
            weight,
          ];
      } catch {
        /* Invalid keys stay visible for explicit cleanup. */
      }
      return [key, weight];
    }),
  );
  return {
    ...plan,
    primary_dimensions: names,
    cell_values: Object.fromEntries(
      Object.entries(plan.cell_values).map(([key, tags]) => [key === old ? name : key, tags]),
    ),
    cell_weights: weights,
  };
}

export const layoutIssueText: Record<string, string> = {
  'coverage:all_cells_excluded': '所有格子都被排除，无法生成覆盖集合。',
  'coverage:target_below_cells': '目标数量少于有效格子数；每个有效格子至少需要一条。',
  'coverage:excluded_cell_weight': '已排除格子仍有自定义权重，请恢复这些格子的默认权重后再预览。',
  'coverage:contradictory_constraints': '比例与数量上下界存在冲突，请调整约束。',
};
