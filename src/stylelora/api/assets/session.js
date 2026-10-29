(() => {
  const code = new URLSearchParams(location.hash.slice(1)).get('bootstrap');
  history.replaceState(null, '', location.pathname);
  const status = document.getElementById('status');
  let token = null;
  async function connect() {
    if (!code) throw new Error('请运行 stylelora ui 重新建立会话。');
    const response = await fetch('/api/session/bootstrap', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code}), cache: 'no-store'
    });
    if (!response.ok) throw new Error('引导链接已失效，请运行 stylelora ui。');
    token = (await response.json()).token;
    const session = await fetch('/api/session', {headers: {Authorization: `Bearer ${token}`}, cache: 'no-store'});
    if (!session.ok) throw new Error('本地服务连接失败。');
    const data = await session.json();
    status.textContent = '已连接。HTTP API 与后台任务服务已就绪。';
    document.getElementById('summary').textContent = JSON.stringify({api_version: data.api_schema_version, project_id: data.project_id}, null, 2);
  }
  connect().catch(error => {status.textContent = error.message;});
  addEventListener('pagehide', () => {token = null;});
})();
