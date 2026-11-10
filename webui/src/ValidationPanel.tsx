import { Validation } from './api';

const labels: Record<string, string> = {
  passed: '后端验收通过', failed: '后端验收失败', stale: '验收结果已过期', error: '后端验收出错',
  pending: '等待后端验收', running: '后端验收中', not_run: '尚未完成后端验收',
  not_configured: '未配置后端验收', cancelled: '验收已取消',
};

export default function ValidationPanel({ validation }: { validation?: Validation }) {
  if (!validation?.status) return null;
  return <section className="handoff validation-panel" aria-label="后端验收结果">
    <strong>{labels[validation.status] || validation.status}</strong>
    {!!validation.attempt && <small> · 第 {validation.attempt} 次执行</small>}
    {validation.artifact_hash && <p className="muted">验证时的产物版本：{validation.artifact_hash.slice(0, 12)}</p>}
    {validation.reason && <p>{validation.reason}</p>}
    {(validation.checks || []).map((check, index) => <details className="command-check" key={`${check.id}:${index}`}>
      <summary>{check.id} · {check.timed_out ? '超时' : `退出码 ${check.exit_code ?? '未知'}`}</summary>
      <pre>{check.output || '无命令输出。'}</pre>
    </details>)}
    {validation.status === 'not_configured' && <p className="muted">角色汇报与工具记录可供核对，当前没有后端自动验收结论。</p>}
  </section>;
}
