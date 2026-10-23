"""hr이 밖으로 나가는 연결. 목과 실제는 같은 인터페이스(Protocol)라서 여기서만 교체한다.

- `memory`: 회사 기억(카드 저장·검색). 실제는 memory 서비스, 지금은 목 저장소.
- `planning`: planning의 `list_in_progress`·`verify_handover`. 지금은 목 데이터.
- `safety`: 실행 관문 `execute()`. 지금은 목 안전 레이어(등급 A/B/C 시뮬레이션).
"""
