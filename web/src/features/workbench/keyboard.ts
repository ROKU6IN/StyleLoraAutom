export function isEditingTarget(target: EventTarget | null) {
  return (
    target instanceof Element &&
    Boolean(
      target.closest('input,textarea,select,[contenteditable="true"],[role="textbox"],[role="combobox"]'),
    )
  );
}
export function reviewKey(key: string) {
  return ({ k: 'keep', x: 'reject', u: 'unreviewed' } as const)[key.toLowerCase() as 'k' | 'x' | 'u'];
}
