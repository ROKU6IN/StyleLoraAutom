import { useRef, useState } from 'react';
import { request, waitOperation } from '../api/client';
import type { Accepted } from '../api/types';
import { AppError, errorText } from '../api/errors';
import { Button, ErrorBox, HoldButton, ui } from '../ui';
import { canWrite, useSession } from '../state/session';
import { projectKey, queryClient } from '../state/queryClient';

interface Candidate {
  candidate_id: string;
  directory: string;
  files: number;
  bytes: number;
  can_publish: boolean;
  publish_directory: string | null;
  reason: string | null;
}
interface Decision {
  decision_id: string;
  action: string;
  source: string;
  target: string;
  observed: string;
}
interface Preview {
  preview_id: string;
  digest: string;
  version: string;
  candidates: Candidate[];
  decisions: Decision[];
}
export default function ExportRecovery({ pid }: { pid: string }) {
  const [preview, setPreview] = useState<Preview | null>(null),
    [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null),
    [uncertain, setUncertain] = useState(false);
  const [confirmation, setConfirmation] = useState<string | null>(null),
    [notice, setNotice] = useState('');
  const inFlight = useRef(false),
    pending = useRef<Accepted | null>(null);
  useSession((s) => s.connection);
  const base = `/api/projects/${pid}/exports/recovery`;
  async function scan() {
    if (inFlight.current || !canWrite()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setConfirmation(null);
    try {
      if (pending.current) {
        try {
          await waitOperation(pending.current);
        } catch (e) {
          if (!(e instanceof AppError) || e.outcome === 'unknown') throw e;
        }
        pending.current = null;
      }
      const accepted = await request<Accepted>('POST', base + '/scan', {});
      pending.current = accepted;
      const result = await waitOperation<Preview>(accepted);
      setPreview(result);
      pending.current = null;
      setUncertain(false);
      setNotice('');
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function apply(candidate: Candidate, action: 'publish' | 'archive') {
    if (!preview || inFlight.current || uncertain || !canWrite()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const accepted = await request<Accepted>(
        'POST',
        base + '/actions',
        {
          preview_id: preview.preview_id,
          digest: preview.digest,
          candidate_id: candidate.candidate_id,
          action,
          confirm: true,
        },
        { version: preview.version },
      );
      pending.current = accepted;
      const record = await waitOperation<{ directory: string }>(accepted);
      pending.current = null;
      setPreview(null);
      setConfirmation(null);
      setNotice(`${action === 'publish' ? '已发布完整导出' : '已归档并保留所有文件'}：${record.directory}`);
      void queryClient.invalidateQueries({ queryKey: projectKey(pid, 'exports') });
    } catch (e) {
      setError(e);
      if ((e instanceof AppError && e.outcome === 'unknown') || pending.current) setUncertain(true);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return (
    <section className={ui.section} aria-label="中断导出恢复">
      <h2>中断导出恢复</h2>
      <p>
        检查报告和全部文件摘要后，完整的复制导出可以发布。证据不足的目录只能归档保留，不删除文件、不补写标注、不重新生成图片。
      </p>
      <ErrorBox message={error ? errorText(error) : undefined} />
      {uncertain && <p role="status">处置结果未知。请重新检查记录和目录，不要重复提交。</p>}
      {notice && <p role="status">{notice}</p>}
      <Button disabled={busy || !canWrite()} onClick={() => void scan()}>
        {busy ? '正在检查或处置…' : '检查中断导出与处置记录'}
      </Button>
      {preview?.candidates.length === 0 && <p>未发现待处置目录。</p>}
      {preview?.candidates.map((candidate) => (
        <div className={ui.stack} key={candidate.candidate_id}>
          <p>
            {candidate.directory} · {candidate.files} 个文件 · {(candidate.bytes / 1024 ** 2).toFixed(2)} MiB
          </p>
          <p>
            {candidate.can_publish
              ? `完整产物可发布到 ${candidate.publish_directory}`
              : '未满足发布条件；可归档保留并重新正常导出。'}
          </p>
          <label className={ui.row}>
            <input
              type="checkbox"
              disabled={busy || uncertain}
              checked={confirmation === candidate.candidate_id}
              onChange={(e) => setConfirmation(e.target.checked ? candidate.candidate_id : null)}
            />
            我已核对这个目录及处置方式
          </label>
          <div className={ui.actions}>
            {candidate.can_publish && (
              <HoldButton
                disabled={busy || uncertain || confirmation !== candidate.candidate_id || !canWrite()}
                onConfirm={() => void apply(candidate, 'publish')}
              >
                发布完整导出
              </HoldButton>
            )}
            <HoldButton
              disabled={busy || uncertain || confirmation !== candidate.candidate_id || !canWrite()}
              onConfirm={() => void apply(candidate, 'archive')}
            >
              归档保留目录
            </HoldButton>
          </div>
        </div>
      ))}
      {preview?.decisions.map((d) => (
        <p key={d.decision_id}>
          处置 {d.decision_id}：{d.source} → {d.target}（
          {d.observed === 'moved' ? '已核实目录移动' : '尚未结清，请保留现场'}）
        </p>
      ))}
    </section>
  );
}
