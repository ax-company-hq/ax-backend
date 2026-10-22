"""ax-agent-template — 담당: 준하.

모든 에이전트가 이 템플릿에서 시작한다. 안전장치(루프 거부·홉 제한·인증·ACL·마감·구조화 출력
검증·실행 관문)는 이 패키지가 강제하므로 우회하는 코드를 쓰지 않는다.

주요 진입점:

- `create_agent_app(card, handlers)`: Agent Card 공개와 A2A 요청 수신 (`server.py`)
- `ax_agent_template.execute.execute(...)`: 상태를 바꾸는 모든 액션의 실행 관문
- `call_agent(target, req)`: 다른 에이전트 호출 (`client.py`)
- `LLMClient`·`MockLLMClient`: LLM 호출 (`llm.py`)
- `publish`·`subscribe`: 이벤트 버스 (`events.py`)
"""

from ax_agent_template.client import call_agent
from ax_agent_template.events import publish, subscribe
from ax_agent_template.llm import LLMClient, MockLLMClient
from ax_agent_template.logging import log_event
from ax_agent_template.queue import SerialQueue
from ax_agent_template.server import create_agent_app

__all__ = [
    "LLMClient",
    "MockLLMClient",
    "SerialQueue",
    "call_agent",
    "create_agent_app",
    "log_event",
    "publish",
    "subscribe",
]
