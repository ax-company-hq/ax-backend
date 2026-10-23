# #228 시나리오 A 보상 쿠폰 호출 주체 — 고객지원 입장

## 의견: A (총괄은 보상·쿠폰 단계를 plan에서 뺀다)
- 기준 문서가 이미 A와 같다. DEP-CS-02(`docs/requirements.md:121`)는 "고객지원이 마케팅 `issue_coupon(compensation)`에 직렬 요청"이고, `docs/agent-common.md:210`도 보상 쿠폰을 support 요청 직렬 처리로 적는다. 총괄이 마케팅 `issue_coupon`을 따로 부르면 이 합의와 어긋난다.
- `request_compensation` 입력은 문의 1건 단위(`inquiry_id`, `buyer_id`, `reason_code`, `count`)다. 총괄은 이 값이 없고, 단가 판정은 고객지원 정책표(`compensation.py`)가 한다. 총괄이 `issue_coupon`을 직접 부르면(C, 또는 A에서 plan 단계를 남기는 경우) 정책표 우회와 이중 지급이 된다.
- B는 새 skill이 필요하고, 사건 구간 문의 조회를 고객지원이 새로 만들어야 한다. 지금은 권하지 않는다.

## 2. `sequence`
지금처럼 고객지원이 붙인다 (`agents/support/README.md:38`). 사건(`context_id`)별로 1부터, 마케팅이 소비한 경우에만 번호를 올린다. 총괄은 `sequence`를 만들지 않는다.

## 총괄 plan에 알릴 점
- 문의에서 파생된 보상은 문의 사건의 `context_id`를 승계한다 (`docs/agent-common.md:200`). 총괄은 `incident.opened`에 공지 `notice_id`를 실어 알리면 된다.
- 사건 종료 때 재무 `loss_aggregation`만 부르는 것은 고객지원 입장에서 문제없다. 보상액은 재무 장부의 `coupon.issued`로 계산한다.
- 정정: `support.inquiry_received`는 고객지원 문의 위젯이 **발행**하는 이벤트이고 구독자는 총괄이다 (`docs/agent-common.md:151`). 제안문의 "고객지원이 `support.inquiry_received`를 받아"는 "고객지원이 문의 접수 시점에 자동응답·보상 판정을 이어서 한다"로 읽는다.

## 남은 일 (이 PR 범위 밖)
- 문의 접수 → 보상 판정 → `request_compensation` 호출을 자동으로 잇는 경로는 아직 없다. 지금 `request_compensation`은 skill로만 호출된다 (dashboard-api, orchestrator). 자동 연결 방식(문의 처리 핸들러 안에서 호출 vs 별도 트리거)은 마케팅 요청 경로와 함께 병환·재헌이 정한다. 구현은 결정 후 별도 이슈로 한다.
- 총괄 plan(`orchestrator/`) 수정과 가짜 부서 정리는 orchestrator 몫이다.
