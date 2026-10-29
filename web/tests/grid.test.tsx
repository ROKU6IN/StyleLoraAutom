import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Grid } from '../src/features/workbench/Grid';
import type { ImageItem } from '../src/api/types';
it('virtualizes ten thousand images and pins the focused row without mounting the dataset', () => {
  const items = Array.from(
    { length: 10_000 },
    (_, index) =>
      ({
        image_id: index + 1,
        filename: `${index + 1}.png`,
        file_missing: true,
        has_alpha: null,
        width: 640,
        height: 480,
        review_status: 'unreviewed',
      }) as ImageItem,
  );
  render(
    <Grid
      items={items}
      total={items.length}
      focus={9000}
      selection={{ kind: 'ids', ids: new Set([2, 9000]) }}
      onActivate={vi.fn()}
      onPreview={vi.fn()}
      onMore={vi.fn()}
      hasMore={false}
      loading={false}
    />,
  );
  expect(screen.getAllByRole('gridcell').length).toBeLessThan(80);
  expect(screen.getByRole('gridcell', { name: /9000.png.*未审/ })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByRole('grid')).toHaveAttribute('aria-rowcount', '2000');
});
