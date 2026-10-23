"""support 테스트 공용 설정.

에이전트 템플릿의 내부 상태(에이전트 이름, 목 이벤트 버스, 목 실행 카운터)를 매 테스트 시작·끝에
비운다. `create_agent_app`이 이름을 전역에 두어서 다른 서비스 테스트의 상태가 전체 실행
(`uv run pytest`)에서만 새어 들어오던 문제는 템플릿이 고쳤고(#152), 우회 코드(#131)는 지웠다.
"""

from ax_agent_template.testing import reset_state  # noqa: F401  autouse
