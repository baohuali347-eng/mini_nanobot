import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ValidationPanel from './ValidationPanel';

describe('后端验收展示', () => {
  it('过期结果保留原始退出码但不显示通过结论', () => {
    const html = renderToStaticMarkup(<ValidationPanel validation={{ status: 'stale', artifact_hash: 'abcdef1234567890', checks: [{ id: 'tests', exit_code: 0 }] }} />);
    expect(html).toContain('验收结果已过期');
    expect(html).toContain('退出码 0');
    expect(html).not.toContain('后端验收通过');
  });
  it('未配置检查时不冒充自动验收', () => {
    const html = renderToStaticMarkup(<ValidationPanel validation={{ status: 'not_configured', checks: [] }} />);
    expect(html).toContain('未配置后端验收');
    expect(html).toContain('没有后端自动验收结论');
  });
  it('显示实际超时与修复后的执行次数，命令输出作为文本', () => {
    const html = renderToStaticMarkup(<ValidationPanel validation={{ status: 'failed', attempt: 2, checks: [{ id: 'tests', timed_out: true, output: '<script>alert(1)</script>' }] }} />);
    expect(html).toContain('第 2 次执行');
    expect(html).toContain('超时');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
  });
});
