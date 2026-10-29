import { lazy, Suspense, useEffect } from 'react';
import {
  createHashRouter,
  Link,
  NavLink,
  Outlet,
  RouterProvider,
  useOutletContext,
  useParams,
  useNavigate,
} from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { bootstrap, onAuthFailure, request, setSessionToken, takeBootstrapCode } from './api/client';
import { mediaCache } from './api/media';
import { AppError } from './api/errors';
import type { Project, Session } from './api/types';
import { useEvents } from './hooks/useEvents';
import { DraftProvider, NavigationGuard } from './hooks/drafts';
import { useSelection } from './state/selection';
import { useSession } from './state/session';
import { Button, EmptyState, Icon, ui } from './ui';
import { ThemeProvider } from './ui/theme';
import styles from './app.module.css';
import StorageNotice from './features/StorageNotice';
const Projects = lazy(() => import('./features/Projects')),
  Workbench = lazy(() => import('./features/workbench/Workbench')),
  Sources = lazy(() => import('./features/Sources')),
  Exports = lazy(() => import('./features/Exports')),
  Schemes = lazy(() => import('./features/Schemes')),
  Tasks = lazy(() => import('./features/Tasks')),
  Settings = lazy(() => import('./features/Settings')),
  Storage = lazy(() => import('./features/Storage')),
  GenerationPresets = lazy(() => import('./features/GenerationPresets')),
  Launcher = lazy(() => import('./features/Launcher')),
  NaiDirect = lazy(() => import('./features/NaiDirect')),
  Models = lazy(() => import('./features/Models')),
  LLMSettings = lazy(() => import('./features/LLMSettings')),
  Jobs = lazy(() => import('./features/Jobs')),
  JobBatches = lazy(() => import('./features/JobBatches')),
  PromptProduction = lazy(() => import('./features/PromptProduction')),
  CoveragePlans = lazy(() => import('./features/CoveragePlans')),
  PromptReview = lazy(() => import('./features/PromptReview')),
  VisionJobs = lazy(() => import('./features/VisionJobs')),
  VisionPolicy = lazy(() => import('./features/VisionPolicy')),
  Duplicates = lazy(() => import('./features/Duplicates')),
  CatalogPage = lazy(() => import('./features/cleaning/CatalogPage')),
  RulesPage = lazy(() => import('./features/cleaning/RulesPage')),
  OperationLog = lazy(() => import('./features/cleaning/OperationLog'));
