import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

// 임시 프로젝트: 훅 + team.json 을 같은 상대 위치로 복사
function makeProject() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ax-guard-'));
  fs.mkdirSync(path.join(tmp, '.claude', 'hooks'), { recursive: true });
  fs.mkdirSync(path.join(tmp, '.github'), { recursive: true });
  fs.copyFileSync(path.join(REPO, '.claude/hooks/guard-folder.mjs'), path.join(tmp, '.claude/hooks/guard-folder.mjs'));
  fs.copyFileSync(path.join(REPO, '.github/team.json'), path.join(tmp, '.github/team.json'));
  for (const d of ['agents/data', 'agents/infra-dejafix', 'agents/finance', 'memory', 'docs/notes/data', 'docs/notes/finance']) {
    fs.mkdirSync(path.join(tmp, d), { recursive: true });
  }
  return tmp;
}

function run(root, file, { args = [], env = {}, tool = 'Edit', stdin } = {}) {
  const key = tool === 'NotebookEdit' ? 'notebook_path' : 'file_path';
  const input = stdin ?? JSON.stringify({ tool_name: tool, tool_input: { [key]: file }, cwd: root });
  const cleanEnv = { ...process.env, CLAUDE_PROJECT_DIR: root };
  delete cleanEnv.AX_DEPTS;
  return spawnSync(process.execPath, [path.join(root, '.claude/hooks/guard-folder.mjs'), ...args], {
    input,
    env: { ...cleanEnv, ...env },
    encoding: 'utf8',
  });
}

const root = makeProject();
const p = (...s) => path.join(root, ...s);

test('own dept folder allowed (existing and new files, relative path)', () => {
  assert.strictEqual(run(root, p('agents/data/a/b/new.py'), { args: ['--depts', 'data'] }).status, 0);
  assert.strictEqual(run(root, 'agents/data/x.py', { args: ['--depts', 'data'] }).status, 0);
});

test('multiple depts', () => {
  assert.strictEqual(run(root, p('memory/x.py'), { args: ['--depts', 'data,memory'] }).status, 0);
});

test('other dept folder blocked with owner hint', () => {
  const r = run(root, p('agents/finance/x.py'), { args: ['--depts', 'data'] });
  assert.strictEqual(r.status, 2);
  assert.match(r.stderr, /finance/);
  assert.match(r.stderr, /to:finance/);
});

test('human paths blocked', () => {
  for (const f of ['.github/workflows/x.yml', 'CLAUDE.md', 'pyproject.toml']) {
    assert.strictEqual(run(root, p(f), { args: ['--depts', 'data'] }).status, 2, f);
  }
});

test('docs/notes: own allowed, other dept blocked', () => {
  assert.strictEqual(run(root, p('docs/notes/data/n.md'), { args: ['--depts', 'data'] }).status, 0);
  assert.strictEqual(run(root, p('docs/notes/finance/n.md'), { args: ['--depts', 'data'] }).status, 2);
  assert.strictEqual(run(root, p('docs/other.md'), { args: ['--depts', 'data'] }).status, 2);
});

test('--out dir allowed', () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'ax-out-'));
  const a = ['--depts', 'data', '--out', out];
  assert.strictEqual(run(root, path.join(out, 'result.json'), { args: a }).status, 0);
  assert.strictEqual(run(root, path.join(out, '..', 'evil.json'), { args: a }).status, 2);
});

test('no args and no AX_DEPTS -> blocked', () => {
  const r = run(root, p('agents/data/x.py'));
  assert.strictEqual(r.status, 2);
  assert.match(r.stderr, /AX_DEPTS/);
});

test('AX_DEPTS env works', () => {
  assert.strictEqual(run(root, p('agents/data/x.py'), { env: { AX_DEPTS: 'data' } }).status, 0);
  assert.strictEqual(run(root, p('agents/finance/x.py'), { env: { AX_DEPTS: 'data' } }).status, 2);
});

test('alias infra-dejafix', () => {
  assert.strictEqual(run(root, p('agents/infra-dejafix/x.py'), { args: ['--depts', 'infra-dejafix'] }).status, 0);
});

test('unknown dept blocked', () => {
  assert.strictEqual(run(root, p('agents/data/x.py'), { args: ['--depts', 'nope'] }).status, 2);
});

test('forbidden names inside dept folder', () => {
  const a = { args: ['--depts', 'data'] };
  assert.strictEqual(run(root, p('agents/data/CLAUDE.md'), a).status, 2);
  assert.strictEqual(run(root, p('agents/data/sub/CLAUDE.local.md'), a).status, 2);
  assert.strictEqual(run(root, p('agents/data/.claude/x'), a).status, 2);
  assert.strictEqual(run(root, p('agents/data/.mcp.json'), a).status, 2);
  assert.strictEqual(run(root, p('docs/notes/data/.claude/x'), a).status, 2);
});

test('.. escape blocked', () => {
  assert.strictEqual(run(root, p('agents/data/../finance/x.py'), { args: ['--depts', 'data'] }).status, 2);
});

test('NotebookEdit uses notebook_path', () => {
  assert.strictEqual(run(root, p('agents/data/n.ipynb'), { args: ['--depts', 'data'], tool: 'NotebookEdit' }).status, 0);
  assert.strictEqual(run(root, p('agents/finance/n.ipynb'), { args: ['--depts', 'data'], tool: 'NotebookEdit' }).status, 2);
});

test('symlink/junction escape blocked', (t) => {
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'ax-outside-'));
  const link = p('agents/data/link');
  try {
    fs.symlinkSync(outside, link, 'junction');
  } catch (e) {
    return t.skip(`심볼릭 링크 생성 불가: ${e.code}`);
  }
  const a = { args: ['--depts', 'data'] };
  assert.strictEqual(run(root, path.join(link, 'new.txt'), a).status, 2);
  assert.strictEqual(run(root, path.join(link, 'deep', 'new.txt'), a).status, 2);
});

test('malformed stdin blocked', () => {
  const a = { args: ['--depts', 'data'] };
  assert.strictEqual(run(root, '', { ...a, stdin: 'not json' }).status, 2);
  assert.strictEqual(run(root, '', { ...a, stdin: '' }).status, 2);
  assert.strictEqual(run(root, '', { ...a, stdin: '{"tool_input":{}}' }).status, 2);
  assert.strictEqual(run(root, '', { ...a, stdin: 'null' }).status, 2);
});
