import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import IntegrationPanel from './IntegrationPanel';

describe('补丁集成结果', () => {
  it('候选验收通过不能把冲突显示为已发布', () => {
    const html = renderToStaticMarkup(<IntegrationPanel integration={{ status: 'conflict', files: ['a.py'], validation: { status: 'passed', checks: [] } }} />);
    expect(html).toContain('集成冲突，该批补丁未发布');
    expect(html).toContain('a.py');
    expect(html).not.toContain('补丁已发布');
  });
  it('发布成功展示当时的版本，失败原因安全转义', () => {
    const success = renderToStaticMarkup(<IntegrationPanel integration={{ status: 'integrated', files: ['a.py'], artifact_hash: 'abc123' }} />);
    expect(success).toContain('补丁已发布');
    expect(success).toContain('发布时的产物版本');
    const failed = renderToStaticMarkup(<IntegrationPanel integration={{ status: 'error', files: [], reason: '<script>bad</script>' }} />);
    expect(failed).not.toContain('<script>');
    expect(failed).toContain('&lt;script&gt;');
  });
});
