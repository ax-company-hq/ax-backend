#!/usr/bin/env node
// PreToolUse 훅: 쓰기 도구(Edit/Write/MultiEdit/NotebookEdit)를 담당 부서 폴더로 제한한다.
// 사용: guard-folder.mjs [--depts a,b] [--out DIR]   (없으면 env AX_DEPTS)
// exit 0 = 허용, exit 2 = 차단(사유는 stderr). 파싱 오류 등은 모두 차단(fail closed).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const WIN = process.platform === 'win32';
const norm = (p) => (WIN ? p.toLowerCase() : p);

function block(msg) {
  process.stderr.write(`[guard-folder] ${msg}\n`);
  process.exit(2);
}

// 인자 파싱 (--k v / --k=v)
function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const m = /^--(depts|out)(?:=(.*))?$/.exec(argv[i]);
    if (!m) continue;
    out[m[1]] = m[2] !== undefined ? m[2] : argv[++i];
  }
  return out;
}

// base 안에 target 이 있는지 (같거나 하위). 상대 세그먼트 배열 반환, 아니면 null
function relInside(base, target) {
  const rel = path.relative(norm(base), norm(target));
  if (rel === '') return [];
  if (rel.split(path.sep)[0] === '..') return null;
  if (path.isAbsolute(rel)) return null;
  // 원래 대소문자 세그먼트 유지
  const orig = path.relative(base, target);
  return orig.split(path.sep).filter(Boolean);
}

// 존재하는 가장 깊은 조상의 realpath + 나머지 (없는 파일도 처리)
function realOfDeepest(p) {
  let cur = p;
  const rest = [];
  while (!fs.existsSync(cur)) {
    const parent = path.dirname(cur);
    if (parent === cur) break;
    rest.unshift(path.basename(cur));
    cur = parent;
  }
  return { ancestor: cur, real: fs.realpathSync(cur), rest };
}

// 심볼릭 링크/정션 개입 여부. base(신뢰 기준 디렉터리) 아래에서만 검사한다.
function hasLink(base, target) {
  const rb = realOfDeepest(base);
  const realBase = path.join(rb.real, ...rb.rest);
  const t = realOfDeepest(target);
  const relAnc = path.relative(base, t.ancestor);
  const expected = relAnc === '' ? realBase : path.join(realBase, relAnc);
  if (norm(path.resolve(t.real)) !== norm(path.resolve(expected))) return true;
  try {
    if (fs.lstatSync(target).isSymbolicLink()) return true;
  } catch { /* 없는 파일 */ }
  return false;
}

