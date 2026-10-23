# 요청 Issue (2026-10-23 보냄)

> **보낸 결과**: 재현 → #178, 예진 → #179, 태윤 → #180(아래 초안을 최신 상태에 맞춰 고쳐 보냄). 병환은 이미 hr에 요청(#164)을 올려 두었기 때문에 새 이슈 대신 **#164에 답변**을 남겼고, 같은 내용으로 #167(계약 요청)에도 계약 PR #177을 안내했다. 아래 1번 본문은 그 답변의 바탕이 된 초안이다(경로 이름은 `dashboard-api-contract.md`의 최신 표를 따른다).


팀 방식(`to:<부서>` 요청, 본문은 **결론 한 줄 → 이유 → 필요한 것 → 형식·샘플 → 영향**)에 맞춰 쓴 초안이다.
Issue는 만들지 않았다. 올릴 때는 아래 제목·본문을 그대로 쓰고 라벨(`to:…`)만 붙이면 된다.
공통 근거 문서: `docs/notes/hr/dashboard-api-contract.md`, `frontend-handoff.md`, `contract-followups.md`, `real-integration-assumptions.md`.
이 자료는 지금 hr 구현 브랜치(`feat/hr-impl`, 후속 `feat/hr-dashboard-data`)에 있다.

---

## 1. to:dashboard-api (병환) — 인사 탭 API 열기

**제목**: `to:dashboard-api 인사 탭 API를 support와 같은 방식으로 열어 주세요 (hr skill A2A 호출)`

**결론**: 대시보드 "인사" 탭이 쓸 엔드포인트 11개를 `/api/dashboard/...`로 열어 주세요. hr skill을 A2A로 부르면 되고, 모양은 문의 API(`support_client.py`)와 같습니다.

**이유**: hr이 화면에 보여 줄 현황(인수인계·질문·온보딩·변경·후보 카드·감사)과 사람 입력(질문 답변·온보딩 단계 완료·후보 카드 승인)을 skill로 제공하게 됐습니다. 화면 담당이 가짜 응답으로 먼저 만들 수 있도록 응답 샘플도 만들어 두었습니다.

**필요한 것**
1. `GET /api/dashboard/hr/{handovers,questions,onboardings,changes,roster,candidate-cards}`, `GET /hr-activity/{context_id}` — hr의 `list_handovers`·`list_open_questions`·`list_onboardings`·`list_changes`·`get_roster`·`list_candidate_cards`·`get_audit_timeline`을 부릅니다.
2. `POST /handover-questions/{id}/answer`(`answer_handover_question`, 본문은 계약 `HandoverAnswerRequest`), `POST /onboardings/{id}/steps/{step_id}`(`update_onboarding_step`), `POST /candidate-cards/{id}/decision`(`decide_candidate_card`), 선택 `POST /plan-checks`(`check_plan`).
3. 응답은 `{data, meta}`, 오류는 `{error:{code,message}}`. hr 결과 → HTTP 오류 매핑은 계약 문서 5장 표(`unwrap`과 같은 방식).
4. 목 모드(`DASHBOARD_MOCK=1`)는 `docs/notes/hr/dashboard-fixtures/*.json`을 그대로 돌려주면 됩니다. hr 주소는 `AX_HR_URL`(기본 `http://hr:8104`), 토큰은 기존 `DASHBOARD_API_TOKEN`.
5. 쓰기 요청의 멱등 키는 `ctx_req_<hex>:<skill>:<대상>`(대상 필수). hr은 같은 키·같은 대상은 한 번만 처리하고, **같은 키를 다른 대상에 쓰면 거부**합니다.

**정해 주세요** (계약 문서 8장): 경로 이름, 페이징, 갱신 주기(15초 폴링 가정), 사람 이름 출처, 후보 카드 승인 권한.

**영향**: hr Agent Card에 `dashboard-api`가 읽기 8개 + 위 쓰기 3개의 요청자로 들어 있습니다. 시작·확정 skill은 orchestrator 전용입니다.

---

## 2. to:memory (재현) — 후보 카드와 영역 검색을 위한 계약 보강

**제목**: `to:memory 후보 카드 상태·영역 검색·상태 변경 도구가 필요합니다 (hr 연동)`

**결론**: 지금 memory 계약(`CardWriteInput`·`CardSearchInput`·`CardLinkInput`)만으로는 hr의 "후보 카드 승인·계획 비교·인수인계"를 처리할 수 없어 4가지를 보강해 주세요.

**이유와 필요한 것**
1. **후보 상태**: `Card.status`에 `candidate`를 추가하고(`active`·`candidate`·`superseded`·`retired`), `CardSearchInput.status`로도 `candidate`를 검색할 수 있어야 합니다. 확신이 낮은 카드를 다른 부서가 memory에서 읽을 때 확정 카드로 오해하지 않게 하려는 것입니다. `CardDraft`에도 `status`(기본 `active`, `candidate` 허용)가 필요합니다. *(hr 쪽 계약 변경은 `feat/hr-dashboard-data`에 준비돼 있고 `Card.status`가 이미 반영됐습니다.)*
2. **영역 검색**: 계획 비교·인수인계·온보딩은 "같은 영역의 카드"만 봅니다. `CardSearchInput.area`(선택)를 추가하거나, 영역을 `extra`가 아니라 카드의 정식 필드로 두고 검색 필터를 열어 주세요. 현재 hr은 `extra["area"]`에 넣고 있습니다.
3. **목록 조회**: 질의어 없이 "이 영역의 이 상태 카드 전부"를 가져오는 방법(`memory_list_cards` 같은 도구, 또는 `query` 생략 허용). 후보 카드 큐가 이것을 씁니다.
4. **상태 변경**: 후보 → 확정(`active`), 후보 → 폐기(`retired`)를 바꾸는 도구(`memory_set_card_status` 가칭). 지금은 `memory_link_card`(대체·폐기 연결, 새 카드 필요)뿐입니다. 쓰기는 `memory.card_write` 액션을 거칩니다.
5. 확인: `memory_write_card`가 돌려주는 `card_id`는 memory가 정한 값이므로 hr은 **돌려받은 ID**를 쓰겠습니다(지금 hr이 만든 ID와 달라질 수 있음).
6. 전송 방식: 도구를 어떤 방식(MCP 서버, `POST /tools/<이름>`, 그 밖)으로 부르는지와 주소/포트를 알려 주세요. hr은 `HR_MEMORY_MODE=http`로 켜는 클라이언트를 준비해 두었고 가정은 `real-integration-assumptions.md`에 있습니다.

