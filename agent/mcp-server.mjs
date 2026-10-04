#!/usr/bin/env node
// MCP (Model Context Protocol) stdio 桥 —— 标准实现，把所有项目的 Agent API 暴露为 MCP tools。
// 用法：node <project>/agent/mcp-server.mjs
// 逻辑：读 agent/.endpoint（或 AGENT_BASE_URL）；不通则按 agent/README 里登记的启动命令自动拉起本地服务。
//
// 鉴权：服务端现在要求「非 GET 必带本机共享令牌」（见 ra/localguard.py）。令牌由服务自己写进
// 本机数据目录（Windows: %LOCALAPPDATA%\RecordedAutomation\agent-token），这个桥每次调用现读它，
// 所以合法的本机 Agent 不需要任何手工步骤 —— 装好就能用，读不到令牌就直接把原因说清楚。
import { spawn } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(__dirname, '..');
const PROTOCOL = '2.2.0';
const SERVER_INFO = { name: path.basename(PROJECT_ROOT) + '-agent-api', version: '1.0.0' };
const TOKEN_HEADER = 'x-agent-token';

const log = (...a) => process.stderr.write(`[mcp] ${a.join(' ')}\n`);

function endpointFile() {
  const p = path.join(__dirname, '.endpoint');
  return existsSync(p) ? readFileSync(p, 'utf8').trim() : null;
}

/** 与 ra/localguard.py 的 default_token_dir() 同一套规则（两种语言各一份，改一处要改两处） */
function tokenFileCandidates() {
  const dataDir = process.platform === 'win32'
    ? process.env.LOCALAPPDATA
    : (process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'));
  const base = dataDir ? path.join(dataDir, 'RecordedAutomation', 'agent-token') : null;
  return [process.env.RA_AGENT_TOKEN_FILE, base].filter(Boolean);
}

/** 每次调用都现读：服务可能刚刚才起来、令牌文件可能刚刚才写出来 */
function readToken() {
  if (process.env.RA_AGENT_TOKEN) return process.env.RA_AGENT_TOKEN.trim();
  for (const p of tokenFileCandidates()) {
    try {
      if (existsSync(p)) {
        const text = readFileSync(p, 'utf8').trim();
        if (text) return text;
      }
    } catch { /* 读不到就试下一个候选 */ }
  }
  return null;
}

async function rpc(base, method, params) {
  const isCall = method !== 'tools/list';
  const headers = { 'content-type': 'application/json' };
  const token = isCall ? readToken() : null;
  if (isCall && token) headers[TOKEN_HEADER] = token;
  const res = await fetch(`${base}/api/agent/${method === 'tools/list' ? 'tools' : 'tool'}`, {
    method: method === 'tools/list' ? 'GET' : 'POST',
    headers,
    body: method === 'tools/list' ? undefined : JSON.stringify(params),
    signal: AbortSignal.timeout(120_000),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok === false) {
    const code = body?.error?.code || '';
    if (code === 'token_required') {
      throw new Error(`服务端要求本机令牌，但没读到令牌文件（候选：${tokenFileCandidates().join(' / ') || '无'}）。`
        + '令牌由录放台启动时写入；确认它在本机运行中，或用 RA_AGENT_TOKEN/RA_AGENT_TOKEN_FILE 指定。');
    }
    throw new Error(body?.error?.message || `HTTP ${res.status}`);
  }
  return body;
}

async function ensureBase() {
  const candidates = [process.env.AGENT_BASE_URL, endpointFile(), process.env.AGENT_DEFAULT_BASE].filter(Boolean);
  for (const base of candidates) {
    try {
      const r = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(1500) });
      if (r.ok) return base;
    } catch { /* 继续尝试 */ }
  }
  // 自动拉起：agent/launch.json 里按顺序登记可用的启动方式。
  // 界面程序自己就带这个服务（同一个进程），所以只要录放台开着，这里通常轮不到拉起。
  const launchFile = path.join(__dirname, 'launch.json');
  if (!existsSync(launchFile)) throw new Error(`Agent 服务未启动且缺少 ${launchFile}`);
  const spec = JSON.parse(readFileSync(launchFile, 'utf8'));
  const commands = spec.commands || [{ argv: [spec.command, ...(spec.args || [])] }];
  const port = spec.ready_port || 8790;
  const probe = async () => {
    for (let p = port; p < port + 12; p++) {
      try {
        const r = await fetch(`http://127.0.0.1:${p}/api/health`, { signal: AbortSignal.timeout(800) });
        if (r.ok) return `http://127.0.0.1:${p}`;
      } catch { /* 未就绪 */ }
    }
    return null;
  };
  const notes = [];
  for (const item of commands) {
    const [cmd, ...args] = item.argv || [];
    if (!cmd) continue;
    try {
      const child = spawn(cmd, args, { cwd: PROJECT_ROOT, stdio: 'ignore', detached: true, shell: false });
      child.on('error', (e) => notes.push(`${cmd}: ${e.message}`));
      child.unref();
    } catch (e) { notes.push(`${cmd}: ${e.message}`); continue; }
    for (let i = 0; i < 40; i++) {           // 每种拉起方式最多等 20 秒，再换下一种
      await new Promise((r) => setTimeout(r, 500));
      const base = await probe();
      if (base) return base;
    }
    notes.push(`${item.note || cmd} 未在 20 秒内就绪`);
  }
  throw new Error('自动拉起 Agent 服务失败：' + notes.join('；') + '。也可以直接打开录放台，它自己就提供这个接口。');
}

let BASE = null;

function msg(id, result) { return { jsonrpc: '2.0', id, result }; }
function err(id, code, message) { return { jsonrpc: '2.0', id, error: { code, message } }; }

async function handle(req) {
  const { id, method, params } = req;
  if (method === 'initialize') {
    return msg(id, { protocolVersion: PROTOCOL, capabilities: { tools: {} }, serverInfo: SERVER_INFO });
  }
  if (method === 'notifications/initialized' || method === 'initialized') return null;
  if (method === 'ping') return msg(id, {});
  if (method === 'tools/list') {
    BASE = BASE || await ensureBase();
    const body = await rpc(BASE, 'tools/list');
    return msg(id, { tools: body.data.map((t) => ({ name: t.name, description: t.description, inputSchema: t.input_schema, annotations: { readOnlyHint: t.risk === 'read', destructiveHint: false, openWorldHint: false } })) });
  }
  if (method === 'tools/call') {
    BASE = BASE || await ensureBase();
    try {
      const body = await rpc(BASE, 'tools/call', { tool: params.name, input: params.arguments || {} });
      return msg(id, { content: [{ type: 'text', text: JSON.stringify(body.data, null, 2) }], isError: false });
    } catch (e) {
      return msg(id, { content: [{ type: 'text', text: `调用失败：${e.message}` }], isError: true });
    }
  }
  return err(id, -32601, `不支持的方法：${method}`);
}

let buf = '';

function write(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let req;
    try { req = JSON.parse(line); } catch { write(err(null, -32700, 'parse error')); continue; }
    // 响应是异步的。切勿在此 process.exit()：stdout 接管道时写是异步缓冲的，
    // 立即退出会丢掉尚未 flush 的 tools/list、tools/call 响应。让事件循环自然排空。
    handle(req).then((out) => { if (out) write(out); })
      .catch((e) => write(err(req.id, -32603, e.message)));
  }
});
log(`bridge ready for ${PROJECT_ROOT}`);
