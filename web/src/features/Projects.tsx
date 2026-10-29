import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { request, waitOperation } from '../api/client';
import { pickDirectory, projectPath } from '../api/endpoints';
import { errorText } from '../api/errors';
import type { Accepted, Project } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Icon, ui } from '../ui';
import RecentProjects from './RecentProjects';
export default function Projects({ project }: { project: Project | null }) {
  const navigate = useNavigate(),
    action = useAction(),
    guard = useDraftGuard();
  useSession((s) => s.connection);
  const [name, setName] = useState(project?.metadata.name ?? '我的风格数据集'),
    [trigger, setTrigger] = useState(project?.metadata.trigger_word ?? '');
  useEffect(() => {
    if (project) {
      setName(project.metadata.name);
      setTrigger(project.metadata.trigger_word);
    }
  }, [project]);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['current'] });
  const open = (create: boolean) =>
    void action.run(async () => {
      const grant = await pickDirectory(create ? 'project_create' : 'project_open');
      const accepted = await request<Accepted>('POST', create ? '/api/projects' : '/api/projects/open', {
        grant_id: grant.grant_id,
        ...(create ? { name: name.trim(), trigger_word: trigger.trim() } : {}),
      });
      const value = await waitOperation<Project>(accepted);
      await refresh();
      navigate(`/p/${value.project_id}/workbench`);
    });
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>STYLELORA / LOCAL STUDIO</div>
          <h1>{project ? '项目概览' : '从一组图片开始'}</h1>
          <p>
            {project
              ? '管理项目名称与导出触发词。所有数据保存在本地项目目录。'
              : '导入、整理、审核，把图片变成可训练的数据集。'}
          </p>
        </div>
        <span className="edition">
          DATASET
          <br />
          WORKBENCH<span>0.4 / LOCAL</span>
        </span>
      </div>
      <ErrorBox message={action.error ? errorText(action.error) : undefined} />
      <ErrorBox
        message={
          project?.recent_warning ? '项目已打开，但最近项目记录未能保存。请检查本地配置文件。' : undefined
        }
      />
      <section className={ui.section}>
        <div className={ui.between}>
          <h2>{project ? '当前项目' : '创建新项目'}</h2>
          <Icon name="folder" />
        </div>
        <div className={ui.formGrid}>
          <Field id="project-name" label="项目名称">
            <input id="project-name" value={name} maxLength={512} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field id="trigger-word" label="触发词" hint="导出时放在每份标签最前面；导出前必须设置。">
            <input
              id="trigger-word"
              value={trigger}
              maxLength={1024}
              placeholder="例如 my_style"
              onChange={(e) => setTrigger(e.target.value)}
            />
          </Field>
        </div>
        <div className={ui.actions}>
          {project ? (
            <>
              <Button
                disabled={action.busy || !canWrite()}
                onClick={() =>
                  void action.run(async () => {
                    await request(
                      'PATCH',
                      projectPath(project.project_id),
                      { name: name.trim(), trigger_word: trigger.trim() },
                      { version: project.version },
                    );
                    await refresh();
                    useSession.getState().notify('项目信息已保存');
                  })
                }
              >
                保存项目信息
              </Button>
              <Button primary onClick={() => navigate(`/p/${project.project_id}/workbench`)}>
                进入工作台 →
              </Button>
            </>
          ) : (
            <Button primary disabled={action.busy || !name.trim() || !canWrite()} onClick={() => open(true)}>
              {action.busy ? '处理中…' : '选择保存位置并创建'}
            </Button>
          )}
        </div>
      </section>
      <RecentProjects />
      <div className={ui.cards}>
        {[
          { n: '01', title: '收集图片', text: '扫描目录，预览图片与 TXT 的导入结果。' },
          { n: '02', title: '整理与审核', text: '逐图编辑标签，保留原始记录并确认训练范围。' },
          { n: '03', title: '导出训练集', text: '检查最终标注，生成图片、TXT 和导出报告。' },
        ].map((card) => (
          <article className={ui.section} key={card.n}>
            <span className={ui.eyebrow}>{card.n}</span>
            <h2>{card.title}</h2>
            <p className={ui.muted}>{card.text}</p>
          </article>
        ))}
      </div>
      <section className={ui.section}>
        <div className={ui.between}>
          <div>
            <h2>{project ? '切换或关闭项目' : '已有项目'}</h2>
            <p className={ui.muted}>选择已有的 .slproj 项目文件夹。关闭项目会等待已受理的写入结束。</p>
          </div>
          <div className={ui.row}>
            <Button disabled={action.busy || !canWrite()} onClick={() => guard.run(() => open(false))}>
              打开已有项目
            </Button>
            {project && (
              <HoldButton
                disabled={action.busy || !canWrite()}
                onConfirm={() =>
                  guard.run(
                    () =>
                      void action.run(async () => {
                        const accepted = await request<Accepted>(
                          'POST',
                          `${projectPath(project.project_id)}/close`,
                          {},
                          { version: project.version },
                        );
                        await waitOperation(accepted);
                        await refresh();
                        navigate('/');
                      }),
                  )
                }
              >
                关闭项目
              </HoldButton>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