function main() {
  let input;
  try {
    input = JSON.parse(fs.readFileSync(0, 'utf8'));
    if (!input || typeof input !== 'object') throw new Error('not object');
  } catch {
    return block('입력(JSON)을 해석할 수 없어 차단합니다.');
  }
  const ti = input.tool_input;
  const target = ti && (ti.file_path ?? ti.notebook_path);
  if (typeof target !== 'string' || !target) return block('대상 경로를 알 수 없어 차단합니다.');
  if (target.includes('\0')) return block('잘못된 경로입니다.');

  const root = path.resolve(process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd());

  // team.json 은 스크립트 위치 기준 (신뢰 사본)
  let team;
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    team = JSON.parse(fs.readFileSync(path.join(here, '..', '..', '.github', 'team.json'), 'utf8'));
  } catch {
    return block('team.json 을 읽을 수 없어 차단합니다.');
  }
  const depts = team.depts || {};
  const forbidden = (team.forbidden_in_dept || []).map((s) => s.toLowerCase());

  const args = parseArgs(process.argv.slice(2));
  const list = (args.depts ?? process.env.AX_DEPTS ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  if (list.length === 0) {
    return block('AX_DEPTS 설정이 없어 모든 쓰기를 막습니다. .claude/settings.local.example.json 참고');
  }

  // 별칭 -> 정식 id
  const resolve = (id) => {
    if (depts[id]) return id;
    return Object.keys(depts).find((k) => (depts[k].aliases || []).includes(id));
  };
  const mine = []; // {id, names, base, forbidCheck}
  for (const raw of list) {
    const id = resolve(raw);
    if (!id) return block(`알 수 없는 부서 id: ${raw}`);
    mine.push({ id, names: [id, raw] });
  }

  const abs = path.resolve(root, target);
  const outDir = args.out ? path.resolve(root, args.out) : null;

  // 허용 영역 계산: {base, segs(base 기준 상대), checkForbidden, linkBase}
  let hit = null;
  for (const m of mine) {
    const folder = path.resolve(root, depts[m.id].folder);
    const segs = relInside(folder, abs);
    if (segs) { hit = { segs, forbid: true, linkBase: root }; break; }
    for (const n of new Set(m.names)) {
      const notes = path.resolve(root, 'docs', 'notes', n);
      const s2 = relInside(notes, abs);
      if (s2) { hit = { segs: s2, forbid: true, linkBase: root }; break; }
    }
    if (hit) break;
  }
  if (!hit && outDir) {
    const segs = relInside(outDir, abs);
    if (segs) hit = { segs, forbid: false, linkBase: outDir };
  }

  if (!hit) {
    // 담당자 안내
    const rel = relInside(root, abs);
    let owner = '사람 담당 경로(사람 전용)';
    let ownerDept = null;
    if (rel) {
      const relPath = rel.join('/').toLowerCase();
      let best = -1;
      for (const [id, d] of Object.entries(depts)) {
        const f = d.folder.toLowerCase();
        if ((relPath === f || relPath.startsWith(f + '/')) && f.length > best) { best = f.length; ownerDept = id; }
        const m = /^docs\/notes\/([^/]+)/.exec(relPath);
        if (m && m[1] === id.toLowerCase() && best < 0) ownerDept = id;
      }
    }
    if (ownerDept) owner = `${ownerDept} 부서 (${(depts[ownerDept].owners || []).join(', ')})`;
    const hint = ownerDept ? `to:${ownerDept}` : 'to:<부서>';
    return block(`${target}는 ${owner} 담당. 직접 고치지 말고 ${hint} 요청으로 넘길 것`);
  }

  // 금지 이름(CLAUDE.md, .claude 등) - 윈도우 후행 점/공백, ADS(:) 우회 대비
  if (hit.forbid) {
    for (const seg of hit.segs) {
      const clean = seg.replace(/[. ]+$/, '').split(':')[0].toLowerCase();
      if (forbidden.includes(clean) || (WIN && /~\d/.test(clean))) {
        return block(`${target}: 부서 폴더 안이라도 ${seg} 는 수정할 수 없습니다 (사람 전용 파일).`);
      }
    }
  }

  // 부서 폴더 안의 사람 전용 파일 (team.json human_files: 기준표·AI 재량 한도)
  if (hit.forbid) {
    const rel = relInside(root, abs);
    if (rel) {
      const norm = (p) => p.split('/').map((s) => s.replace(/[. ]+$/, '').split(':')[0]).join('/').toLowerCase();
      const relPath = norm(rel.join('/'));
      if ((team.human_files || []).some((f) => norm(f) === relPath)) {
        return block(`${target}: 부서 폴더 안이지만 사람만 바꾸는 파일입니다 (team.yml human_files). 사람 PR로 code owner 승인을 받으세요.`);
      }
    }
  }

  // 심볼릭 링크/정션 차단
  try {
    if (hasLink(hit.linkBase, abs)) return block(`${target}: 심볼릭 링크/정션이 개입된 경로라 차단합니다.`);
  } catch {
    return block(`${target}: 경로 검사 중 오류가 나 차단합니다.`);
  }
  process.exit(0);
}

try {
  main();
} catch (e) {
  block(`예상치 못한 오류로 차단합니다: ${e && e.message}`);
}
