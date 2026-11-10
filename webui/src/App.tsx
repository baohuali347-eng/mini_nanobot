import { FormEvent, useEffect, useRef, useState } from 'react';
import { Activity, chat, Message, request, RunEvent, Session, Status } from './api';
import ValidationPanel from './ValidationPanel';
import IntegrationPanel from './IntegrationPanel';

const roles: Record<string, string> = { explorer: '探索', coder: '实现', reviewer: '审查' };
const operations: Record<string, string> = { add: '新增', modify: '修改', delete: '删除' };
const states: Record<string, string> = { queued: '排队中', waiting: '等待前置', blocked: '前置阻断', needs_followup: '待处理', running: '执行中', validating: '后端验收中', done: '已完成', error: '遇到问题', cancelled: '已停止', idle: '已就绪', interrupted: '上次运行中断' };
const names: Record<string, string> = { read_file: '读取文件', list_dir: '查看目录', search: '搜索代码', write_file: '写入文件', edit_file: '修改文件', exec: '执行命令', subagent: '管理子任务', read_memory: '读取记忆', remember: '保存记忆' };

function MessageContent({ content }: { content: string }) {
  // 仅处理代码围栏，其他内容作为普通文本渲染，避免执行模型返回的 HTML。
  return <>{content.split(/(```[\s\S]*?```)/g).map((part, index) => part.startsWith('```')
    ? <pre key={index}><code>{part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')}</code></pre>
    : <div className="message-text" key={index}>{part}</div>)}</>;
}

function TaskCard({ item }: { item: Activity }) {
  const handoff = item.handoff;
  return <details className="task-card" open={item.state === 'running' || item.state === 'blocked' || item.state === 'needs_followup' || item.state === 'error'}>
    <summary><strong>{roles[item.role || ''] || item.role} agent</strong><span className={'state ' + item.state}>{states[item.state || ''] || item.state}</span></summary>
    <small className="task-id">任务：{item.task_id}</small><p>{item.task}</p>
    <p className="muted">前置任务：{item.depends_on?.join('、') || '无，可独立执行'}</p>
    {!!item.acceptance?.length && <div><b>验收要求</b><ul>{item.acceptance.map((value, index) => <li key={index}>{value}</li>)}</ul></div>}
    {item.result && <><b>角色汇报</b><pre>{item.result}</pre></>}
    {handoff && <div className="handoff">
      <p className="muted">角色声明：{{ completed: '已完成（角色自报）', needs_followup: '需要后续处理', unstructured: '普通文本汇报' }[handoff.reported_outcome] || '尚未提交'}。请结合执行证据判断。</p>
      <b>文件工具修改记录</b><p>{handoff.files_changed.join('、') || '无记录'}</p>
      <b>实际命令记录</b>{handoff.checks.length ? handoff.checks.map((check, index) => <details className="command-check" key={index}>
        <summary>{check.timed_out ? '超时' : `退出码 ${check.exit_code}`} · {check.command}</summary>
        <small>目录：{check.working_dir}</small><pre>{check.output || '命令无输出。'}</pre>
      </details>) : <p className="muted">该角色未执行命令。</p>}
      {!!handoff.unresolved.length && <><b>未解决问题</b><ul>{handoff.unresolved.map((value, index) => <li key={index}>{value}</li>)}</ul></>}
      {handoff.evidence_truncated && <p className="notice">交接证据已限长，请查阅完整工具事件。</p>}
      <ValidationPanel validation={handoff.validation} />
      {handoff.artifact && <details><summary>补丁记录 · {handoff.artifact.files.length} 个文件</summary>
        <p className="muted">补丁采集与发布分别记录，请结合整体集成结果判断。</p>
        <p>{handoff.artifact.files.map(file => `${operations[file.operation] || file.operation}：${file.path}`).join('、')}</p>
        <pre>{handoff.artifact.diff}</pre>
        {handoff.artifact.truncated && <p>交接已限长，完整补丁保存在后端记录中。</p>}
      </details>}
    </div>}
  </details>;
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<Session | null>(null);
  const [workspace, setWorkspace] = useState('');
  const [input, setInput] = useState('');
  const [stream, setStream] = useState('');
  const [activities, setActivities] = useState<Activity[]>([]);
  const [memory, setMemory] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [iteration, setIteration] = useState(0);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [access, setAccess] = useState('');
  const streaming = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  async function listSessions() { setSessions(await request<Session[]>('/sessions')); }
  async function select(id: string) {
    const item = await request<Session>(`/sessions/${id}`);
    setSelected(item); setWorkspace(item.workspace); setActivities(item.events); setStream('');
    setBusy(item.status === 'running');
    setMemory((await request<{ content: string }>(`/sessions/${id}/memory`)).content);
    localStorage.setItem('mini_nanobot.session', id);
  }
  async function initialize() {
    try {
      const hash = new URLSearchParams(location.hash.slice(1));
      if (hash.get('token')) {
        await request('/login', 'POST', { token: hash.get('token') });
        history.replaceState(null, '', location.pathname);
      }
      const info = await request<Status>('/status');
      setStatus(info); setWorkspace(info.workspace); setNeedsLogin(false);
      const rows = await request<Session[]>('/sessions');
      setSessions(rows);
      const previous = localStorage.getItem('mini_nanobot.session');
      const chosen = rows.find(row => row.id === previous) || rows[0];
      if (chosen) await select(chosen.id);
    } catch (error) {
      setNotice(String((error as Error).message)); setNeedsLogin(true);
    }
  }
  useEffect(() => { void initialize(); }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [selected?.messages, stream]);
  // 页面刷新或断线后查询检查点；正在读取事件流时避免重复刷新消息。
  useEffect(() => {
    if (!busy || !selected) return;
    const timer = setInterval(() => {
      if (!streaming.current) void select(selected.id).catch(error => setNotice(error.message));
    }, 2000);
    return () => clearInterval(timer);
  }, [busy, selected?.id]);

  async function createSession() {
    const item = await request<Session>('/sessions', 'POST', { workspace });
    await select(item.id); await listSessions(); setNotice('');
    return item;
  }
  function receive(event: RunEvent) {
    if (event.type === 'text') setStream(value => value + (event.text || ''));
    if (event.type === 'round') setIteration(event.iteration || 1);
    if (event.type === 'history') {
      setSelected(value => value ? { ...value, messages: event.messages || [], status: 'running' } : value);
      setStream('');
    }
    if (['tool', 'task', 'error'].includes(event.type)) setActivities(value => [...value, event].slice(-100));
    if (event.type === 'error') setNotice(event.message || '任务遇到问题。');
    if (event.type === 'integration') setSelected(value => value ? { ...value, integration: event.integration } : value);
    if (event.type === 'validation' && event.task_id === 'main') {
      setSelected(value => value ? { ...value, validation: event.validation } : value);
    }
    if (event.type === 'repair') {
      setSelected(value => value ? { ...value, validation: { status: 'pending', attempt: event.attempt, checks: [] }, integration: { status: 'not_started', files: [] } } : value);
      setNotice(`后端验收未通过，正在进行第 ${(event.attempt || 2) - 1} 次修复。`);
    }
    if (event.type === 'done' && event.session) {
      setSelected(event.session); setActivities(event.session.events); setStream(''); setBusy(false);
      void listSessions();
      if (event.session.validation?.status === 'passed') setNotice('后端验收通过。');
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!input.trim() || busy) return;
    const message = input.trim();
    setNotice(''); setBusy(true); setIteration(0);
    try {
      const item = selected || await createSession();
      setSelected(value => value ? { ...value, validation: { status: 'pending', checks: [] }, integration: { status: 'not_started', files: [] } } : value);
      setBusy(true); setInput(''); streaming.current = true;
      await chat(item.id, message, receive);
      const snapshot = await request<Session>(`/sessions/${item.id}`);
      setSelected(snapshot); setBusy(snapshot.status === 'running');
    } catch (error) {
      setNotice((error as Error).message); setBusy(false);
    } finally { streaming.current = false; }
  }
  async function stop() {
    if (!selected) return;
    try { await request(`/sessions/${selected.id}/stop`, 'POST'); }
    catch (error) { setNotice((error as Error).message); }
  }
  async function saveMemory() {
    if (!selected) return;
    try {
      await request(`/sessions/${selected.id}/memory`, 'PUT', { content: memory });
      setNotice('项目记忆已保存，下次任务会加载。');
    } catch (error) { setNotice((error as Error).message); }
  }

  if (needsLogin) return <div className="login"><form onSubmit={async event => {
    event.preventDefault();
    try { await request('/login', 'POST', { token: access }); await initialize(); }
    catch (error) { setNotice((error as Error).message); }
  }}><div className="brand-icon">m</div><h1>欢迎使用 mini_nanobot</h1><p>使用启动终端给出的登录链接，或填写本机访问码。</p>
    <input aria-label="访问码" type="password" value={access} onChange={event => setAccess(event.target.value)} placeholder="本机访问码" />
    <button className="primary">进入工作台</button><p className="notice">{notice}</p></form></div>;

  const tasks = new Map<string, Activity>();
  for (const item of activities) if (item.type === 'task' && item.task_id) tasks.set(`${item.run_id || ''}:${item.workflow_attempt || 1}:${item.task_id}`, item);
  const toolsById = new Map<string, Activity>();
  for (const item of activities) if (item.type === 'tool') {
    toolsById.set(`${item.run_id || ''}:${item.workflow_attempt || 1}:${item.task_id || 'main'}:${item.call_id || item.name}`, item);
  }
  const tools = [...toolsById.values()];
  return <div className="app">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon">m</span><div><strong>mini_nanobot</strong><small>中文 AI 编程助手</small></div></div>
      <button className="new-session" disabled={busy} onClick={() => { setSelected(null); setActivities([]); setMemory(''); setNotice(''); }}>＋ 新建会话</button>
      <details className="mobile-navigation"><summary>代码目录与最近会话</summary>
        <textarea aria-label="手机代码目录" disabled={busy || !!selected} value={workspace} onChange={event => setWorkspace(event.target.value)} />
        <select aria-label="切换最近会话" disabled={busy} value={selected?.id || ''} onChange={event => void select(event.target.value).catch(error => setNotice(error.message))}>
          <option value="" disabled>选择一个已有会话</option>{sessions.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select>
      </details>
      <div className="section-title">工作目录</div>
      <textarea aria-label="代码项目目录" className="workspace" rows={3} disabled={busy || !!selected} value={workspace} onChange={event => setWorkspace(event.target.value)} />
      <p className="muted">新会话使用上面的目录。工具会在该目录中读取、修改和验证代码。</p>
      <div className="section-title history-title">最近会话 <span>{sessions.length}</span></div>
      <nav>{sessions.map(item => <button key={item.id} disabled={busy} className={'session ' + (selected?.id === item.id ? 'selected' : '')} onClick={() => void select(item.id).catch(error => setNotice(error.message))}>
        <span>{item.title}</span><small>{states[item.status] || item.status}</small>
      </button>)}</nav>
      <div className="sidebar-bottom"><span className={'dot ' + (!status?.configured ? 'warning' : '')} />{status?.configured ? '模型已配置' : '请填写 .env 后重启'}<small>{status?.model || '正在连接后端…'}</small></div>
    </aside>

    <main>
      <header><div><span className="eyebrow">编程工作台</span><h1>{selected?.title || '让代码任务有条理地完成'}</h1></div><span className="local-badge">仅本机访问</span></header>
      <div className="conversation">
        {!selected?.messages.length && <section className="welcome"><span className="welcome-label">从一个具体问题开始</span><h2>读懂代码，再把问题解决。</h2><p>描述目标、文件范围和验收方式，助手会按需调用工具或委派子任务。</p>
          <div className="role-intro">{Object.entries(roles).map(([key, label], index) => <div key={key}><b>0{index + 1} · {label}</b><span>{['定位文件与问题依据', '实现修改并执行验证', '独立检查实现质量'][index]}</span></div>)}</div>
          <button className="example" onClick={() => setInput('请用探索 agent 阅读项目结构，说明主要模块和入口，不修改文件。')}>试一试：让探索 agent 带你认识项目 ↗</button>
        </section>}
        {selected?.messages.map((item: Message, index) => <article className={'message ' + item.role} key={index}><div className="message-author">{item.role === 'user' ? '你' : 'mini_nanobot'}</div><MessageContent content={item.content} /></article>)}
        {stream && <article className="message assistant"><div className="message-author">mini_nanobot · 正在回复</div><MessageContent content={stream} /></article>}
        <div ref={bottom} />
      </div>
      <div className="composer-area">
        {notice && <div className="notice" role="status">{notice}</div>}
        {busy && <div className="running"><span className="dot pulse" />正在执行{iteration ? ` · 第 ${iteration} 轮` : ''}<button onClick={() => void stop()}>停止任务</button></div>}
        <form onSubmit={send} className="composer"><textarea aria-label="编程任务" value={input} onChange={event => setInput(event.target.value)} placeholder="描述你想解决的代码问题…" disabled={busy} rows={3} onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
        }} /><div className="composer-footer"><span>Enter 发送 · Shift + Enter 换行</span><button className="primary" disabled={busy || !input.trim() || !status?.configured}>发送任务 ↑</button></div></form>
      </div>
    </main>

    <aside className="inspector"><div className="section-title">任务与记忆</div><h2>执行过程</h2><p className="muted">查看工具活动和子任务结果，确认工作如何完成。</p>
      <ValidationPanel validation={selected?.validation} />
      <IntegrationPanel integration={selected?.integration} />
      <div className="task-list">{tasks.size === 0 && <p className="empty">子 agent 启动后会显示依赖、验收要求和交接证据。</p>}{[...tasks.entries()].map(([key, item]) => <TaskCard key={key} item={item} />)}</div>
      <details className="tool-list" open><summary>工具活动 <span>{tools.length}</span></summary>{tools.slice(-20).map((item, index) => <details key={item.event_id || index}><summary><span>{names[item.name || ''] || item.name}</span><small>{states[item.state || '']}</small></summary>{item.result && <pre>{item.result}</pre>}</details>)}{!tools.length && <p className="empty">暂无工具调用。</p>}</details>
      <details className="memory-panel"><summary>项目长期记忆</summary><p className="muted">记录项目规范和你的偏好，在该目录的新会话中继续使用。</p><textarea aria-label="项目长期记忆" rows={7} value={memory} disabled={!selected || busy} onChange={event => setMemory(event.target.value)} placeholder="例如：使用 pytest；修改后运行单元测试。" /><button disabled={!selected || busy} onClick={() => void saveMemory()}>保存记忆</button></details>
    </aside>
  </div>;
}
