import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import clsx from 'clsx';
import { timing } from '../tokens/base';
import styles from './ui.module.css';
export const ui = styles;

export function FloatingPanel({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const panel = useRef<HTMLElement>(null),
    close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    heading.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (
        event.key === 'Escape' &&
        !event.defaultPrevented &&
        event.target instanceof Node &&
        panel.current?.contains(event.target)
      ) {
        event.preventDefault();
        event.stopPropagation();
        close.current();
      }
    };
    window.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('keydown', escape);
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  return (
    <section
      ref={panel}
      hidden={!open}
      role="dialog"
      aria-modal="false"
      aria-label={title}
      className={styles.floatingPanel}
      data-executor-panel="true"
    >
      <div className={styles.between}>
        <h2 tabIndex={-1} ref={heading}>
          {title}
        </h2>
        <Button quiet onClick={onClose}>
          关闭执行器面板
        </Button>
      </div>
      {children}
    </section>
  );
}
const icons = import.meta.glob<string>('./icons/*.svg', { eager: true, query: '?url', import: 'default' });
export function Icon({ name }: { name: string }) {
  return (
    <span
      aria-hidden="true"
      className={styles.icon}
      style={{ maskImage: `url("${icons[`./icons/${name}.svg`] ?? icons['./icons/grid.svg']}")` }}
    />
  );
}
export function Button({
  primary,
  quiet,
  small,
  danger,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  primary?: boolean;
  quiet?: boolean;
  small?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      {...props}
      className={clsx(
        styles.button,
        primary && styles.primary,
        quiet && styles.quiet,
        small && styles.small,
        danger && styles.danger,
        className,
      )}
    />
  );
}
export function Badge({ status }: { status: string }) {
  const names: Record<string, string> = { keep: '保留', reject: '剔除', unreviewed: '未审' };
  return (
    <span className={clsx(styles.badge, styles[status])}>
      <span aria-hidden="true">{status === 'keep' ? '✓' : status === 'reject' ? '×' : '○'}</span>
      {names[status] ?? status}
    </span>
  );
}
export function Field({
  id,
  label,
  children,
  hint,
}: {
  id: string;
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      {children}
      {hint && <span className={styles.muted}>{hint}</span>}
    </div>
  );
}
export function ErrorBox({ message }: { message?: string }) {
  return message ? (
    <div role="alert" className={styles.error}>
      {message}
    </div>
  ) : null;
}
export function EmptyState({
  title,
  description,
  children,
  icon = 'grid',
}: {
  title: string;
  description: string;
  children?: ReactNode;
  icon?: string;
}) {
  return (
    <div className={styles.empty}>
      <span className={styles.emptyMark}>
        <Icon name={icon} />
      </span>
      <h2>{title}</h2>
      <p>{description}</p>
      {children}
    </div>
  );
}
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  wide = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={clsx(styles.dialog, wide && styles.wide)}>
          <Dialog.Title className={styles.dialogTitle}>{title}</Dialog.Title>
          <Dialog.Description className={styles.dialogDescription}>{description}</Dialog.Description>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function HoldButton({
  onConfirm,
  children,
  disabled = false,
}: {
  onConfirm: () => void;
  children: ReactNode;
  disabled?: boolean;
}) {
  const [pressed, setPressed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stop = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    setPressed(false);
  };
  const start = () => {
    if (disabled || timer.current) return;
    setPressed(true);
    timer.current = setTimeout(() => {
      timer.current = null;
      setPressed(false);
      onConfirm();
    }, timing.hold);
  };
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return (
    <Button
      danger
      disabled={disabled}
      className={styles.hold}
      aria-pressed={pressed}
      title="按住 1 秒确认"
      onPointerDown={start}
      onPointerUp={stop}
      onPointerLeave={stop}
      onPointerCancel={stop}
      onBlur={stop}
      onKeyDown={(event) => {
        if ([' ', 'Enter'].includes(event.key)) {
          event.preventDefault();
          if (!event.repeat) start();
        }
      }}
      onKeyUp={(event) => {
        if ([' ', 'Enter'].includes(event.key)) stop();
      }}
    >
      {pressed ? '继续按住…' : children}
      <small> · 按住 1 秒</small>
    </Button>
  );
}
export function TaskProgress({ done, total, label }: { done: number; total: number | null; label: string }) {
  return (
    <div className={styles.stack}>
      <div className={styles.progressLabel}>
        <span>{label}</span>
        <span className={styles.mono}>
          {done} / {total ?? '—'}
        </span>
      </div>
      <progress aria-label={label} {...(total && total > 0 ? { max: total, value: done } : {})} />
    </div>
  );
}
