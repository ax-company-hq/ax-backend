# hr 야간 작업 보고 (10/22 저녁 ~ 10/23 아침)

브랜치 `feat/hr-impl` (origin에 백업 푸시됨). PR은 만들지 않았다.

## 1. 한 일

| 단계 | 내용 | 커밋 |
|---|---|---|
| 1-1 기록 | `record_case`: 결정 탐지·카드 조립·확신도 규칙·연결(대체)·멱등성·목 저장소·카드 덱 탭 | 586aa33 |
| 1-2 명부·교대 | 시드 명부(영역 6, 담당 9), 공백 점검(담당 없음/교대 공백), `get_roster`, 명부 탭 | 3eda5b9 |
| 1-3 인수인계·온보딩 | `start_handover`·`answer_handover_question`·`confirm_handover`(B/C), `start_onboarding`, 탭 2개 | 7f5a818 |
| 1-4 발령·퇴직·입사 | `request_assignment_change`·`confirm_reassignment`(B)·`confirm_offboarding`(C), 잠금, 발령·퇴직 탭 | 14a1101 |
| 1-5 계획 비교 | `check_plan`: 기각 이력·충돌·일치 판정, 경과 일수 코드 계산, 계획 비교 탭 | 99f5e76 |
| 1-6 공통 마무리 | LLM 대체(fallback) 표시, 숫자 대조, 인젝션, 감사 로그 테스트 | 473e719 |
| 2 검토 준비 | 단독 테스트(공통 8 + 고유 4), README 5항목, `agent-card.json` skill 10개, 안전 등급 신고표 초안 | e7617ca |
| 추가 | 교대 시각 점검 타이머(`HR_TIMER=1`일 때 주기 실행, 데모 버튼) | (마지막 커밋) |

검증: hr 테스트 125개 통과, 데모 시나리오 19개 전부 기대 결과 일치·결정적, `ruff check`·`lint-imports` 통과, 레포 전체 pytest(support 제외) 529개 통과.
데모는 `uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114 (탭 7개).

## 2. 내가 정한 것 (아침에 확인해 주세요)

1. **완성도 = 채운 필수 항목 / 5 × 100**, 확정 기준 80% (제안값). 필수 항목: 진행 중인 일·결정과 이유·되돌리는 기준·연락처·알려진 위험. 진행 중(planning)·결정(카드)·연락처(명부)는 자동, 나머지는 인계자에게 질문.
2. **확신도**: 이유·대안·근거가 모두 있으면 high(자동 확정), 둘이면 mid, 그 밖은 low. high만 바로 확정하고 나머지는 후보.
3. **완성도 미달 확정은 별도 액션 `confirm_handover_incomplete`(C)** 로 관문에 올린다. 같은 액션 이름에 B/C를 섞을 수 없어서 나눴다. 신고표에 포함.
4. **`members` 테이블 추가**(hr 소유): 입사자·퇴직자를 명부와 별개로 기억하기 위해.
5. **교대 규칙**: 교대 확정 시 맡은 담당은 넘기고 본인은 예비 담당으로 남는다. 발령·퇴직은 본인이 그 영역에서 빠진다. 예비 담당이 담당으로 승격하면 예비 역할은 지운다.
6. **퇴직은 두 단계**: `confirm_reassignment`(B)로 일을 넘기고 → `confirm_offboarding`(C, 보류 큐 승인 후 같은 요청 재전송). 다른 영역에서 유일한 담당이면 적용하지 않는다.
7. **hr이 스스로 시작하는 인수인계(타이머)의 사건 번호는 `ctx_hnd_shift_<영역>_<시각>`** — 임시 접두사. 계약에 정식 규칙이 없다.
8. **계획 비교 유사도**: 카드 제목·결정·채택 안 한 대안과 초안의 2-gram 겹침. 후보 확신도 카드는 참고 목록에만 넣고 판정 근거로 쓰지 않는다. LLM은 기각 경고 문장만 다듬고 실패하면 고정 문장.
9. C 보류 사유가 마땅치 않아 `over_threshold`를 임시로 썼다(신고표 질문 2).

## 3. 계약·타 파트에 필요한 후속 (아무것도 건드리지 않음, 아침에 정해 주세요)

- **계약 후속 PR 후보**: `Card.status`에 `candidate`, `Card`에 `area/owner/alternatives_rejected/supersedes` 필드, `RecordCase.area`, hr이 시작하는 사건 번호 접두사. 지금은 `Card.extra`와 `source_refs`(`area:<영역>`)로 우회했다.
- **태윤**: `docs/notes/hr/safety-grade-registration-draft.md`(registry.toml 제안 + 질문 5개). `confirm_reassignment` 검증자 형식이 가장 큰 질문.
- **재현·태윤 Slack 답변 대기**: 카드 검색·후보 API, 보류 큐 `params.summary`. 답이 오면 `clients/`의 목만 바꾸면 된다.
- PR #103(계약)은 예진 재승인·태윤·준하 승인 대기. 봇 계약 PR(#63 #66 #72 #92 #96)은 준하 승인 대기. #103은 #92와 `schemas/__init__.py`에서 충돌할 가능성이 있다.

## 4. 알려진 한계

- 모든 외부 연결은 목이다(회사 기억·planning·안전 레이어). 3단계에서 `clients/`만 교체한다.
- 인계자 질문의 대기 시간 제한(`handover_question_ttl_minutes`)은 설정만 있고 만료 처리는 아직 없다.
- 온보딩 진행률을 올리는 요청(단계 완료 표시)은 계약에 없다. 인수인계 확정 단계만 자동으로 완료된다.
- 레포 전체 `ruff format --check`는 `.github/scripts` 등 hr 밖 파일 7개가 걸린다(기존 상태, 건드리지 않음). `agents/support`의 `test_fail_closed_when_safety_unreachable`는 dev에서도 실패한다(기존).

## 5. 아침에 할 일 제안

1. 데모(8114)를 열어 탭 7개를 훑어보기 — 특히 인수인계·발령·퇴직 탭의 보류 큐 승인 흐름.
2. 위 "정한 것" 1·2·5의 기준 확인.
3. 2단계 검토가 끝나면 3단계(목 → 실제 memory/planning/safety 교체) 진행.

## 6. 덤: 다른 에이전트 Agent Card 읽기 점검 (origin/dev 기준, 읽기만 함)

| 에이전트 | 지금 skill |
|---|---|
| planning | 없음 (`list_in_progress`·`verify_handover`가 아직 없어 hr 실제 연동 대기) |
| infra | agent-card.json 없음 |
| support | 19개 (조회·상태 변경·`process_claim_queue` B) |
| marketing | 4개 (`issue_coupon` B 등) |
| finance | 4개 (`verify_amount` 등) |
| data | 비어 있음 |

hr 실제 연동(3단계)은 planning 두 skill이 올라온 뒤에 가능하다.
