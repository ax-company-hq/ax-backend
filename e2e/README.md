# e2e — 시나리오 A 단계별 점수

사람 경로(재현·준하·태윤). 부서 폴더가 아니므로 부서 에이전트는 수정하지 않는다.

uv workspace 멤버가 아닌 일반 테스트 폴더다. 루트 `pyproject.toml`·`uv.lock`을 바꾸지 않고, 의존성은 루트 dev 그룹(pytest, pytest-asyncio)과 부서 패키지(`uv sync --all-packages`)로 충분하다. `e2e/conftest.py`가 `ax_e2e`를 이 폴더에서 import하게 한다.

- 단계 표: 총괄 12단계(`orchestrator/.../plans/scenario_a.py`를 읽어 생성) + 고객 경로 5단계 (`stages.py`)
- 호출부: `AX_E2E_MODE=mock|real` (기본 mock). mock은 부서 앱을 프로세스 안에서 ASGI로 잇는다. real은 `AX_REAL_HOST`(예: localhost)가 있으면 compose로 띄운 포트로 HTTP 호출해 단독 점수를 낸다 (없으면 pending), 연결은 아직 pending. real 결과는 `real-result.json`(stages[].real, totals.real_pass·real_runs_consecutive)으로 남는다. 실행은 `.github/workflows/real-run.yml`
- 점수: 단독(부서를 고정 입력으로 직접 호출) / 연결(사건 이벤트 하나를 총괄 `/api/events`에 넣어 12단계가 이어지는지, 사건 contextId 하나, 공통 로그 시간순) / 고객 경로
- 상태: pass / pending / broken / regressed. 같은 입력 3회가 모두 같아야 pass. 첫 실행은 기준선만 남긴다
- 결과: `e2e-result.json`(DevStatus 부분 형식, `ax_contracts.data_access.dev_status`) + `e2e-detail.json`(검사별 사유)
- 고객 경로의 연결 판정은 support 사건 조회(#272, #274)가 머지되기 전까지 pending (`blocked-by #272 #274`)

```
uv sync --all-packages --locked
uv run pytest e2e -q
AX_E2E_MODE=mock PYTHONPATH=e2e uv run python -m ax_e2e.scenario_a.cli run --out out --commit "$(git rev-parse HEAD)" [--prev prev/e2e-result.json]
```
