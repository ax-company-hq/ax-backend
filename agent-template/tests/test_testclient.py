import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ax_agent_template import testclient


def _args(*extra: str):
    base = ["--agent", "http://localhost:9999", "--skill", "echo"]
    return testclient.build_parser().parse_args([*base, *extra])


def test_build_request_defaults_to_orchestrator_mock_token() -> None:
    request = testclient.build_request(_args())
    assert request.skill == "echo"
    assert request.meta.token == "mock:orchestrator"
    assert request.meta.tenant_id == "shop-demo"
    assert request.meta.context_id.startswith("ctx_req_")


def test_build_request_reads_input_files(tmp_path) -> None:
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"n": 5}), encoding="utf-8")
    untrusted_file = tmp_path / "untrusted.json"
    untrusted_file.write_text(json.dumps({"customer_text": "안녕"}), encoding="utf-8")
    request = testclient.build_request(
        _args(
            "--input",
            str(input_file),
            "--untrusted",
            str(untrusted_file),
            "--as",
            "finance",
            "--sequence",
            "2",
        )
    )
    assert request.input == {"n": 5}
    assert request.untrusted == {"customer_text": "안녕"}
    assert request.meta.token == "mock:finance"
    assert request.sequence == 2


def test_main_prints_result(app: FastAPI, monkeypatch, capsys, tmp_path) -> None:
    client = TestClient(app)
    monkeypatch.setattr(
        testclient.httpx, "post", lambda url, json, timeout: client.post("/a2a", json=json)
    )
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"n": 7}), encoding="utf-8")
    exit_code = testclient.main(
        ["--agent", "http://localhost:9999", "--skill", "echo", "--input", str(input_file)]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["output"]["input"] == {"n": 7}
