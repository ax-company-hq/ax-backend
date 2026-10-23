"""compat-check.py 규칙 테스트. 스크립트를 경로로 직접 로드한다."""

import importlib.util
import itertools
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "compat-check.py"
_spec = importlib.util.spec_from_file_location("compat_check", SCRIPT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


def write_tree(root: Path, schemas=None, openapi=None):
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "openapi").mkdir(parents=True, exist_ok=True)
    for name, schema in (schemas or {}).items():
        doc = {"validation": schema, "serialization": schema}
        (root / "schemas" / f"{name}.json").write_text(json.dumps(doc))
    for name, doc in (openapi or {}).items():
        (root / "openapi" / f"{name}.json").write_text(json.dumps(doc))
    return root


_n = itertools.count()


def run(tmp_path, base_schemas, head_schemas):
    d = tmp_path / f"run{next(_n)}"  # 한 테스트에서 여러 번 불러도 서로 섞이지 않게
    b = write_tree(d / "b", base_schemas)
    h = write_tree(d / "h", head_schemas)
    return cc.compare_trees(b, h)


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741


def test_classify():
    assert cc.classify("FooRequest") == (cc.REQ,)
    assert cc.classify("FooResult") == (cc.RES,)
    assert cc.classify("Escalation") == (cc.REQ, cc.RES)


def test_request_required_add_fails(tmp_path):
    base = {"m.FooRequest": obj({"a": S}, ["a"])}
    head = {"m.FooRequest": obj({"a": S, "b": S}, ["a", "b"])}
    assert any("필수 필드 추가" in p for p in run(tmp_path, base, head))


def test_request_optional_add_ok(tmp_path):
    base = {"m.FooRequest": obj({"a": S}, ["a"])}
    head = {"m.FooRequest": obj({"a": S, "b": S}, ["a"])}
    assert run(tmp_path, base, head) == []


def test_request_optional_to_required_fails(tmp_path):
    base = {"m.FooRequest": obj({"a": S})}
    head = {"m.FooRequest": obj({"a": S}, ["a"])}
    assert run(tmp_path, base, head)


def test_field_removal_fails_both_directions(tmp_path):
    base = {"m.FooRequest": obj({"a": S, "b": S}), "m.BarResult": obj({"a": S, "b": S})}
    head = {"m.FooRequest": obj({"a": S}), "m.BarResult": obj({"a": S})}
    problems = run(tmp_path, base, head)
    assert len([p for p in problems if "필드 삭제" in p]) == 2


def test_response_field_add_ok(tmp_path):
    base = {"m.BarResult": obj({"a": S}, ["a"])}
    head = {"m.BarResult": obj({"a": S, "b": S}, ["a", "b"])}
    assert run(tmp_path, base, head) == []


def test_response_required_to_optional_fails(tmp_path):
    base = {"m.BarResult": obj({"a": S}, ["a"])}
    head = {"m.BarResult": obj({"a": S})}
    assert any("필수 -> 선택" in p for p in run(tmp_path, base, head))


def test_response_optional_to_required_ok(tmp_path):
    base = {"m.BarResult": obj({"a": S})}
    head = {"m.BarResult": obj({"a": S}, ["a"])}
    assert run(tmp_path, base, head) == []


def test_type_change_fails(tmp_path):
    base = {"m.BarResult": obj({"a": S}, ["a"])}
    head = {"m.BarResult": obj({"a": I}, ["a"])}
    assert any("타입 변경" in p for p in run(tmp_path, base, head))


def test_integer_to_number_request_ok_response_fails(tmp_path):
    num = {"type": "number"}
    assert run(tmp_path, {"m.XRequest": obj({"a": I})}, {"m.XRequest": obj({"a": num})}) == []
    assert run(tmp_path, {"m.XResult": obj({"a": I})}, {"m.XResult": obj({"a": num})})


def test_nullable_response_rules(tmp_path):
    opt = {"anyOf": [S, {"type": "null"}]}

    # 값 -> 값|null : 응답에서 깨짐, 요청에서 호환
    def one(kind, old, new):
        return run(tmp_path, {kind: obj({"a": old}, ["a"])}, {kind: obj({"a": new}, ["a"])})

    assert one("m.XResult", S, opt)
    assert one("m.XRequest", S, opt) == []
    # 값|null -> 값 : 응답 호환, 요청 깨짐
    assert one("m.XResult", opt, S) == []
    assert one("m.XRequest", opt, S)


def test_optional_field_nullable_not_required_to_optional(tmp_path):
    # required에 없던 필드가 X -> X|None 이 되는 건 필수->선택은 아니지만 null 추가 변경이다
    opt = {"anyOf": [S, {"type": "null"}]}
    base = {"m.XResult": obj({"a": S})}
    head = {"m.XResult": obj({"a": opt})}
    problems = run(tmp_path, base, head)
    assert problems and not any("필수 -> 선택" in p for p in problems)


def test_enum_rules(tmp_path):
    e1 = {"type": "string", "enum": ["a", "b"]}
    e2 = {"type": "string", "enum": ["a", "b", "c"]}
    assert run(tmp_path, {"m.XResult": obj({"k": e1})}, {"m.XResult": obj({"k": e2})})
    assert run(tmp_path, {"m.XResult": obj({"k": e2})}, {"m.XResult": obj({"k": e1})}) == []
    assert run(tmp_path, {"m.XRequest": obj({"k": e2})}, {"m.XRequest": obj({"k": e1})})
    assert run(tmp_path, {"m.XRequest": obj({"k": e1})}, {"m.XRequest": obj({"k": e2})}) == []