**영향**: 위가 정해지기 전까지 hr은 목 저장소로만 동작합니다(실제 연동 단계의 선행 조건).

---

## 3. to:planning (예진) — 인수인계용 skill 2개

**제목**: `to:planning list_in_progress·verify_handover skill을 열어 주세요 (hr 인수인계 B 검증)`

**결론**: 계약 PR #103(`contracts/.../planning.py`)에 있는 `list_in_progress`(읽기)와 `verify_handover`(읽기·검증) 두 skill을 planning Agent Card에 등록해 주세요. 지금 Card에는 `find_related_prs`·`link_incident`·`verify_rollback_target` 세 개뿐입니다.

**이유**: hr이 인수인계 초안을 만들 때 인계자의 진행 중 PR·티켓·배포를 `list_in_progress`로 가져오고, 확정(B) 전에 인계 목록에 빠진 진행 중 항목이 없는지 `verify_handover`로 교차검증합니다.

**필요한 것**
- `list_in_progress`: 입력 `{owner_id, area?}`, 출력 `{items: InProgressItem[], collected_at}`. `owner_id`는 hr 명부의 `member_id`(`emp_xx`), `area`는 명부의 영역 값(`payment`·`search`·`catalog`·`support-desk`·`infra-ops`·`inventory`)과 같습니다.
- `verify_handover`: 입력 `{handover_id, owner_id, area?, handover_item_ids[]}`, 출력 `{approved, reason_code, missing[], stale_item_ids[], checked_at}`. **사실 확인만** 합니다(진행 중 항목이 인계 목록에 빠짐없이 있는지). 내용을 판단하지 않습니다.
- 둘 다 상태를 바꾸지 않으므로 등급 `read`입니다. `allowed_requesters`는 `hr`(`verify_handover`는 안전 레이어 라우팅이면 `safety`도).
- 값이 없거나 오래됐으면 지어내지 않고 빈 목록과 수집 시각을 돌려주세요.

**영향**: 이 두 skill이 없으면 hr의 인수인계 확정(B)을 실제로 검증할 수 없고, hr은 목 데이터로만 동작합니다. 모델이 다르면 hr이 맞추겠으니 알려 주세요.

---

## 4. to:safety (태윤) — hr 액션 등급 등록과 질문

**제목**: `to:safety hr 액션 9개를 레지스트리에 등록해 주세요 (+ 검증자·보류 사유 질문)`

**결론**: hr의 상태 변경 액션 중 레지스트리에 있는 것은 `record_case`와 회사 기억(`memory.card_write`·`memory.card_link`)뿐이라 나머지 9개를 등록해 주세요. 그렇지 않으면 hr의 쓰기는 모두 C로 보류됩니다. 제안 항목은 `docs/notes/hr/safety-grade-registration-draft.md`와 로컬 초안 브랜치 `draft/hr-safety-registry`에 있고 `uv run pytest safety`(62개)가 통과합니다.

**등록 제안**: A — `start_handover`·`answer_handover_question`·`request_assignment_change`·`start_onboarding`·`update_onboarding_step`; B — `confirm_handover`(검증자 planning `verify_handover`), `confirm_reassignment`; C — `confirm_handover_incomplete`(완성도 기준 미만), `confirm_offboarding`.

**정해 주세요**
1. `confirm_reassignment`: "변경 뒤 담당 공백·중복이 없는가"를 검증할 방법. `safety` 코드 규칙(새 규칙 종류가 필요함)으로 할지, hr이 검증 skill을 열고 safety가 호출할지.
2. 기준 금액이 없는 C(퇴직 확정)의 `HoldReason`: 지금 hr 목은 `over_threshold`를 임시로 씁니다. `high_impact`(가칭) 같은 값을 추가할지.
3. 보류 건에 사람이 읽을 `summary`(예: `퇴직 확정 요청: emp_06 (support-desk)`)를 어디에 담아 보낼지. hr은 `params.summary`로 보냅니다.
4. 완성도 기준(제안 80%)을 `thresholds.toml`에 둘지 hr 설정에 둘지. hr이 액션 이름(`confirm_handover` vs `confirm_handover_incomplete`)으로 등급을 고르는 구조가 부담이면 기준 키를 safety로 옮기겠습니다.
5. 입력 오류를 `rejected`로 표현하려고 `ErrorCode.INVALID_INPUT` 추가를 제안합니다(계약 후속, `contracts/` 변경이라 승인이 필요합니다).
6. 안전 레이어에 닿지 못했을 때(`exe_unavailable`) hr은 사람이 거절한 것과 구분해 `GATE_UNAVAILABLE`로 처리합니다. `hold_reason`이 `verify_rejected`·`review_timeout`일 때만 "사람/검증자의 거절"로 봅니다. 이 가정이 맞는지 확인해 주세요.

**영향**: 위가 정해지기 전에는 hr의 B·C 경로를 실제 safety로 돌려 볼 수 없습니다.
