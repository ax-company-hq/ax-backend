# hr 에이전트 설계

실제 회사의 이 부서: 인사운영팀(People Ops) — 인수인계, 업무 기록(결정 이력), 인사평가 운영
벤치마킹 솔루션: (제안, 사람이 확인) 평가·성과 — 레몬베이스, Lattice / 인사 정보·근태 — 플렉스, Workday. 채용공고로 확인한 것은 아니고 일반적인 인사운영 직무 기준이다 (`hr-benchmark-skills.md` 참고)
협업 부서: orchestrator, planning, memory, safety

1. 누구의 어떤 일을 대신하나 (이 부서 고유 업무 3가지):
   - 업무 기록(회사 기억의 입구): 부서가 보낸 사건·결정(요약·결정 이유·검토한 대안·근거)을 결정 카드로 정리해 저장한다 (`record_case`). 확신이 낮으면 후보 카드로 보류하고 사람이 승인·폐기한다 (`list_candidate_cards`, `decide_candidate_card`). 새 계획을 같은 영역의 결정과 비교해 기각된 안과 비슷하면 알린다 (`check_plan`). 사건 하나에서 hr이 한 일은 시간순으로 볼 수 있다 (`get_audit_timeline`).
   - 인수인계(담당자가 바뀔 때): 인계 초안을 진행 중인 일(기획개발)·결정 카드(회사 기억)·연락처(명부)로 채우고 빈 곳은 인계자에게 묻는다 (`start_handover`, `answer_handover_question`, `confirm_handover`). 영역별 담당자는 `get_roster`로 본다. 온보딩 안내와 발령·교대·퇴직 처리는 이 에이전트의 기능이 아니다(2026-10-26에 뺐다).
   - (설계 단계, 확정 아님) 인사평가 및 관리 자동화: 평가 주기·제출 현황 관리, 결과 취합·보정 자료, 업무 기록을 근거로 한 평가 초안 지원. 그리고 멘토 요청인 인사평가 에이전트 평가(결과의 수치·근거 점검, 편향 점검, 사람 수정률, 회귀 평가). 점수와 최종 판단은 사람이 한다.
2. 스스로 판단하는 것 / 사람(safety 큐)에게 넘기는 것:
   - 스스로(코드가 계산): 인수인계 완성도·빈 자리 수·경과 일수, 같은 요청·같은 답은 한 번만 반영. AI는 설명 문장만 만들고 실패하면 고정 문장으로 대신한다.
   - safety 규칙: 인수인계 확정은 B(planning 교차검증), 완성도가 기준 아래인 확정은 C.
   - 사람에게: 후보 카드 승인·폐기, 인수인계 확정 미달(C). (설계) 인사평가 최종 확정(C).
3. 다른 부서에게 받는 요청 (누가·언제·무엇) = 내 skill 목록:
   - orchestrator · 담당자가 바뀔 때 · `start_handover`, `confirm_handover`
   - planning, support, marketing, finance, data, infra, orchestrator · 사건·결정을 남길 때 · `record_case`
   - 전 부서 · 담당 영역을 알아야 할 때 · `get_roster`
   - planning, orchestrator · 새 계획 초안을 확인할 때 · `check_plan`
   - dashboard-api · 화면용 조회와 사람 입력 · `list_handovers`, `list_open_questions`, `list_candidate_cards`, `get_audit_timeline`, `answer_handover_question`, `decide_candidate_card`
4. 다른 부서에게 하는 요청 (누구에게·무엇을) = 필요한 skill 목록:
   - planning · 인계 초안에 넣을 진행 중인 일 `list_in_progress`, 인수인계 확정 때 누락 교차검증 `verify_handover`
   - memory · 결정 카드 저장·검색 (`card_write`, `card_search`)
   - safety · 상태를 바꾸는 일의 등급 판정과 교차검증·사람 승인 대기 (`execute()` 경유)
   - (예정) data · 인사평가 설계가 확정되면 평가 근거 지표. 아직 요청하지 않는다.
5. 기억해야 하는 것 (memory 부서와 연결):
   - 결정 카드(원인·조치·결정 이유·검토한 대안·근거)는 회사 기억에 저장하고 같은 영역의 옛 결정은 "대체됨"으로 잇는다.
   - 인수인계 상태와 감사 로그(`get_audit_timeline`)는 hr 안에 둔다.
6. 잘하는지 보는 지표:
   - 인수인계 완성도와 빈 자리 수, 담당 공백 영역 수
   - 후보 카드 승인·폐기 비율, 결정 탐지 정확도(규칙 샘플 기준)
   - 안전 회귀 테스트 0건 잘못 통과, 응답 시간
7. 하지 않는 일 (다른 부서·시스템 규칙의 일):
   - 개인 평가 점수·승진 같은 최종 판단은 하지 않는다 (사람이 한다).
   - 권한 변경을 직접 실행하지 않는다. 목록을 만들어 각 부서에 전달만 한다.
   - 다른 부서 DB를 직접 건드리지 않고, 외부에서 온 글을 지시로 해석하지 않는다.
   - 채용·근태·급여는 아직 범위가 아니다 (미구현).
   - 인수인계는 총괄이 시킨 경우에만 시작한다.
