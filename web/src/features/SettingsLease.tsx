import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { useSession } from '../state/session';
import { errorText } from '../api/errors';
import { ErrorBox, ui } from '../ui';

export interface LeaseStatus {
  blocked: boolean;
  phase: string;
  reason: string | null;
  inflight: boolean | null;
  source_id: number | null;
  changed_keys: string[];
  recovery_adapter_available: boolean;
}
const reasons: Record<string, string> = {
  'profile:lease_active': '一份来源正在占用设置，结束并完成恢复前不能交给另一份来源。',
  'profile:lease_busy': '正在检查或处理设置占用，请稍候。',
  'profile:apply_unknown': '参数应用结果尚未核对。读取到原值也不能证明写入已经结束。',
  'profile:restore_unknown': '恢复写入结果尚未核对，需要确认该次写入已结束，不能直接重复恢复。',
  'profile:restore_pending': '设置尚待恢复，后续来源保持阻塞。',
  'profile:restore_conflict': '当前设置与原值、应用值不一致。保留外部修改，等待核对。',
  'profile:invalid_journal': '恢复记录格式或校验失败，保留原文件，不能自动解除阻塞。',
  'profile:journal_unavailable': '暂时无法读取恢复记录，不能确认设置状态。',
  'profile:journal_changed': '恢复记录在操作期间发生变化，需要核对。',
};

export default function SettingsLease() {
  const connection = useSession((state) => state.connection);
  const query = useQuery({
    queryKey: ['backends', 'settings-lease'],
    queryFn: ({ signal }) =>
      request<LeaseStatus>('GET', '/api/backends/nai_launcher/settings-lease', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const value = query.data;
  return (
    <section className={ui.section} aria-label="生成设置恢复状态">
      <h2>生成设置恢复状态</h2>
      <p>项目内的“来源与导入”可查看各条目的恢复证据，并在具备适配器与完整证据时执行对账和恢复。</p>
      <ErrorBox message={query.error ? errorText(query.error) : undefined} />
      {value && !query.error ? (
        <>
          <p role="status">{value.blocked ? '设置尚未释放' : '没有待处理的设置恢复记录'}</p>
          {value.reason && <p className={ui.notice}>{reasons[value.reason] ?? '设置状态需要核对。'}</p>}
          {value.inflight && <p className={ui.notice}>仍有生成结果未确定，暂不能恢复设置。</p>}
          {value.source_id !== null && <p>关联来源编号：{value.source_id}</p>}
          {!!value.changed_keys.length && <p>本次改动字段：{value.changed_keys.join('、')}</p>}
          {!value.recovery_adapter_available && (
            <p className={ui.muted}>真实恢复适配尚未开放；此处只读取本地记录，不修改 Launcher 设置。</p>
          )}
        </>
      ) : (
        !query.error && <p>正在读取本地恢复记录…</p>
      )}
    </section>
  );
}
