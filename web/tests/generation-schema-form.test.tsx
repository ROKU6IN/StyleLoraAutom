import { useState } from 'react';
import { expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import SchemaForm, {
  parameterIssues,
  supportedSchema,
  type FormCapabilities,
  type ParameterSchema,
} from '../src/features/GenerationSchemaForm';
import fixture from './fixtures/nai-direct-capabilities.json';

const capabilities = fixture as FormCapabilities;
function Editor({ initial = fixture.form.default_config, caps = capabilities, disabled = false } = {}) {
  const [value, setValue] = useState<Record<string, unknown>>(initial);
  return (
    <fieldset disabled={disabled}>
      <SchemaForm capabilities={caps} value={value} onChange={setValue} />
      <output data-testid="config">{JSON.stringify(value)}</output>
    </fieldset>
  );
}
const value = () => JSON.parse(screen.getByTestId('config').textContent!);
it('renders published defaults, enums and read-only fixed values without frontend model lists', () => {
  render(<Editor />);
  expect(screen.getByLabelText('模型')).toHaveValue('nai-diffusion-5-full');
  expect(screen.getByLabelText('步数')).toHaveValue('23');
  expect(screen.getByLabelText('采样器')).toHaveAttribute('readonly');
  expect(screen.getByRole('slider', { name: '步数滑块' })).toHaveAttribute('max', '50');
});
it('retains same-name parameters and zero while switching supported models', () => {
  render(<Editor />);
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '37' } });
  fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'nai-diffusion-4-5-full' } });
  expect(value().profile).toMatchObject({ model: 'nai-diffusion-4-5-full', steps: 37, cfg_rescale: 0 });
});
it('preserves invalid numeric input and fractional typing without clamping or coercing to zero', () => {
  render(<Editor />);
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '' } });
  expect(value().profile.steps).toBe('');
  expect(screen.getByLabelText('步数')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByRole('slider', { name: '步数滑块' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('提示词引导强度'), { target: { value: '6.' } });
  expect(screen.getByLabelText('提示词引导强度')).toHaveValue('6.');
  fireEvent.change(screen.getByLabelText('提示词引导强度'), { target: { value: '6.25' } });
  expect(value().profile.scale).toBe(6.25);
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '51' } });
  expect(value().profile.steps).toBe(51);
  expect(screen.getByText(/允许范围：1 至 50/)).toBeInTheDocument();
});
it('checks dimension step and retains unsupported JSON fields across form edits', () => {
  const initial = structuredClone(fixture.form.default_config);
  Object.assign(initial.profile, { future_option: 'keep-me' });
  render(<Editor initial={initial} />);
  fireEvent.change(screen.getByLabelText('横图宽度'), { target: { value: '777' } });
  expect(screen.getByText(/须为 64 的倍数/)).toBeInTheDocument();
  expect(value().profile.future_option).toBe('keep-me');
  expect(screen.getByText(/含有未支持的参数/)).toBeInTheDocument();
});
it('uses changed backend ranges and enums without replacing edited state', () => {
  const { rerender } = render(<Editor />);
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '30' } });
  const next = structuredClone(capabilities);
  next.config_schema.properties!.profile.properties!.steps.maximum = 25;
  next.config_schema.properties!.profile.properties!.model.enum!.push('future-model');
  rerender(<Editor caps={next} />);
  expect(screen.getByLabelText('步数')).toHaveValue('30');
  expect(screen.getByText(/允许范围：1 至 25/)).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'future-model' })).toBeInTheDocument();
});
it('falls back safely for unknown conditional schemas and versions', () => {
  const caps = structuredClone(capabilities);
  Object.assign(caps.config_schema, { oneOf: [{}] });
  expect(supportedSchema(caps.config_schema)).toBe(false);
  render(<Editor caps={caps} />);
  expect(screen.getByText(/参数描述暂不支持/)).toBeInTheDocument();
  expect(screen.queryByLabelText('步数')).not.toBeInTheDocument();
});
it('maps backend field errors to accessible controls and respects source locks', () => {
  const { rerender } = render(
    <SchemaForm
      capabilities={capabilities}
      value={fixture.form.default_config}
      onChange={() => {}}
      serverIssues={{ 'config.profile.steps': '后端拒绝步数' }}
    />,
  );
  expect(screen.getByLabelText('步数')).toHaveAccessibleDescription(/后端拒绝步数/);
  rerender(<Editor disabled />);
  expect(screen.getByLabelText('步数')).toBeDisabled();
});
it('validates required, fixed, boolean and fractional constraints without truthiness mistakes', () => {
  const schema: ParameterSchema = { type: 'number', minimum: 0, maximum: 1, multipleOf: 0.1 };
  expect(parameterIssues(schema, 0)).toEqual({});
  expect(parameterIssues(schema, 0.3)).toEqual({});
  expect(parameterIssues(schema, 0.35)).toHaveProperty('config');
  expect(parameterIssues({ const: false }, false)).toEqual({});
  expect(parameterIssues({ type: 'boolean' }, 'false')).toHaveProperty('config');
  expect(Object.keys(parameterIssues(capabilities.config_schema, {}))).toContain('config.profile');
});
