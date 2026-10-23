# hr 요청 #178 — 후보 카드·영역 검색·목록 조회·상태 변경

## 현재 상태 (확인한 것)
- `memory/src/ax_memory/main.py`에는 `/health`만 있다. 도구 서버(MCP, `POST /tools/<이름>`)는 아직 없다.
  호출 방식과 주소/포트는 도구 서버 구현 때 확정한다. 포트는 README 기준 8002이다.
- 계약 `Card`에는 `candidate`, `area`가 없다(#177 머지 전). `CardSearchInput.query`는 필수다.
- 저장소(`store.py`)에는 `list(kind, status, tenant_id)`와 `update_status`가 이미 있다. 후보 여부·영역 필터와 `candidate` 변경은 없다.

## 구현 방향 (계약이 정해지면)
1. 후보: `Card.candidate: bool = False`를 저장한다. 검색·목록은 기본값으로 후보를 제외하고 `candidate` 필터(`False` 기본, `True`, `None`=전체)로 고른다. 확정 전 카드를 확정 카드로 읽지 않게 하는 것이 목적이다.
2. 영역: 정식 필드(`Card.area`)와 `CardSearchInput.area` 필터를 권장한다. 그 전까지 hr의 `extra["area"]`는 필터 대상이 아니다.
3. 목록 조회: `memory_list_cards`(area·status·kind·candidate 필터, `query` 없음)를 별도 도구로 둔다. `memory_search_card.query`를 선택으로 바꾸는 것보다 계약 호환에 안전하다.
4. 상태 변경: `memory_set_card_status`(가칭) — 후보→확정(`candidate=false`), 후보→폐기(`status=retired`). 쓰기이므로 `memory.card_write` 액션과 `execute()`를 거친다. 후보가 아닌 카드의 확정 요청과 이미 폐기된 카드의 재변경은 거부한다.

## hr 확인 사항에 대한 답
- `card_id`는 memory가 정한다(`IdPrefix.CARD` + uuid). hr은 `memory_write_card`가 돌려준 ID를 쓰면 된다. 같은 멱등 키(`context_id + kind + 제목 해시`)로 다시 쓰면 기존 ID가 돌아온다(`created=false`).
- 호출 방식·주소: 위 "현재 상태" 참고. 확정 전까지 hr은 목 저장소를 유지한다.

## 선행 조건
- contracts: `Card.candidate`, `Card.area`(또는 확정된 위치), `CardSearchInput.area`/`candidate`, 목록·상태 변경 입출력 모델.
