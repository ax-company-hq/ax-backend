import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

// 임시 프로젝트에 훅과 team.json 복사 (스크립트 위치 기준으로 읽기 때문)
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ax-bash-'));
fs.mkdirSync(path.join(root, '.claude', 'hooks'), { recursive: true });
fs.mkdirSync(path.join(root, '.github'), { recursive: true });
fs.copyFileSync(path.join(REPO, '.claude/hooks/guard-bash.mjs'), path.join(root, '.claude/hooks/guard-bash.mjs'));
fs.copyFileSync(path.join(REPO, '.github/team.json'), path.join(root, '.github/team.json'));

function run(command, depts = 'data,memory', stdin) {
  const env = { ...process.env, CLAUDE_PROJECT_DIR: root };
  delete env.AX_DEPTS;
  if (depts) env.AX_DEPTS = depts;
  return spawnSync(process.execPath, [path.join(root, '.claude/hooks/guard-bash.mjs')], {
    input: stdin ?? JSON.stringify({ tool_name: 'Bash', tool_input: { command } }),
    env,
    encoding: 'utf8',
  });
}
const ok = (c, d) => assert.strictEqual(run(c, d).status, 0, c);
const no = (c, d) => assert.strictEqual(run(c, d).status, 2, c);

test('allowed pushes', () => {
  ok('git push -u origin data/impact-analysis');
  ok('git push origin memory/foo');
  ok('git push --set-upstream origin docs/fix-readme');
  ok('git push origin ci/stage-1');
  ok('git push -u origin infra-dejafix/x', 'dejafix');
  ok('git push origin dejafix/x', 'infra-dejafix');
});

test('blocked pushes', () => {
  no('git push --force origin data/x');
  no('git push -f origin data/x');
  no('git push --force-with-lease origin data/x');
  no('git push --delete origin data/x');
  no('git push origin --delete data/x');
  no('git push origin :data/x');
  no('git push origin +data/x');
  no('git push origin data/x:dev');
  no('git push --all origin');
  no('git push --tags origin');
  no('git push origin dev');
  no('git push origin main');
  no('git push origin HEAD:dev');
  no('git push origin HEAD:main');
  no('git push origin finance/x');
  no('git push upstream data/x');
  no('git push');
  no('git push origin');
  no('git -C other push origin data/x');
});

test('chained / nested pushes blocked', () => {
  no('echo hi && git push origin main');
  no('echo hi; git push origin main');
  no('ls | git push origin main');
  no('git status\ngit push origin main');
  no('bash -c "git push origin main"');
  no('echo $(git push origin main)');
});

test('no AX_DEPTS blocks all pushes', () => {
  no('git push -u origin data/x', '');
});

test('non-push commands allowed', () => {
  ok('git status');
  ok('git commit -m "update docs"');
  // 의도된 오탐: git 토큰 + push 토큰이 있으면 정확한 push 한 줄이 아닌 한 차단
  no('git commit -m "mention git push"');
  ok('ls -la && echo done');
  ok('git status', '');
});

test('security-review bypasses blocked', () => {
  no('git.exe push origin dev');
  no('GIT push origin dev');
  no('/usr/bin/git push origin dev');
  no('"C:\\Program Files\\Git\\bin\\git.exe" push origin dev');
  no('& git push origin dev');
  no('if true; then git push origin dev; fi');
  no('{ git push origin dev; }');
  no('exec git push origin dev');
  no('nice git push origin dev');
  no('timeout 5 git push origin dev');
  no('! git push origin dev');
  no('git -c alias.p=push p origin dev');
  no('git config alias.p push');
  no('git send-pack origin dev');
  no('git push origin data/x; echo hi');
  no('FOO=1 git push origin data/x');
  no('if ($true) { git push origin dev }');
  no('gh api -X PUT repos/o/r/pulls/3/merge');
  no('gh api --method PUT repos/o/r/pulls/3/merge');
  no('GH PR MERGE 3');
  no('gh.exe pr merge 3');
  ok('gh api repos/o/r/pulls/3');
});

test('gh pr merge blocked', () => {
  no('gh pr merge 3');
  no('echo x && gh pr merge 3 --squash');
  ok('gh pr view 3');
});

test('revert branch push allowed', () => {
  ok('git push origin revert/123');
  ok('git push -u origin revert/123');
  no('git push origin revert/123 --force');
});

test('gh pr merge --auto allowed', () => {
  ok('gh pr merge 3 --auto --squash');
  ok('gh pr merge 12 --auto --merge -R ax-company-hq/ax-frontend');
  ok('gh pr merge 7 --rebase --auto');
});

test('gh pr merge without --auto or with dangerous flags blocked', () => {
  no('gh pr merge 3 --squash');
  no('gh pr merge 3 --auto --squash --admin');
  no('gh pr merge 3 --auto --squash --delete-branch');
  no('gh pr merge 3 --auto --squash -d');
  no('gh pr merge 3 --auto --squash; echo x');
  no('gh pr merge 3 --auto --squash -R someone/else');
  no('gh pr merge 3 --auto');
});

test('malformed stdin blocked', () => {
  assert.strictEqual(run('', 'data', 'garbage').status, 2);
});