def test_model_removed_fails_added_ok(tmp_path):
    base = {"m.AResult": obj({"a": S})}
    assert any("모델 삭제" in p for p in run(tmp_path, base, {}))
    assert run(tmp_path / "x", {}, base) == []


def test_unknown_class_checked_both_ways(tmp_path):
    base = {"m.Thing": obj({"a": S})}
    head = {"m.Thing": obj({"a": S, "b": S}, ["b"])}  # 요청 규칙에 걸림
    assert run(tmp_path, base, head)


def test_ref_resolution_and_nested(tmp_path):
    def schema(inner_props, inner_required=()):
        return {
            "type": "object",
            "properties": {"inner": {"$ref": "#/$defs/Inner"}},
            "required": ["inner"],
            "$defs": {"Inner": obj(inner_props, inner_required)},
        }

    base = {"m.OutResult": schema({"x": S, "y": S}, ["x"])}
    # ref 안쪽 필드 삭제는 최상위 경로 inner.y 로 보고돼야 한다
    problems = run(tmp_path / "1", base, {"m.OutResult": schema({"x": S}, ["x"])})
    assert any("inner.y" in p and "필드 삭제" in p for p in problems)
    # ref 안쪽 필수 -> 선택
    problems = run(tmp_path / "2", base, {"m.OutResult": schema({"x": S, "y": S})})
    assert any("inner.x" in p for p in problems)
    # 동일하면 통과
    assert run(tmp_path / "3", base, base) == []


def test_recursive_ref_does_not_hang(tmp_path):
    tree = {
        "type": "object",
        "properties": {"child": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]}},
            }
        },
    }
    assert run(tmp_path, {"m.TreeResult": tree}, {"m.TreeResult": tree}) == []


def test_array_items_nested(tmp_path):
    arr = lambda t: obj({"xs": {"type": "array", "items": t}})  # noqa: E731
    assert run(tmp_path, {"m.XResult": arr(S)}, {"m.XResult": arr(I)})


# ---- OpenAPI


def op(params=(), body=None, resp=None):
    o = {"parameters": list(params), "responses": {"200": {"description": "ok"}}}
    if body is not None:
        o["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    if resp is not None:
        o["responses"]["200"]["content"] = {"application/json": {"schema": resp}}
    return o


def oa(paths, schemas=None):
    return {"paths": paths, "components": {"schemas": schemas or {}}}


def run_oa(tmp_path, base, head):
    d = tmp_path / f"oa{next(_n)}"
    b = write_tree(d / "b", openapi={"api": base})
    h = write_tree(d / "h", openapi={"api": head})
    return cc.compare_trees(b, h)


def test_openapi_removed_endpoint_fails_added_ok(tmp_path):
    base = oa({"/a": {"get": op()}, "/b": {"get": op()}})
    head = oa({"/a": {"get": op()}})
    assert any("엔드포인트 삭제" in p for p in run_oa(tmp_path / "1", base, head))
    assert run_oa(tmp_path / "2", head, base) == []


def test_openapi_params(tmp_path):
    q = lambda n, req: {"name": n, "in": "query", "required": req, "schema": S}  # noqa: E731
    base = oa({"/a": {"get": op([q("x", False)])}})
    added = oa({"/a": {"get": op([q("x", False), q("y", False)])}})
    assert run_oa(tmp_path / "1", base, added) == []
    assert run_oa(tmp_path / "2", base, oa({"/a": {"get": op([q("x", False), q("y", True)])}}))
    assert run_oa(tmp_path / "3", base, oa({"/a": {"get": op([])}}))


def test_openapi_refs_request_and_response(tmp_path):
    def doc(req_props, req_required, resp_props, resp_required=()):
        return oa(
            {
                "/a": {
                    "post": op(
                        body={"$ref": "#/components/schemas/In"},
                        resp={"$ref": "#/components/schemas/Out"},
                    )
                }
            },
            {"In": obj(req_props, req_required), "Out": obj(resp_props, resp_required)},
        )

    base = doc({"a": S}, ["a"], {"r": S}, ["r"])
    assert run_oa(tmp_path / "1", base, base) == []
    # 응답 필드 추가, 요청 선택 필드 추가는 호환
    ok = doc({"a": S, "o": S}, ["a"], {"r": S, "n": S}, ["r"])
    assert run_oa(tmp_path / "2", base, ok) == []
    # 요청 필수 추가
    bad_req = doc({"a": S, "o": S}, ["a", "o"], {"r": S}, ["r"])
    assert any("requestBody" in p for p in run_oa(tmp_path / "3", base, bad_req))
    # 응답 필드 삭제
    bad_res = doc({"a": S}, ["a"], {})
    assert any("200" in p and "필드 삭제" in p for p in run_oa(tmp_path / "4", base, bad_res))


def test_main_exit_codes(tmp_path, capsys):
    b = write_tree(tmp_path / "b", {"m.AResult": obj({"a": S})})
    same = write_tree(tmp_path / "s", {"m.AResult": obj({"a": S})})
    broken = write_tree(tmp_path / "x", {"m.AResult": obj({})})
    assert cc.main(["x", str(b), str(same)]) == 0
    assert cc.main(["x", str(b), str(broken)]) == 1
    assert "필드 삭제" in capsys.readouterr().out
