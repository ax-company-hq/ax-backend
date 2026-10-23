"""호출부 (AX_E2E_MODE): mock은 부서 앱을 프로세스 안에서 ASGI로 직접 잇는다.

- mock: 부서 앱을 import해 `agent_url(부서)` 주소로 ASGI에 연결한다 (`ax_agent_template.client`와
  총괄의 httpx를 같은 라우터로 바꿔 끼운다). LLM은 `LLM_*`를 비우고 `<PREFIX>_MOCK=1`로 목 처리한다.
- real: `AX_REAL_HOST`(예 localhost)가 있으면 compose가 연 포트로 HTTP 호출한다 (`call_dept`가
  라우터 없이 실제 연결). 없으면 스텁이라 모든 단계가 pending이다. 실서버 점수는 지금 단독만 낸다.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ax_e2e.scenario_a import paths

MODES = ("mock", "real")


def e2e_mode() -> str:
    mode = os.getenv("AX_E2E_MODE", "mock")
    if mode not in MODES:
        raise ValueError(f"AX_E2E_MODE는 mock|real: {mode}")
    return mode


def real_host() -> str | None:
    """실서버 호출 대상 호스트 (`AX_REAL_HOST`, 예 localhost). 없으면 real 모드는 스텁이다."""
    return os.getenv("AX_REAL_HOST") or None


def prepare_real_env(host: str) -> None:
    """real 모드: 부서 주소를 `http://<host>:<포트>`로 정한다 (compose가 포트를 호스트에 연다).

    토큰은 요청자별 `<PREFIX>_TOKEN` 환경변수를 쓴다 (real_run.py가 서버 PC `.env`에서 넘긴다).
    e2e 프로세스는 LLM을 직접 부르지 않으므로 LLM 키는 필요 없다.
    """
    from ax_agent_template.config import DEFAULT_PORTS, env_prefix

    os.environ["TENANT_ID"] = "shop-demo"
    for name, port in DEFAULT_PORTS.items():
        os.environ[f"AX_{env_prefix(name)}_URL"] = f"http://{host}:{port}"


def prepare_env(workdir: Path) -> None:
    """부서 앱을 import하기 전에 호출한다. 비밀값 없이 전부 목 모드."""
    from ax_agent_template.config import env_prefix

    for key in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        os.environ[key] = ""
    os.environ["TENANT_ID"] = "shop-demo"
    os.environ["AGENT_TEMPLATE_MOCK"] = "1"
    os.environ["ORCHESTRATOR_MOCK"] = "1"
    os.environ["ORCHESTRATOR_MOCK_LATENCY"] = "0"
    os.environ.pop("ORCHESTRATOR_REAL_DEPTS", None)
    for dept in paths.cards():
        os.environ[f"{env_prefix(dept)}_MOCK"] = "1"
        os.environ.pop(f"AX_{env_prefix(dept)}_URL", None)
    os.environ["AX_COMMON_LOG_DB"] = str(workdir / "common_log.db")
    # 부서가 자기 작업 폴더에 쓰는 파일은 임시 폴더로
    os.chdir(workdir)


@dataclass
class Apps:
    """로드한 부서 앱과 로드 실패 사유."""

    apps: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class _Routes:
    mounts: dict[str, httpx.ASGITransport] = {}


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


class RoutedAsyncClient(_ORIG_ASYNC_CLIENT):  # type: ignore[misc, valid-type]
    """부서 주소로 가는 요청만 ASGI 앱으로 보낸다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "transport" not in kwargs and "mounts" not in kwargs:
            kwargs["mounts"] = dict(_Routes.mounts)
        super().__init__(*args, **kwargs)


class _HttpxShim(types.ModuleType):
    """httpx와 같되 AsyncClient만 라우터로 바꾼 모듈 (전역 httpx를 건드리지 않는다)."""

    AsyncClient = RoutedAsyncClient

    def __getattr__(self, name: str) -> Any:
        return getattr(httpx, name)


def load_apps() -> Apps:
    from ax_agent_template.config import agent_url

    result = Apps()
    for dept in sorted(paths.cards()):
        module = paths.app_module(dept)
        try:
            if not module:
                raise ImportError("앱 모듈을 찾을 수 없음")
            app = importlib.import_module(module).app
        except Exception as exc:  # 부서 코드가 깨져도 다른 부서 점수는 낸다
            result.errors[dept] = f"앱 로드 실패: {type(exc).__name__}: {exc}"[:300]
            continue
        result.apps[dept] = app
        _Routes.mounts[agent_url(dept)] = httpx.ASGITransport(app=app)
    return result


def install_router() -> None:
    """call_agent(부서 간·총괄)와 카드 수집이 라우터를 거치게 한다."""
    shim = _HttpxShim("httpx")
    import ax_agent_template.client as client_mod
    import ax_orchestrator.real as real_mod

    client_mod.httpx = shim  # type: ignore[attr-defined]
    real_mod.httpx = shim  # type: ignore[attr-defined]


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ax-e2e-"))


def build_request(
    skill: str,
    data: dict,
    *,
    requester: str,
    context_id: str,
    untrusted: dict | None = None,
    sequence: int | None = None,
    idempotency_key: str | None = None,
):
    """`ax-test-call`(testclient.build_request)과 같은 형식의 요청."""
    import argparse

    from ax_agent_template import testclient

    args = argparse.Namespace(
        agent="",
        skill=skill,
        input=None,
        untrusted=None,
        requester=requester,
        token=None,
        context_id=context_id,
        tenant_id="shop-demo",
        path=[],
        idempotency_key=idempotency_key,
        sequence=sequence,
        timeout=30.0,
    )
    req = testclient.build_request(args)
    req = req.model_copy(update={"input": data, "untrusted": untrusted or {}})
    if e2e_mode() == "real":
        from ax_agent_template.config import env_prefix

        token = os.getenv(f"{env_prefix(requester)}_TOKEN")
        if token:  # 없으면 mock 토큰이 그대로 가서 실서버가 거부한다 (단계 실패 사유로 남는다)
            req = req.model_copy(update={"meta": req.meta.model_copy(update={"token": token})})
    return req


async def call_dept(dept: str, req) -> dict:
    """부서 앱의 `POST /a2a`를 부르고 결과 JSON을 돌려준다. 연결 오류도 dict로."""
    from ax_agent_template.config import agent_url

    async with RoutedAsyncClient() as client:
        try:
            resp = await client.post(
                f"{agent_url(dept)}/a2a", json=req.model_dump(mode="json"), timeout=30.0
            )
        except httpx.HTTPError as exc:
            return {"status": "failed", "outcome": f"unreachable:{type(exc).__name__}", "http": 0}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    body["http"] = resp.status_code
    return body


def utcnow() -> datetime:
    return datetime.now(UTC)


def window() -> tuple[datetime, datetime]:
    now = utcnow()
    return now - timedelta(minutes=15), now + timedelta(minutes=15)
