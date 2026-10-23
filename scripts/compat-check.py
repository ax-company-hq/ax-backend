"""base/head 내보내기 결과를 비교해 하위 호환을 깨는 변경만 실패시킨다.

실행: python scripts/compat-check.py <base_dir> <head_dir>   (종료 코드 1 = 깨짐)

분류 규칙 (계약 모델 클래스 이름 접미사, 예외는 REQUEST_ONLY_MODELS):
- 요청형: Request, Query, Input, Command, Params, Meta  -> validation 스키마 + 요청 규칙
- 응답형: Result, Response, Output, Event, Card, Record, Decision, Data
                                                      -> serialization 스키마 + 응답 규칙
- 그 외: 둘 다 (요청 규칙과 응답 규칙을 모두 적용, 가장 엄격)
- REQUEST_ONLY_MODELS: skill 입력으로만 쓰는 모델을 이름으로 하나씩 지정한다(요청형으로 취급).
  접미사로 넓히지 않고 목록으로 두어, 새 모델이 응답 규칙을 빠뜨리는 일이 없게 한다.

요청 규칙 (기존 클라이언트가 보내던 값을 계속 받아야 한다):
  필수 필드 추가 / 선택->필수 / 필드 삭제 / 타입 좁힘(허용 타입 감소, null 불가로 변경) /
  enum 값 제거 또는 새 enum 제약 추가 는 깨짐.
응답 규칙 (기존 소비자가 읽던 값을 계속 받아야 한다):
  필드 삭제 / 필수->선택 / 타입 변경(허용 타입 증가, null 허용으로 변경) /
  enum 값 추가 또는 enum 제약 제거 는 깨짐. 필드 추가는 호환.
공통: 모델/엔드포인트 삭제는 깨짐, 추가는 정상. integer->number 확장은 요청에서만 호환.
$ref는 이름이 아니라 가리키는 스키마의 구조로 비교한다 (이름만 바뀌면 통과).
OpenAPI: 요청 = parameters(삭제/새 필수/선택->필수) + requestBody, 응답 = responses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUEST_SUFFIXES = ("Request", "Query", "Input", "Command", "Params", "Meta")
# 요청형으로만 취급할 모델 (접미사 규칙에 안 걸리는 skill 입력 모델). 추가할 때 PR에 이유를 적는다.
# - RecordCase: hr `record_case`의 입력. 응답으로 쓰는 소비자가 없다(출력은 `RecordCaseOutput`).
REQUEST_ONLY_MODELS = frozenset({"RecordCase"})
RESPONSE_SUFFIXES = (
    "Result", "Response", "Output", "Event", "Card", "Record", "Decision", "Data",
)  # fmt: skip
REQ, RES = "request", "response"
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def classify(class_name: str) -> tuple[str, ...]:
    if class_name in REQUEST_ONLY_MODELS or class_name.endswith(REQUEST_SUFFIXES):
        return (REQ,)
    if class_name.endswith(RESPONSE_SUFFIXES):
        return (RES,)
    return (REQ, RES)


# ---------------------------------------------------------------- 스키마 비교


class Ctx:
    """루트 문서 두 개(base/head), 방향, 문제 목록, 순환 방지 스택."""

    def __init__(self, base_root: dict, head_root: dict, direction: str):
        self.roots = (base_root, head_root)
        self.direction = direction
        self.problems: list[str] = []
        self.stack: set[tuple[int, int]] = set()

    @property
    def req(self) -> bool:
        return self.direction == REQ

    def resolve(self, schema, side: int) -> dict:
        for _ in range(50):
            if not (isinstance(schema, dict) and "$ref" in schema):
                break
            ref = schema["$ref"]
            node = self.roots[side]
            if not ref.startswith("#/"):
                return {}
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            schema = node
        return schema if isinstance(schema, dict) else {}

    def attempt(self, fn, *args) -> bool:
        """fn을 돌려 문제가 없었는지만 돌려준다 (문제는 버린다)."""
        saved, self.problems = self.problems, []
        try:
            fn(*args)
            return not self.problems
        finally:
            self.problems = saved


def _variants(schema, ctx: Ctx, side: int, depth: int = 0) -> tuple[list[dict], bool]:
    """anyOf/oneOf를 (null 아닌 변형 목록, nullable)로 펼친다."""
    schema = ctx.resolve(schema, side)
    key = "anyOf" if "anyOf" in schema else "oneOf" if "oneOf" in schema else None
    if key is None:
        t = schema.get("type")
        if t == "null":
            return [], True
        if isinstance(t, list) and "null" in t:
            rest = [x for x in t if x != "null"]
            return [dict(schema, type=rest[0] if len(rest) == 1 else rest)], True
        return [schema], False
    out: list[dict] = []
    nullable = False
    for v in schema[key] if depth < 10 else []:
        sub, null = _variants(v, ctx, side, depth + 1)
        out.extend(sub)
        nullable = nullable or null
    return out, nullable


def _types(schema: dict):
    t = schema.get("type")
    if t is None:
        if "properties" in schema:
            return {"object"}
        if "items" in schema:
            return {"array"}
        return None  # 제약 없음(any)
    return set(t) if isinstance(t, list) else {t}


def _subset(a, b) -> bool:
    """타입 집합 a가 b에 포함되는가 (None = any, integer는 number에 포함)."""
    if b is None:
        return True
    if a is None:
        return False
    return all(x in b or (x == "integer" and "number" in b) for x in a)


def _enum(schema: dict):
    if "enum" in schema:
        return {json.dumps(v) for v in schema["enum"]}
    if "const" in schema:
        return {json.dumps(schema["const"])}
    return None


def _fmt(t) -> str:
    return "any" if t is None else "|".join(sorted(t))


def compare(b, h, ctx: Ctx, path: str) -> None:
    _compare(b, h, ctx, path)


def _compare(b, h, ctx: Ctx, path: str) -> None:
    bvars, bnull = _variants(b, ctx, 0)
    hvars, hnull = _variants(h, ctx, 1)
    if ctx.req and bnull and not hnull:
        ctx.problems.append(f"{path}: null 허용 -> 불가 (요청 타입 좁힘)")
    if not ctx.req and hnull and not bnull:
        ctx.problems.append(f"{path}: null 허용으로 변경 (응답 타입 변경)")
    if not bvars or not hvars:
        if bool(bvars) != bool(hvars):
            ctx.problems.append(f"{path}: 타입 변경 (null 전용 <-> 값)")
        return
    if len(bvars) == 1 and len(hvars) == 1:
        _compare_one(bvars[0], hvars[0], ctx, path)
        return
    # 요청: base의 각 변형을 head 어느 변형이 받아야 함 / 응답: head의 각 변형이 base에 있어야 함
    if ctx.req:
        for i, v in enumerate(bvars):
            if not any(ctx.attempt(_compare_one, v, w, ctx, path) for w in hvars):
                ctx.problems.append(f"{path}: anyOf/oneOf 변형 #{i} 이(가) 더 이상 호환되지 않음")
    else:
        for i, w in enumerate(hvars):
            if not any(ctx.attempt(_compare_one, v, w, ctx, path) for v in bvars):
                ctx.problems.append(f"{path}: anyOf/oneOf 새 변형 #{i} 은(는) 기존과 호환되지 않음")


def _compare_one(b, h, ctx: Ctx, path: str) -> None:
    # 단일 변형이 다시 $ref일 수 있어 한 번 더 해석 (순환은 compare가 막는다)
    b, h = ctx.resolve(b, 0), ctx.resolve(h, 1)
    key = (id(b), id(h))  # 순환 방지: 진행 중인 (base, head) 스키마 쌍은 다시 내려가지 않는다
    if key in ctx.stack:
        return
    ctx.stack.add(key)
    try:
        _compare_body(b, h, ctx, path)
    finally:
        ctx.stack.discard(key)


def _compare_body(b: dict, h: dict, ctx: Ctx, path: str) -> None:
    bt, ht = _types(b), _types(h)
    ok = _subset(bt, ht) if ctx.req else _subset(ht, bt)
    if not ok:
        ctx.problems.append(f"{path}: 타입 변경 {_fmt(bt)} -> {_fmt(ht)}")
        return
    be, he = _enum(b), _enum(h)
    if ctx.req:
        if he is not None and (be is None or be - he):
            gone = sorted(be - he) if be is not None else ["<제약 없던 값>"]
            ctx.problems.append(f"{path}: 요청 enum 값 제거/제한 {', '.join(gone)}")
    elif be is not None and (he is None or he - be):
        added = sorted(he - be) if he is not None else ["<제약 해제>"]
        ctx.problems.append(f"{path}: 응답 enum 값 추가/확장 {', '.join(added)}")
    if "properties" in b or "properties" in h or "object" in (bt or ()):
        _compare_object(b, h, ctx, path)
    if isinstance(b.get("items"), dict) and isinstance(h.get("items"), dict):
        compare(b["items"], h["items"], ctx, path + "[]")
    ba, ha = b.get("additionalProperties"), h.get("additionalProperties")
    if isinstance(ba, dict) and isinstance(ha, dict):
        compare(ba, ha, ctx, path + "{}")


def _compare_object(b: dict, h: dict, ctx: Ctx, path: str) -> None:
    bp, hp = b.get("properties", {}), h.get("properties", {})
    breq, hreq = set(b.get("required", [])), set(h.get("required", []))
    join = (lambda n: f"{path}.{n}") if path else (lambda n: n)
    for name in sorted(bp):
        p = join(name)
        if name not in hp:
            ctx.problems.append(f"{p}: 필드 삭제")
            continue
        if ctx.req and name in hreq and name not in breq:
            ctx.problems.append(f"{p}: 선택 -> 필수 (요청 필수 필드 추가)")
        if not ctx.req and name in breq and name not in hreq:
            ctx.problems.append(f"{p}: 필수 -> 선택 (응답)")
        compare(bp[name], hp[name], ctx, p)
    if ctx.req:
        for name in sorted(set(hp) - set(bp)):
            if name in hreq:
                ctx.problems.append(f"{join(name)}: 필수 필드 추가 (요청)")


def check_schema(base_root, head_root, direction, label, b=None, h=None) -> list[str]:
    """루트 문서 기준으로 b/h(기본: 루트 자신)를 비교하고 문제 문자열 목록을 돌려준다."""
    ctx = Ctx(base_root, head_root, direction)
    compare(base_root if b is None else b, head_root if h is None else h, ctx, "")
    return [f"{label} [{direction}] {p.removeprefix(': ').removeprefix('.')}" for p in ctx.problems]


# ---------------------------------------------------------------- OpenAPI


def _json_schema(content):
    if not content:
        return None
    for mt in ("application/json", *content):
        if isinstance(content.get(mt), dict):
            return content[mt].get("schema")
    return None


def _deref(obj, root):
    for _ in range(50):
        if not (isinstance(obj, dict) and "$ref" in obj):
            break
        node = root
        for part in obj["$ref"][2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        obj = node
    return obj if isinstance(obj, dict) else {}


def _params(op: dict, item: dict, root: dict) -> dict:
    out = {}
    for p in [*item.get("parameters", []), *op.get("parameters", [])]:
        p = _deref(p, root)
        out[(p.get("in"), p.get("name"))] = p
    return out


def check_openapi(base: dict, head: dict, label: str) -> list[str]:
    out: list[str] = []
    hpaths = head.get("paths", {})
    for path, bitem in sorted(base.get("paths", {}).items()):
        for method in METHODS:
            if method not in bitem:
                continue
            ep = f"{label} {method.upper()} {path}"
            if method not in hpaths.get(path, {}):
                out.append(f"{ep}: 엔드포인트 삭제")
                continue
            hitem = hpaths[path]
            bop, hop = bitem[method], hitem[method]
            bpar, hpar = _params(bop, bitem, base), _params(hop, hitem, head)
            for k, bp in sorted(bpar.items(), key=str):
                name = f"{ep} 파라미터 {k[1]}({k[0]})"
                if k not in hpar:
                    out.append(f"{name} [request]: 삭제")
                    continue
                if hpar[k].get("required") and not bp.get("required"):
                    out.append(f"{name} [request]: 선택 -> 필수")
                out += check_schema(
                    base, head, REQ, name, bp.get("schema", {}), hpar[k].get("schema", {})
                )
            for k, hp in sorted(hpar.items(), key=str):
                if k not in bpar and hp.get("required"):
                    out.append(f"{ep} 파라미터 {k[1]}({k[0]}) [request]: 새 필수 파라미터")
            bbody = _deref(bop.get("requestBody", {}), base)
            hbody = _deref(hop.get("requestBody", {}), head)
            if bbody and not hbody:
                out.append(f"{ep} [request]: requestBody 삭제")
            elif hbody and not bbody and hbody.get("required"):
                out.append(f"{ep} [request]: 새 필수 requestBody")
            elif bbody and hbody:
                if hbody.get("required") and not bbody.get("required"):
                    out.append(f"{ep} [request]: requestBody 선택 -> 필수")
                bs, hs = _json_schema(bbody.get("content")), _json_schema(hbody.get("content"))
                if bs is not None and hs is None:
                    out.append(f"{ep} [request]: requestBody 스키마 삭제")
                elif bs is not None:
                    out += check_schema(base, head, REQ, f"{ep} requestBody", bs, hs)
            hres = hop.get("responses", {})
            for code, br in sorted(bop.get("responses", {}).items()):
                if code not in hres:
                    out.append(f"{ep} [response]: 상태 코드 {code} 삭제")
                    continue
                bs = _json_schema(_deref(br, base).get("content"))
                hs = _json_schema(_deref(hres[code], head).get("content"))
                if bs is None:
                    continue
                if hs is None:
                    out.append(f"{ep} [response]: {code} 응답 스키마 삭제")
                    continue
                out += check_schema(base, head, RES, f"{ep} {code}", bs, hs)
    return out


# ---------------------------------------------------------------- 트리 비교


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def classification(base_dir: Path) -> dict[str, tuple[str, ...]]:
    return {
        p.name[:-5]: classify(p.name[:-5].rsplit(".", 1)[-1])
        for p in sorted((base_dir / "schemas").glob("*.json"))
    }


def compare_trees(base_dir: Path, head_dir: Path) -> list[str]:
    out: list[str] = []
    hs = {p.name: p for p in (head_dir / "schemas").glob("*.json")}
    for bpath in sorted((base_dir / "schemas").glob("*.json")):
        name = bpath.name[:-5]
        if bpath.name not in hs:
            out.append(f"{name}: 모델 삭제")
            continue
        b, h = _load(bpath), _load(hs[bpath.name])
        for direction in classify(name.rsplit(".", 1)[-1]):
            mode = "validation" if direction == REQ else "serialization"
            out += check_schema(b[mode], h[mode], direction, name)
    ho = {p.name: p for p in (head_dir / "openapi").glob("*.json")}
    for bpath in sorted((base_dir / "openapi").glob("*.json")):
        if bpath.name not in ho:
            out.append(f"openapi/{bpath.name}: head에 없음 (앱 삭제?)")
            continue
        out += check_openapi(_load(bpath), _load(ho[bpath.name]), bpath.name[:-5])
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: compat-check.py <base_dir> <head_dir>", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    problems = list(dict.fromkeys(compare_trees(Path(argv[1]), Path(argv[2]))))
    if problems:
        print(f"계약 호환성 깨짐 {len(problems)}건:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("계약 호환성 OK (깨지는 변경 없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
