export const defaultCoverage = {
  primary_dimensions: ['scene', 'lighting'],
  cell_values: { scene: ['forest', 'beach', 'city street'], lighting: ['sunlight', 'night'] },
  cell_weights: {},
  exclude: [],
  no_humans_excludes: ['appearance', 'clothing', 'pose', 'expression'],
  constraints: [
    { dimension: 'count', tag: 'no humans', minimum_ratio: 0.15 },
    { dimension: 'view', tag: 'wide shot', minimum_ratio: 0.1 },
  ],
};

export type CoverageView = {
  template: string;
  name: string;
  primary_dimensions: string[];
  cell_count: number;
  target_count: number;
  random_seed: string | null;
  browser_compatible: boolean;
};
export type SavedCoverage = CoverageView & {
  coverage_id: string;
  revision: number;
  version: string;
};
export const coveragePath = '/api/prompt-production/coverage-plans';
export const initialTemplate = JSON.stringify(
  {
    schema_version: 1,
    name: '场景与光线覆盖',
    plan: { ...defaultCoverage, target_count: 100, random_seed: 42 },
  },
  null,
  2,
);

export function coverageApplication(value: CoverageView) {
  if (
    !value.browser_compatible ||
    (value.random_seed !== null && !Number.isSafeInteger(Number(value.random_seed)))
  )
    throw new Error(
      '此方案的种子超过浏览器可精确表示的范围。请在方案编辑页另存一份，将种子设为空或 0–9007199254740991 的整数。原方案完整保留。',
    );
  const { target_count, random_seed, ...rules } = (
    JSON.parse(value.template) as {
      plan: { target_count: number; random_seed: number | null; [key: string]: unknown };
    }
  ).plan;
  return {
    count: String(target_count),
    seed: random_seed === null ? '' : String(random_seed),
    rules: JSON.stringify(rules, null, 2),
  };
}
