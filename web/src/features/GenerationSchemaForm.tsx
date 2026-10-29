import { Field, ui } from '../ui';

type Scalar = string | number | boolean | null;
export type ParameterSchema = {
  type?: 'object' | 'integer' | 'number' | 'boolean' | 'string';
  const?: Scalar;
  enum?: Scalar[];
  minimum?: number;
  maximum?: number;
  multipleOf?: number;
  properties?: Record<string, ParameterSchema>;
  required?: string[];
  additionalProperties?: false;
};
export type FormCapabilities = {
  backend: string;
  config_schema: ParameterSchema;
  form?: {
    version: number;
    default_config: Record<string, unknown>;
    labels: Record<string, string>;
    fixed_hint: string;
    hint: string;
  };
};
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const scalar = (value: unknown) => value === null || ['string', 'boolean', 'number'].includes(typeof value);
const keywords = new Set([
  'type',
  'const',
  'enum',
  'minimum',
  'maximum',
  'multipleOf',
  'properties',
  'required',
  'additionalProperties',
]);

// Deliberately bounded dialect. Unknown schema rules must not be ignored while
// showing an apparently valid editable form (notably conditional model schemas).
export function supportedSchema(value: unknown, depth = 0): value is ParameterSchema {
  if (!object(value) || depth > 8 || Object.keys(value).some((key) => !keywords.has(key))) return false;
  if ('const' in value) return Object.keys(value).length === 1 && scalar(value.const);
  if ('enum' in value)
    return (
      Object.keys(value).length === 1 &&
      Array.isArray(value.enum) &&
      value.enum.length > 0 &&
      value.enum.every(scalar) &&
      new Set(value.enum.map(String)).size === value.enum.length
    );
  if (value.type === 'object')
    return (
      Object.keys(value).every((key) =>
        ['type', 'properties', 'required', 'additionalProperties'].includes(key),
      ) &&
      value.additionalProperties === false &&
      object(value.properties) &&
      Array.isArray(value.required) &&
      value.required.every(
        (key) => typeof key === 'string' && Object.hasOwn(value.properties as object, key),
      ) &&
      Object.values(value.properties).every((child) => supportedSchema(child, depth + 1))
    );
  if (!['integer', 'number', 'boolean', 'string'].includes(String(value.type))) return false;
  if (Object.keys(value).some((key) => !['type', 'minimum', 'maximum', 'multipleOf'].includes(key)))
    return false;
  if (['boolean', 'string'].includes(String(value.type))) return Object.keys(value).length === 1;
  return ['minimum', 'maximum', 'multipleOf'].every(
    (key) =>
      !(key in value) ||
      (typeof value[key] === 'number' &&
        Number.isFinite(value[key]) &&
        (key !== 'multipleOf' || value[key] > 0)),
  );
}
export function configObject(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    return object(value) ? value : null;
  } catch {
    return null;
  }
}
export function parameterIssues(
  schema: ParameterSchema,
  value: unknown,
  path = 'config',
): Record<string, string> {
  if ('const' in schema) return value === schema.const ? {} : { [path]: '此参数须使用后端规定的固定值。' };
  if (schema.enum) return schema.enum.includes(value as Scalar) ? {} : { [path]: '请选择后端支持的选项。' };
  if (schema.type === 'object') {
    if (!object(value)) return { [path]: '请填写配置对象。' };
    const issues: Record<string, string> = {};
    if (Object.keys(value).some((key) => !Object.hasOwn(schema.properties!, key)))
      issues[path] = '含有未支持的参数，请在高级 JSON 中核对；原值已保留。';
    for (const [key, child] of Object.entries(schema.properties!)) {
      if (!Object.hasOwn(value, key) && !schema.required?.includes(key)) continue;
      Object.assign(issues, parameterIssues(child, value[key], `${path}.${key}`));
    }
    return issues;
  }
  if (schema.type === 'number' || schema.type === 'integer') {
    if (
      typeof value !== 'number' ||
      !Number.isFinite(value) ||
      (schema.type === 'integer' && !Number.isInteger(value))
    )
      return { [path]: schema.type === 'integer' ? '请填写整数。' : '请填写数字。' };
    if (
      (schema.minimum !== undefined && value < schema.minimum) ||
      (schema.maximum !== undefined && value > schema.maximum)
    )
      return { [path]: `允许范围：${schema.minimum ?? '不限'} 至 ${schema.maximum ?? '不限'}。` };
    if (
      schema.multipleOf &&
      Math.abs(value / schema.multipleOf - Math.round(value / schema.multipleOf)) > 1e-8
    )
      return { [path]: `须为 ${schema.multipleOf} 的倍数。` };
  } else if (typeof value !== schema.type) return { [path]: '请填写正确类型的值。' };
  return {};
}
export default function GenerationSchemaForm({
  capabilities,
  value,
  onChange,
  serverIssues = {},
}: {
  capabilities: FormCapabilities;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  serverIssues?: Record<string, string>;
}) {
  const schema = capabilities.config_schema;
  if (capabilities.form?.version !== 1 || !supportedSchema(schema))
    return (
      <div>
        <p role="status">参数描述暂不支持表单编辑。请核对下方高级 JSON；保存时由后端校验。</p>
        <pre aria-label="未支持的参数配置">{JSON.stringify(value, null, 2)}</pre>
      </div>
    );
  const metadata = capabilities.form;
  const issues = { ...parameterIssues(schema, value), ...serverIssues };
  function render(node: ParameterSchema, current: unknown, path: string, change: (value: unknown) => void) {
    const label = metadata!.labels[path] ?? path;
    const id = 'parameter-' + path.replaceAll('.', '-');
    const issue = issues[path];
    if (node.type === 'object')
      return (
        <fieldset key={path}>
          <legend>{label}</legend>
          {issue && <p role="alert">{issue}</p>}
          <div className={ui.formGrid}>
            {Object.entries(node.properties!).map(([key, child]) =>
              render(child, object(current) ? current[key] : undefined, `${path}.${key}`, (next) =>
                change({ ...(object(current) ? current : {}), [key]: next }),
              ),
            )}
          </div>
        </fieldset>
      );
    const fixed = 'const' in node;
    const number = node.type === 'integer' || node.type === 'number';
    const step = node.multipleOf ?? (node.type === 'integer' ? 1 : 'any');
    const hint = fixed
      ? metadata!.fixed_hint
      : number
        ? `范围 ${node.minimum ?? '不限'}–${node.maximum ?? '不限'}${node.multipleOf ? `；步进 ${node.multipleOf}` : ''}`
        : '';
    const accessibility = { 'aria-invalid': !!issue, 'aria-describedby': id + '-help' };
    return (
      <Field key={path} id={id} label={label}>
        {fixed ? (
          <input id={id} readOnly value={current === undefined ? '' : String(current)} {...accessibility} />
        ) : node.enum ? (
          <select
            id={id}
            value={current === undefined ? '' : String(current)}
            onChange={(event) => change(node.enum!.find((v) => String(v) === event.target.value))}
            {...accessibility}
          >
            {!node.enum.includes(current as Scalar) && (
              <option value={String(current ?? '')}>未支持的当前值（已保留）</option>
            )}
            {node.enum.map((option) => (
              <option key={String(option)} value={String(option)}>
                {String(option)}
              </option>
            ))}
          </select>
        ) : node.type === 'boolean' ? (
          <input
            id={id}
            type="checkbox"
            checked={current === true}
            onChange={(event) => change(event.target.checked)}
            {...accessibility}
          />
        ) : (
          <input
            id={id}
            inputMode={number ? 'decimal' : undefined}
            value={scalar(current) ? String(current ?? '') : ''}
            {...accessibility}
            onChange={(event) => {
              const text = event.target.value;
              change(
                number && text.trim() && !text.endsWith('.') && Number.isFinite(Number(text))
                  ? Number(text)
                  : text,
              );
            }}
          />
        )}
        {number && node.minimum !== undefined && node.maximum !== undefined && !fixed && (
          <input
            type="range"
            aria-label={`${label}滑块`}
            min={node.minimum}
            max={node.maximum}
            step={step}
            disabled={!!issue}
            value={typeof current === 'number' ? current : node.minimum}
            onChange={(event) => change(Number(event.target.value))}
          />
        )}
        <span id={id + '-help'}>
          {hint}
          {issue && <span role="alert"> {issue}</span>}
        </span>
      </Field>
    );
  }
  return (
    <div aria-label="生成参数表单">
      <p>{metadata.hint}</p>
      {render(schema, value, 'config', (next) => onChange(next as Record<string, unknown>))}
    </div>
  );
}
