#!/usr/bin/env node
// PreToolUse(Bash|PowerShell) 훅 - 로컬 대화형 전용. git push / gh 머지 제한.
// exit 0 = 허용, exit 2 = 차단(stderr 사유).
//
// 규칙(셸 종류와 무관한 보수적 토큰 규칙):
//  1) 명령 어디에든 `git` 토큰(경로/대소문자/.exe 무관)이 있고 push / send-pack / alias 관련 토큰도 있으면,
//     명령 전체가 정확히 허용된 push 한 줄일 때만 통과. 그 외는 차단.
//     (오탐: `git commit -m "mention git push"` 도 차단된다. 의도된 보수적 동작.)
//  2) `gh pr merge`, `gh api` 의 /merge 호출·PUT pulls, GraphQL 머지 변이는 차단.
//
// 주의: Bash/PowerShell 로 하는 파일 쓰기(sed -i, Set-Content 등)는 로컬에서 폴더 가드되지 않는다.
//       서버 측 folder-guard 가 최종 방어선이다.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function block(msg) {
  process.stderr.write(`[guard-bash] ${msg}\n`);
  process.exit(2);
}

// 사용자 AX_DEPTS 로 허용 브랜치 접두어 집합 계산
function allowedPrefixes() {
  const list = (process.env.AX_DEPTS || '').split(',').map((s) => s.trim()).filter(Boolean);
  if (list.length === 0) return null;
  const here = path.dirname(fileURLToPath(import.meta.url));
  const team = JSON.parse(fs.readFileSync(path.join(here, '..', '..', '.github', 'team.json'), 'utf8'));
  const depts = team.depts || {};
  const set = new Set(['docs', 'ci']);
  for (const raw of list) {
    const id = depts[raw] ? raw : Object.keys(depts).find((k) => (depts[k].aliases || []).includes(raw));
    if (!id) continue;
    set.add(id);
    set.add(raw);
    (depts[id].aliases || []).forEach((a) => set.add(a));
    set.add(path.posix.basename(depts[id].folder));
  }
  return set;
}

// 실행 파일 토큰인지: 경로 접두어 제거, 소문자, 확장자(.exe 등) 제거
const exeName = (tok) => tok.split(/[\\/]/).pop().toLowerCase().replace(/\.(exe|cmd|bat|com)$/, '');

// 명령 전체가 정확히 허용된 push 한 줄인지 검사 (아니면 block)
function checkExactPush(cmd, prefixes) {
  if (!prefixes) return block('AX_DEPTS 설정이 없어 git push 를 막습니다. .claude/settings.local.example.json 참고');
  const why = '`git push [-u] origin <부서>/<브랜치>` 한 줄만 허용됩니다 (다른 명령과 섞거나 우회 형태는 차단).';
  const c = cmd.trim();
  if (/[;&|`$(){}<>"'\\!\n\r]/.test(c)) return block(why);
  const t = c.split(/\s+/);
  if (t[0] !== 'git' || t[1] !== 'push') return block(why);
  const okFlags = new Set(['-u', '--set-upstream', '-q', '--quiet']);
  const pos = [];
  for (const a of t.slice(2)) {
    if (a.startsWith('-')) {
      if (!okFlags.has(a)) return block(`허용되지 않는 push 옵션: ${a}`);
    } else pos.push(a);
  }
  if (pos.length !== 2) return block(why);
  const [remote, branch] = pos;
  if (remote !== 'origin') return block(`origin 이외의 remote 는 막습니다: ${remote}`);
  const m = /^([^/]+)\/[a-z0-9][a-z0-9-]*$/.exec(branch);
  if (!m) return block(`브랜치 형식이 올바르지 않거나 보호 대상입니다: ${branch}`);
  if (!prefixes.has(m[1])) return block(`${m[1]}/ 접두어는 내 담당(AX_DEPTS)이 아닙니다. 허용: ${[...prefixes].join(', ')}`);
}

function main() {
  let cmd;
  try {
    cmd = JSON.parse(fs.readFileSync(0, 'utf8')).tool_input.command;
  } catch {
    return block('입력을 해석할 수 없어 차단합니다.');
  }
  if (typeof cmd !== 'string') return block('명령을 알 수 없어 차단합니다.');

  let prefixes;
  try {
    prefixes = allowedPrefixes();
  } catch {
    prefixes = null;
  }

  // 셸 구분자·따옴표·괄호·! 등으로 느슨하게 토큰화 (경로의 \ 와 / 는 유지)
  const toks = cmd.split(/[\s;&|(){}`$"'<>,!]+/).filter(Boolean);
  const lower = toks.map((x) => x.toLowerCase());
  const has = (name) => toks.some((x) => exeName(x) === name);

  if (has('git')) {
    const risky = lower.some((x) => x === 'push' || x === 'send-pack' || x.includes('alias'));
    if (risky) checkExactPush(cmd, prefixes);
  }

  if (has('gh')) {
    if (lower.includes('pr') && lower.includes('merge')) return block('에이전트는 PR 을 머지할 수 없습니다 (gh pr merge 금지).');
    if (lower.includes('api')) {
      const put = lower.some((x) => x === 'put' || /^--method=put$/.test(x) || x === '-xput');
      if (lower.some((x) => x.includes('/merge')) || (put && lower.some((x) => x.includes('pulls')))) {
        return block('gh api 를 통한 PR 머지는 금지입니다.');
      }
    }
    if (/mergepullrequest|enablepullrequestautomerge/i.test(cmd)) return block('GraphQL 을 통한 PR 머지는 금지입니다.');
  }
  process.exit(0);
}

try {
  main();
} catch (e) {
  block(`예상치 못한 오류로 차단합니다: ${e && e.message}`);
}
