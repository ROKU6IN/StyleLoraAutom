import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { defaultRangeExtractor, useVirtualizer } from '@tanstack/react-virtual';
import clsx from 'clsx';
import type { ImageItem } from '../../api/types';
import type { Selection } from '../../state/selection';
import { usePrefs } from '../../state/prefs';
import { useMedia } from '../../hooks/useMedia';
import { geometry } from '../../tokens/base';
import { Badge, ui } from '../../ui';
import styles from './workbench.module.css';
function Thumbnail({ image }: { image: ImageItem }) {
  const media = useMedia(image.file_missing ? null : image.thumbnail_url);
  return (
    <div className={styles.thumbnail}>
      {media.url ? (
        <img src={media.url} alt="" loading="lazy" draggable={false} />
      ) : (
        <span>{image.file_missing ? '文件缺失' : media.failed ? '预览不可用' : '…'}</span>
      )}
      {image.has_alpha === true && (
        <span className={styles.alpha} title="含透明通道，原图预览可切换背景">
          α
        </span>
      )}
    </div>
  );
}
export function Grid({
  items,
  total,
  focus,
  selection,
  sameFilter = true,
  onActivate,
  onPreview,
  onMore,
  hasMore,
  loading,
  onColumnsChanged,
}: {
  items: ImageItem[];
  total: number;
  focus: number | null;
  selection: Selection;
  sameFilter?: boolean;
  onActivate: (id: number, event?: MouseEvent) => void;
  onPreview: () => void;
  onMore: () => void;
  hasMore: boolean;
  loading: boolean;
  onColumnsChanged?: (columns: number) => void;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(geometry.initialWidth);
  const density = usePrefs((s) => s.density);
  const size = geometry[density];
  const cols = Math.max(1, Math.floor(width / (size.cell + geometry.gap)));
  useEffect(() => onColumnsChanged?.(cols), [cols, onColumnsChanged]);
  const focusIndex = items.findIndex((i) => i.image_id === focus),
    focusRow = focusIndex < 0 ? -1 : Math.floor(focusIndex / cols);
  const virtual = useVirtualizer({
    count: Math.ceil(items.length / cols),
    getScrollElement: () => parent.current,
    estimateSize: () => size.row,
    overscan: geometry.overscan,
    initialRect: { width: geometry.initialWidth, height: geometry.initialHeight },
    rangeExtractor: (range) => {
      const values = defaultRangeExtractor(range);
      if (focusRow >= 0 && !values.includes(focusRow)) values.push(focusRow);
      return values.sort((a, b) => a - b);
    },
  });
  useEffect(() => {
    if (!parent.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(parent.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (focusRow >= 0) virtual.scrollToIndex(focusRow, { align: 'auto' });
  }, [focusRow, virtual]);
  const rows = virtual.getVirtualItems(),
    lastVisible = rows.filter((row) => row.index !== focusRow).at(-1)?.index ?? 0;
  useEffect(() => {
    if (hasMore && !loading && lastVisible >= Math.ceil(items.length / cols) - 4) onMore();
  }, [lastVisible, items.length, cols, hasMore, loading, onMore]);
  return (
    <div
      ref={parent}
      className={styles.gridScroll}
      data-testid="image-grid"
      role="grid"
      aria-label="图片列表"
      aria-rowcount={Math.ceil(total / cols)}
      aria-colcount={cols}
      aria-multiselectable="true"
    >
      <div className={styles.gridCanvas} style={{ height: virtual.getTotalSize() }}>
        {rows.map((row) => (
          <div
            key={row.key}
            role="row"
            aria-rowindex={row.index + 1}
            className={styles.gridRow}
            style={{
              transform: `translateY(${row.start}px)`,
              gridTemplateColumns: `repeat(${cols},minmax(0,1fr))`,
            }}
          >
            {items.slice(row.index * cols, (row.index + 1) * cols).map((image, column) => {
              const selected = selection.kind === 'filter' ? sameFilter : selection.ids.has(image.image_id);
              return (
                <div
                  role="gridcell"
                  aria-colindex={column + 1}
                  aria-selected={selected}
                  tabIndex={
                    focus === image.image_id || (focus === null && row.index === 0 && column === 0) ? 0 : -1
                  }
                  data-image-id={image.image_id}
                  key={image.image_id}
                  className={clsx(
                    styles.tile,
                    focus === image.image_id && styles.focused,
                    selected && styles.selected,
                  )}
                  onClick={(event) => onActivate(image.image_id, event)}
                  onDoubleClick={onPreview}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      onActivate(image.image_id);
                    }
                  }}
                >
                  <Thumbnail image={image} />
                  <div className={styles.tileInfo}>
                    <span title={image.filename}>{image.filename}</span>
                    <Badge status={image.review_status} />
                  </div>
                  <div className={styles.tileMeta}>
                    <span>
                      {image.width} × {image.height}
                    </span>
                    <span>{selected ? '已选' : '#' + image.image_id}</span>
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
      {loading && <p className={ui.caption}>正在加载更多图片…</p>}
    </div>
  );
}
