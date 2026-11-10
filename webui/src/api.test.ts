import { describe, expect, it } from 'vitest';
import { readEvents } from './api';

describe('中文流式事件协议', () => {
  it('网络分块落在中文字符和 JSON 中间时仍正确还原', async () => {
    const bytes = new TextEncoder().encode('{"type":"text","text":"中文回复"}\n{"type":"done"}\n');
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (let index = 0; index < bytes.length; index += 2) controller.enqueue(bytes.slice(index, index + 2));
      controller.close();
    } });
    const result: unknown[] = [];
    await readEvents(stream, event => result.push(event));
    expect(result).toEqual([{ type: 'text', text: '中文回复' }, { type: 'done' }]);
  });
  it('最后一条事件没有换行时也能读取', async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new TextEncoder().encode('{"type":"done"}')); controller.close();
    } });
    const result: unknown[] = [];
    await readEvents(stream, event => result.push(event));
    expect(result).toEqual([{ type: 'done' }]);
  });
});
