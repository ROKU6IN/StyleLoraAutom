import { useState } from 'react';
import { expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import DictionaryEditor from '../src/features/DictionaryEditor';
import {
  dictionaryDraftIssues,
  readDictionaryContent,
  type DictionaryKind,
} from '../src/features/dictionaryEditorData';

const entries = [
  { tags: ['no humans', 'forest'], natural_language: 'A forest.', aspect: 'landscape', rating: null },
  { tags: ['1girl', 'beach'], natural_language: null, aspect: null, rating: 'sensitive' },
];
function mount(value: unknown = entries, kind: DictionaryKind = 'prompts', disabled = false) {
  function Editor() {
    const [content, setContent] = useState(JSON.stringify(value));
    return <DictionaryEditor kind={kind} content={content} onChange={setContent} disabled={disabled} />;
  }
  render(<Editor />);
}
const text = () => (screen.getByLabelText(/^(提示词条目|特征维度)（JSON）$/) as HTMLTextAreaElement).value;
const draft = () => JSON.parse(text());
const edit = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

it('edits ordered tags, description, aspect and nullable rating in the shared draft', () => {
  mount();
  edit('有序标签（每行一个）', 'no humans\nBlue_Sky\nforest');
  edit('条目自然语言描述', 'A river, and trees.\nSecond line.');
  edit('条目图幅', 'square');
  edit('条目评级', 'general');
  expect(draft()[0]).toEqual({
    tags: ['no humans', 'Blue_Sky', 'forest'],
    natural_language: 'A river, and trees.\nSecond line.',
    aspect: 'square',
    rating: 'general',
  });
  edit('条目评级', '');
  edit('条目图幅', '');
  edit('条目自然语言描述', '');
  expect(draft()[0]).toMatchObject({ rating: null, aspect: null, natural_language: null });
  expect(draft()[1]).toEqual(entries[1]);
});

it('adds, copies, reorders and confirms deletion, with an immediate draft undo', () => {
  mount();
  click('新增提示词条目');
  expect(screen.getByLabelText('条目评级')).toHaveValue('');
  expect(dictionaryDraftIssues('prompts', text())[0]).toContain('第 3 条');
  edit('有序标签（每行一个）', 'no humans\nmountain');
  click('条目上移');
  expect(draft()[1].tags).toEqual(['no humans', 'mountain']);
  click('复制当前条目');
  expect(draft()).toHaveLength(4);
  click('删除当前条目');
  click('保留内容');
  expect(draft()).toHaveLength(4);
  click('删除当前条目');
  click('确认从草稿删除');
  expect(draft()).toHaveLength(3);
  click('撤销最近一次删除');
  expect(draft()).toHaveLength(4);
});

it('search and pagination edit original row identities and preserve all hidden rows', () => {
  const rows = Array.from({ length: 45 }, (_, i) => ({ tags: ['no humans', 'item ' + i], rating: null }));
  mount(rows);
  expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(21);
  click('条目下一页');
  click('编辑第 22 条');
  edit('条目自然语言描述', 'only the twenty-second row');
  edit('检索当前条目', 'item_44');
  click('编辑第 45 条');
  expect(screen.getByRole('button', { name: '条目上移' })).toBeDisabled();
  edit('条目评级', 'sensitive');
  expect(draft()).toHaveLength(45);
  expect(draft()[44].rating).toBe('sensitive');
  expect(draft()[21].natural_language).toBe('only the twenty-second row');
  expect(draft()[20]).toEqual(rows[20]);
});

it('adds and renames feature dimensions, edits candidates and deletes a whole dimension', () => {
  mount(
    { scene: [{ tag: 'forest', rating: 'general' }], view: [{ tag: 'wide shot', rating: null }] },
    'features',
  );
  edit('维度名称', 'environment');
  expect(Object.keys(draft())).toEqual(['environment', 'view']);
  click('新增候选标签');
  edit('候选标签', 'city_street');
  edit('条目评级', 'sensitive');
  click('条目上移');
  expect(draft().environment[0]).toEqual({ tag: 'city_street', rating: 'sensitive' });
  click('新增维度');
  edit('维度名称', 'lighting');
  edit('候选标签', 'sunlight');
  expect(draft().lighting[0].rating).toBeNull();
  click('删除当前维度');
  expect(screen.getByRole('dialog')).toHaveTextContent('lighting');
  click('确认从草稿删除');
  expect(draft().lighting).toBeUndefined();
  click('撤销最近一次删除');
  expect(draft().lighting[0].tag).toBe('sunlight');
});

it('prevents dimension name collisions and safely round-trips special object keys', () => {
  mount(JSON.parse('{"scene":[{"tag":"forest"}],"__proto__":[{"tag":"sunlight"}]}'), 'features');
  edit('维度名称', ' __proto__ ');
  expect(screen.getByRole('alert')).toHaveTextContent('已有同名维度');
  expect(Object.keys(draft())).toEqual(['scene', '__proto__']);
  edit('当前特征维度', '__proto__');
  edit('候选标签', 'night');
  expect(draft().__proto__).toEqual([{ tag: 'night' }]);
});

it('allows temporary blank fields in the draft and identifies invalid rows without losing input', () => {
  mount();
  edit('有序标签（每行一个）', 'forest\n');
  expect(screen.getByLabelText('有序标签（每行一个）')).toHaveValue('forest\n');
  expect(screen.getByRole('status')).toHaveTextContent('第 1 条');
  edit('有序标签（每行一个）', 'forest');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('keeps invalid JSON and unknown fields intact instead of converting or dropping them', () => {
  mount();
  edit('提示词条目（JSON）', '[{"tags":["forest"],"future_field":7}]');
  expect(screen.queryByLabelText('有序标签（每行一个）')).not.toBeInTheDocument();
  expect(draft()[0].future_field).toBe(7);
  edit('提示词条目（JSON）', '[broken');
  expect(text()).toBe('[broken');
  edit('提示词条目（JSON）', JSON.stringify(entries));
  click('编辑第 1 条');
  expect(screen.getByLabelText('有序标签（每行一个）')).toHaveValue('no humans\nforest');
});

it('disables all edits while a save is in flight or the session cannot write', () => {
  mount(entries, 'prompts', true);
  expect(screen.getByLabelText('有序标签（每行一个）')).toBeDisabled();
  expect(screen.getByLabelText('提示词条目（JSON）')).toBeDisabled();
  expect(screen.getByRole('button', { name: '复制当前条目' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '新增提示词条目' })).toBeDisabled();
});

it('does not silently coerce unknown ratings or oversized inputs into editable data', () => {
  expect(readDictionaryContent('prompts', '[{"tags":["forest"],"rating":"safe"}]')).toBeNull();
  expect(readDictionaryContent('features', '{"scene":[{"tag":"forest","rating":true}]}')).toBeNull();
  expect(dictionaryDraftIssues('features', '{}')).toContain('特征词典需 1–20 个维度。');
  expect(dictionaryDraftIssues('features', '{"scene":[]}')[0]).toContain('至少需要一个');
  expect(dictionaryDraftIssues('features', '{"scene":[]," scene ":[]}')).toContain(
    '维度名称去除首尾空白后不能重复。',
  );
  expect(dictionaryDraftIssues('prompts', JSON.stringify([{ tags: Array(101).fill('tag') }]))[0]).toContain(
    '1–100',
  );
});

it('keeps zero prompt entries representable and restores editing by adding a row', () => {
  mount([]);
  expect(dictionaryDraftIssues('prompts', '[]')).toEqual([]);
  click('新增提示词条目');
  edit('有序标签（每行一个）', 'forest');
  expect(draft()).toHaveLength(1);
  expect(draft()[0].rating).toBeNull();
});

it('a further edit invalidates the deletion undo instead of reverting unrelated new work', () => {
  mount();
  click('删除当前条目');
  click('确认从草稿删除');
  expect(screen.getByRole('button', { name: '撤销最近一次删除' })).toBeEnabled();
  click('编辑第 1 条');
  edit('条目自然语言描述', 'new work');
  expect(screen.queryByRole('button', { name: '撤销最近一次删除' })).not.toBeInTheDocument();
});