const bootstrapCode = takeBootstrapCode();
let started = false;
function Connect() {
  const state = useSession();
  const navigate = useNavigate();
  useEffect(() => {
    if (started) return;
    started = true;
    onAuthFailure(() => {
      setSessionToken(null);
      useSession.getState().setConnection('reauth');
    });
    void bootstrap(bootstrapCode)
      .then((session) => {
        useSession.getState().setSession(session);
        if (session.project_id) void navigate(`/p/${session.project_id}/workbench`, { replace: true });
      })
      .catch(() => useSession.getState().setConnection('reauth'));
  }, [navigate]);
  if (!state.session)
    return (
      <div className={styles.connect}>
        <div className={styles.wordmark}>
          STYLE<span>LORA</span>
          <small>LOCAL DATA STUDIO</small>
        </div>
        <EmptyState
          title={state.connection === 'booting' ? '正在连接本地服务' : '重新连接本地服务'}
          description={
            state.connection === 'booting'
              ? '正在建立安全会话…'
              : '请在项目终端运行下面的命令，打开新的授权页面。刷新页面后需要重新授权。'
          }
          icon="folder"
        >
          {state.connection !== 'booting' && <code className={ui.caption}>python -m stylelora ui</code>}
        </EmptyState>
      </div>
    );
  return (
    <DraftProvider>
      <Shell />
    </DraftProvider>
  );
}
function Shell() {
  const session = useSession((s) => s.session)!,
    connection = useSession((s) => s.connection),
    notice = useSession((s) => s.notice);
  const current = useQuery({
    queryKey: ['current'],
    queryFn: ({ signal }) => request<Project | null>('GET', '/api/projects/current', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 5000,
  });
  const project = current.data ?? null;
  useEvents(connection === 'reauth' ? undefined : session.instance_id, project?.project_id ?? null);
  useEffect(() => {
    mediaCache.clear();
    useSelection.getState().reset(project?.project_id ?? null);
    return () => {
      mediaCache.clear();
    };
  }, [project?.project_id]);
  useEffect(() => {
    let active = true;
    const ping = async () => {
      try {
        const value = await request<Session>('GET', '/api/session');
        if (!active) return;
        if (value.draining) useSession.getState().setConnection('draining');
        else if (useSession.getState().connection === 'offline')
          useSession.getState().setConnection('degraded');
      } catch (error) {
        if (active && !(error instanceof AppError && error.status === 401))
          useSession.getState().setConnection('offline');
      }
    };
    const timer = setInterval(() => void ping(), 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => useSession.getState().notify(''), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  const nav = [
    { path: '/', icon: 'folder', label: '项目概览' },
    ...(project
      ? [
          { path: `/p/${project.project_id}/workbench`, icon: 'grid', label: '数据工作台' },
          { path: `/p/${project.project_id}/sources`, icon: 'folder', label: '来源与导入' },
          { path: `/p/${project.project_id}/exports`, icon: 'export', label: '导出数据集' },
          { path: `/p/${project.project_id}/schemes`, icon: 'sliders', label: '导入导出方案' },
          { path: `/p/${project.project_id}/log`, icon: 'activity', label: '规则操作日志' },
          { path: `/p/${project.project_id}/duplicates`, icon: 'grid', label: '近似重复扫描' },
          { path: `/p/${project.project_id}/jobs`, icon: 'activity', label: 'WD 标注任务' },
          { path: `/p/${project.project_id}/batches`, icon: 'activity', label: '标注批次' },
          { path: `/p/${project.project_id}/vision`, icon: 'activity', label: '视觉描述任务' },
        ]
      : []),
    { path: '/tasks', icon: 'activity', label: '任务进度' },
    { path: '/prompt-production', icon: 'grid', label: '提示词生产' },
    { path: '/settings/tags', icon: 'grid', label: '本地标签库' },
    { path: '/settings/rules', icon: 'sliders', label: '替换规则库' },
    { path: '/settings/launcher', icon: 'activity', label: 'Launcher / MCP' },
    { path: '/settings/nai-direct', icon: 'activity', label: 'NovelAI 直连' },
    { path: '/settings/presets', icon: 'sliders', label: '尺寸预设' },
    { path: '/settings/models', icon: 'sliders', label: 'WD 模型管理' },
    { path: '/settings/llm', icon: 'sliders', label: 'LLM 连接与角色' },
    { path: '/settings/vision-policy', icon: 'sliders', label: '视觉关键词' },
    { path: '/settings/storage', icon: 'folder', label: '存储占用与清理' },
    { path: '/settings', icon: 'sliders', label: '界面设置' },
  ];
  return (
    <div className={styles.shell}>
      <a
        className="skip-link"
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById('main')?.focus();
        }}
      >
        跳到主要内容
      </a>
      <NavigationGuard />
      <aside className={styles.sidebar}>
        <Link to="/" className={styles.wordmark}>
          STYLE<span>LORA</span>
          <small>LOCAL DATA STUDIO</small>
        </Link>
        <div className={styles.projectLabel}>
          <span className={ui.eyebrow}>WORKSPACE</span>
          <strong>{project?.metadata.name ?? '尚未打开项目'}</strong>
        </div>
        <nav aria-label="主导航">
          {nav.map((item) => (
            <NavLink
              key={item.path}
              end
              to={item.path}
              aria-label={item.label}
              title={item.label}
              className={({ isActive }) => (isActive ? styles.active : '')}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className={styles.sidebarFoot}>
          <span className={ui.mono}>PRIVATE BY DESIGN</span>
          <p>图片与标注存于本地</p>
          <span className={ui.mono}>v0.59.0</span>
        </div>
      </aside>
      <div className={styles.main}>
        <header className={styles.header}>
          <span>{project ? '项目 / ' + project.metadata.name : '本地工作空间'}</span>
          <div className={ui.row}>
            <span className={styles.connection} data-state={connection}>
              ●{' '}
              {
                {
                  online: '已连接',
                  degraded: '事件重连中',
                  offline: '连接中断',
                  draining: '正在关闭',
                  booting: '连接中',
                  reauth: '需要授权',
                }[connection]
              }
            </span>
            <span className={ui.mono}>HTTP · LOCAL</span>
          </div>
        </header>
        {connection !== 'online' && (
          <div role="status" className={ui.notice}>
            {connection === 'reauth'
              ? '授权已失效，草稿保留在当前页面。请运行 python -m stylelora ui 打开新的授权页面。'
              : connection === 'offline'
                ? '本地服务暂时无法连接，写入已暂停。草稿保留在当前页面。'
                : connection === 'draining'
                  ? '服务正在完成收尾，写入已暂停。'
                  : '事件连接重建中，正在通过定时查询同步状态。'}
          </div>
        )}
        {session.capabilities.storage_thresholds && <StorageNotice pid={project?.project_id} />}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- The scroll container needs a keyboard entry point, including pages without controls. */}
        <main id="main" className={styles.content} tabIndex={0}>
          <Suspense fallback={<EmptyState title="正在加载页面" description="请稍候…" />}>
            <Outlet context={{ project, loading: current.isPending, pid: session.project_id }} />
          </Suspense>
        </main>
        {notice && (
          <div role="status" className={styles.toast}>
            {notice}
            <Button small quiet onClick={() => useSession.getState().notify('')}>
              关闭
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
type Context = { project: Project | null; loading: boolean };
function ProjectRoute({
  page,
}: {
  page:
    | 'workbench'
    | 'sources'
    | 'exports'
    | 'log'
    | 'schemes'
    | 'duplicates'
    | 'jobs'
    | 'vision'
    | 'batches';
}) {
  const { project, loading } = useOutletContext<Context>();
  const { pid } = useParams();
  if (loading) return <EmptyState title="正在读取项目" description="请稍候…" />;
  if (!project || project.project_id !== pid)
    return (
      <EmptyState title="项目句柄已失效" description="项目可能已关闭或切换，请返回项目概览。">
        <Link to="/">返回项目概览</Link>
      </EmptyState>
    );
  return page === 'workbench' ? (
    <Workbench key={pid} pid={pid!} />
  ) : page === 'sources' ? (
    <Sources key={pid} pid={pid!} />
  ) : page === 'log' ? (
    <OperationLog key={pid} pid={pid!} />
  ) : page === 'duplicates' ? (
    <Duplicates key={pid} pid={pid!} />
  ) : page === 'jobs' ? (
    <Jobs key={pid} pid={pid!} />
  ) : page === 'vision' ? (
    <VisionJobs key={pid} pid={pid!} />
  ) : page === 'batches' ? (
    <JobBatches key={pid} pid={pid!} />
  ) : page === 'schemes' ? (
    <Schemes key={pid} pid={pid!} />
  ) : (
    <Exports key={pid} project={project} />
  );
}
function ProjectHome() {
  const { project } = useOutletContext<Context>();
  return <Projects project={project} />;
}
function TaskPage() {
  const { project } = useOutletContext<Context>();
  return <Tasks pid={project?.project_id} />;
}
function StoragePage() {
  const { project } = useOutletContext<Context>();
  return <Storage pid={project?.project_id} />;
}
const router = createHashRouter([
  {
    element: <Connect />,
    children: [
      { index: true, element: <ProjectHome /> },
      { path: 'p/:pid/workbench', element: <ProjectRoute page="workbench" /> },
      { path: 'p/:pid/sources', element: <ProjectRoute page="sources" /> },
      { path: 'p/:pid/exports', element: <ProjectRoute page="exports" /> },
      { path: 'p/:pid/log', element: <ProjectRoute page="log" /> },
      { path: 'p/:pid/schemes', element: <ProjectRoute page="schemes" /> },
      { path: 'p/:pid/duplicates', element: <ProjectRoute page="duplicates" /> },
      { path: 'p/:pid/jobs', element: <ProjectRoute page="jobs" /> },
      { path: 'p/:pid/batches', element: <ProjectRoute page="batches" /> },
      { path: 'p/:pid/vision', element: <ProjectRoute page="vision" /> },
      { path: 'tasks', element: <TaskPage /> },
      { path: 'settings/storage', element: <StoragePage /> },
      { path: 'prompt-production', element: <PromptProduction /> },
      { path: 'coverage-plans', element: <CoveragePlans /> },
      { path: 'prompt-review/:taskId/:index', element: <PromptReview /> },
      { path: 'settings', element: <Settings /> },
      { path: 'settings/tags', element: <CatalogPage /> },
      { path: 'settings/rules', element: <RulesPage /> },
      { path: 'settings/launcher', element: <Launcher /> },
      { path: 'settings/nai-direct', element: <NaiDirect /> },
      { path: 'settings/presets', element: <GenerationPresets /> },
      { path: 'settings/models', element: <Models /> },
      { path: 'settings/llm', element: <LLMSettings /> },
      { path: 'settings/vision-policy', element: <VisionPolicy /> },
      {
        path: '*',
        element: (
          <EmptyState title="页面不存在" description="请从导航进入工作台。">
            <Link to="/">返回项目概览</Link>
          </EmptyState>
        ),
      },
    ],
  },
]);
export default function App() {
  return (
    <ThemeProvider>
      <RouterProvider router={router} />
    </ThemeProvider>
  );
}
