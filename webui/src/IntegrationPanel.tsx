import { Integration } from './api';
import ValidationPanel from './ValidationPanel';

const labels: Record<string, string> = {
  integrated: '补丁已发布', conflict: '集成冲突，该批补丁未发布', validation_failed: '组合验收未通过，补丁未发布',
  blocked: '子任务未完成，补丁未发布', error: '集成出错，请检查发布记录',
  rolled_back: '发布失败，本批写入已回滚', recovery_required: '发布中断，保留外部修改，请人工核对',
};

export default function IntegrationPanel({ integration }: { integration?: Integration }) {
  if (!integration || !labels[integration.status]) return null;
  return <section className="handoff" aria-label="补丁集成结果">
    <strong>{labels[integration.status]}</strong>
    {integration.reason && <p>{integration.reason}</p>}
    {!!integration.files.length && <p>涉及文件：{integration.files.slice(0, 20).join('、')}{integration.files.length > 20 ? '…' : ''}</p>}
    {integration.artifact_hash && <p className="muted">发布时的产物版本：{integration.artifact_hash.slice(0, 12)}</p>}
    <ValidationPanel validation={integration.validation} />
  </section>;
}
