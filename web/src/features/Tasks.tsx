import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { allPages, request } from '../api/client';
import type { Operation } from '../api/types';
import { errorText } from '../api/errors';
import { useAction } from '../hooks/useAction';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { EmptyState, ErrorBox, HoldButton, TaskProgress, ui } from '../ui';
const names: Record<string, string> = {
  'job.wd': '执行 WD 标注任务',
  'job.llm_vision': '执行视觉描述任务',
  'rating.reclassify': '重新分级图片',
  'tagger.install': '安装 / 校验 WD 模型',
  'duplicates.scan': '近似重复图片扫描',
  'backend.probe': '检查 Launcher 连接',
  'filesystem.pick': '选择本地目录',
  'import.preview': '扫描导入目录',
  'import.run': '导入图片',
  'export.preview': '预览导出',
  'export.run': '导出图片',
  'project.create': '创建项目',
  'project.open': '打开项目',
  'project.close': '关闭项目',
  'import.root': '校验源目录',
  'image.verify_file': '核验恢复原图',
};
export default function Tasks({ pid }: { pid?: string }) {
  const navigate = useNavigate();
  const action = useAction(),
    connection = useSession((s) => s.connection);
  const tasks = useQuery({
    queryKey: ['operations', pid],
    queryFn: ({ signal }) => allPages<Operation>(`/api/operations${pid ? '?project_id=' + pid : ''}`, signal),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>ACTIVITY / 04</div>
          <h1>任务进度</h1>
          <p>当前进程的后台任务。关闭页面不会中断已受理的工作。</p>
        </div>
      </div>
      <ErrorBox
        message={action.error ? errorText(action.error) : tasks.error ? errorText(tasks.error) : undefined}
      />
      {tasks.data?.length ? (
        <div className={ui.stack}>
          {[...tasks.data].reverse().map((task) => (
            <article className={ui.section} key={task.operation_id}>
              <div className={ui.between}>
                <h2>{names[task.kind] ?? task.kind}</h2>
                <span className={ui.tag}>
                  {
                    (
                      {
                        pending: '等待中',
                        running: '进行中',
                        done: '完成',
                        failed: '失败',
                        canceled: '已取消',
                        interrupted: '已中断',
                      } as Record<string, string>
                    )[task.status]
                  }
                </span>
              </div>
              <TaskProgress done={task.completed} total={task.total} label="处理进度" />
              {task.error && <ErrorBox message={errorText(task.error)} />}
              <div className={ui.between}>
                <span className={ui.mono}>{task.operation_id}</span>
                {task.kind.startsWith('job.') && (
                  <button
                    type="button"
                    className={ui.button}
                    onClick={() =>
                      void navigate(
                        `/p/${task.project_id}/${task.kind === 'job.llm_vision' ? 'vision' : 'jobs'}`,
                      )
                    }
                  >
                    查看标注任务
                  </button>
                )}
                {task.kind === 'tagger.install' && (
                  <button
                    type="button"
                    className={ui.button}
                    onClick={() => void navigate('/settings/models')}
                  >
                    查看模型状态
                  </button>
                )}
                {task.kind === 'duplicates.scan' && task.status === 'done' && (
                  <button
                    type="button"
                    className={ui.button}
                    onClick={() =>
                      void action.run(async () => {
                        const detail = await request<Operation<{ scan_id: string }>>(
                          'GET',
                          `/api/operations/${task.operation_id}`,
                        );
                        void navigate(`/p/${task.project_id}/duplicates?scan=${detail.result.scan_id}`);
                      })
                    }
                  >
                    查看重复候选
                  </button>
                )}
                {['pending', 'running'].includes(task.status) &&
                  !task.kind.startsWith('project.') &&
                  !task.kind.startsWith('job.') && (
                    <HoldButton
                      disabled={action.busy || !canWrite()}
                      onConfirm={() =>
                        void action.run(async () => {
                          await request(
                            'POST',
                            `/api/operations/${task.operation_id}/cancel`,
                            {},
                            { version: task.version },
                          );
                          await queryClient.invalidateQueries({ queryKey: ['operations'] });
                        })
                      }
                    >
                      取消任务
                    </HoldButton>
                  )}
              </div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          icon="activity"
          title="暂无后台任务"
          description="导入、导出与项目操作的状态会显示在这里。"
        />
      )}
    </div>
  );
}
