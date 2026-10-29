import { useEffect, useRef, useState } from 'react';
import type { ImageItem, Review } from '../../api/types';
import { useMedia } from '../../hooks/useMedia';
import { usePrefs, type Stage } from '../../state/prefs';
import { Button, Modal, ui } from '../../ui';
import { canWrite, useSession } from '../../state/session';
import styles from './workbench.module.css';
export function Preview({
  image,
  onClose,
  onMove,
  onReview,
}: {
  image: ImageItem | null;
  onClose: () => void;
  onMove: (delta: number) => void;
  onReview: (status: Review) => void;
}) {
  const media = useMedia(image && !image.file_missing ? image.original_url : null),
    [scale, setScale] = useState(1),
    [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const prefs = usePrefs();
  useSession((s) => s.connection);
  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [image?.image_id]);
  return (
    <Modal
      open={Boolean(image)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={image?.filename ?? '原图预览'}
      description="原图支持拖动和缩放。← → 切换图片，K 保留，X 剔除，U 未审，B 切换背景。"
      wide
    >
      <div className={ui.between}>
        <div className={ui.row}>
          <Button small onClick={() => setScale((s) => Math.max(0.25, s / 1.25))}>
            缩小
          </Button>
          <Button
            small
            onClick={() => {
              setScale(1);
              setOffset({ x: 0, y: 0 });
            }}
          >
            适应
          </Button>
          <Button small onClick={() => setScale((s) => Math.min(8, s * 1.25))}>
            放大
          </Button>
          <span className={ui.mono}>{Math.round(scale * 100)}%</span>
        </div>
        <label>
          背景{' '}
          <select
            aria-label="预览背景"
            value={prefs.stage}
            onChange={(e) => prefs.update({ stage: e.target.value as Stage })}
          >
            <option value="checker">棋盘格</option>
            <option value="white">白色</option>
            <option value="gray">灰色</option>
            <option value="black">黑色</option>
            <option value="dark">暗底</option>
          </select>
        </label>
      </div>
      <div
        className={styles.stage}
        data-stage={prefs.stage}
        onPointerDown={(e) => {
          drag.current = { x: e.clientX - offset.x, y: e.clientY - offset.y };
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          if (drag.current) setOffset({ x: e.clientX - drag.current.x, y: e.clientY - drag.current.y });
        }}
        onPointerUp={() => {
          drag.current = null;
        }}
        onPointerCancel={() => {
          drag.current = null;
        }}
      >
        {media.url ? (
          <img
            src={media.url}
            alt={image?.filename}
            draggable={false}
            style={{ transform: `translate(${offset.x}px,${offset.y}px) scale(${scale})` }}
          />
        ) : (
          <p>{media.failed || image?.file_missing ? '原图不可用' : '正在读取原图…'}</p>
        )}
      </div>
      <div className={ui.between}>
        <div className={ui.row}>
          <Button onClick={() => onMove(-1)}>← 上一张</Button>
          <Button onClick={() => onMove(1)}>下一张 →</Button>
        </div>
        <div className={ui.row}>
          <Button disabled={!canWrite() || image?.file_missing} onClick={() => onReview('keep')}>
            保留 K
          </Button>
          <Button disabled={!canWrite()} onClick={() => onReview('reject')}>
            剔除 X
          </Button>
          <Button onClick={onClose}>关闭预览</Button>
        </div>
      </div>
    </Modal>
  );
}
