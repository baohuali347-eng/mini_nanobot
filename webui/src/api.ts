/** 页面只与本地后端通信。模型密钥不进入前端配置。 */
export type Message = { role: 'user' | 'assistant'; content: string };
export type Handoff = {
  task_id: string; role: string; status: string; depends_on: string[]; acceptance: string[];
  reported_outcome: string; summary: string; files_changed: string[]; unresolved: string[];
  checks: { command: string; working_dir: string; exit_code: number | null; timed_out: boolean; output: string }[];
  evidence_truncated: boolean;
};
export type Activity = {
  type: string; event_id?: number; run_id?: string; task_id?: string; call_id?: string; role?: string;
  task?: string; state?: string; name?: string; result?: string; message?: string;
  depends_on?: string[]; acceptance?: string[]; handoff?: Handoff | null;
};
export type Session = {
  id: string; title: string; workspace: string; status: string;
  messages: Message[]; events: Activity[]; summary: string;
};
export type Status = { model: string; configured: boolean; workspace: string; max_subagents: number };
export type RunEvent = Activity & { text?: string; iteration?: number; messages?: Message[]; session?: Session };

export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败，请检查服务。');
  return result as T;
}

/** HTTP 分块可能截断中文或 JSON；解码器和行缓冲必须跨块保留。 */
export async function readEvents(stream: ReadableStream<Uint8Array>, receive: (event: RunEvent) => void) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let pending = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      const lines = pending.split('\n');
      pending = lines.pop() || '';
      for (const line of lines) if (line.trim()) receive(JSON.parse(line));
      if (done) break;
    }
    if (pending.trim()) receive(JSON.parse(pending));
  } finally { reader.releaseLock(); }
}

export async function chat(id: string, message: string, receive: (event: RunEvent) => void) {
  const response = await fetch(`/api/sessions/${id}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }),
  });
  if (!response.ok) throw new Error((await response.json()).error || '任务启动失败。');
  if (!response.body) throw new Error('浏览器不支持流式读取。');
  await readEvents(response.body, receive);
}
