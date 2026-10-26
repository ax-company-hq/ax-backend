# AI 가상 인플루언서 파이프라인 — 마케팅 에이전트 설계 노트 (초안)

> 작성: 재헌(마케팅) · 2026-10-23 · 상태: **초안, 팀 의견 받는 중** · 범위: **B안(목 모드 MVP)** → 결과 확인 후 C안(실제 영상 생성)

## 1. 배경과 목표

멘토를 통해 실제 마케팅팀의 발제가 왔다. **AI 가상 인플루언서를 운영하기 위해 매일 아침 밈·트렌드를 수집·분석하고, 그걸로 쇼츠 영상을 기획하고, AI가 제작·평가·보고하는 파이프라인**을 마케팅 담당이 고민해 달라는 것이다.

- 우리 회사(캣북스: 책·문구, 고양이가 지키는 서점)에 붙이면 **가상 인플루언서 = 캣북스 고양이 캐릭터**로 쇼핑몰 상품(shop-api 카탈로그)과 바로 연결된다.
- 발표에서는 시나리오 A(장애 대응)와 별개의 **"평소 운영" 시나리오**가 된다(1부 "에이전트들이 회사를 운영한다").
- 이번 단계(B): **수집은 예시 데이터, 분석·기획·평가·보고는 실제 동작, 제작은 목(mock)**. 실제 영상 생성·게시(C)는 스위치 뒤에 두고 결과를 본 뒤 결정한다.

## 2. 파이프라인과 skill (안)

마케팅 에이전트의 skill로 푼다. 상태를 바꾸는 일은 모두 `execute()` 관문을 거치고(`executed` 뒤에만 실제 반영), 숫자·판정은 코드가 계산한다(LLM은 문장 초안만).

| 단계 | skill | 등급 | 입력 → 출력 | 비고 |
| --- | --- | --- | --- | --- |
| 아침 수집 | `collect_trends` | 읽기 | 수집 기준일·출처 목록 → `TrendItem[]` | 외부 글·밈 문구는 **전부 `untrusted`**. v0은 `fixtures/trends.json` |
| 분석 | `analyze_trends` | 읽기 | `TrendItem[]` → 순위화된 `TrendCandidate[]` | 점수는 코드(인기 변화율·상품 연결·브랜드 적합·위험). 근거 항목 ID를 붙인다 |
| 기획 | `plan_short` | 읽기(초안) | `TrendCandidate` → `ShortPlan` | 훅·컷별 스토리보드(6~8컷)·자막·해시태그·길이 15~30초·연결 상품. v0은 고정 템플릿(공지 초안 방식), LLM은 이후 |
| 제작 | `produce_short` | **B** | `ShortPlan` → `ProducedShort` | 비용이 드는 일. 목: 스토리보드 이미지·자리표시 영상 참조(`asset_ref`)와 `mock=true`. 실제 생성(C)은 `SHORT_PROVIDER` 스위치 |
| 평가 | `evaluate_short` | 읽기 | `ProducedShort` → `Evaluation` | **규칙 점검은 코드**(길이·금칙어·저작권·AI 표기·실존 인물), 품질 루브릭은 점수화(v0 규칙 기반) |
| 게시 | `publish_short` | **C** | `ProducedShort`+통과한 `Evaluation` → 게시 결과 | 외부 공개는 되돌릴 수 없다 → **사람 승인(safety 보류 큐)**. v0은 "게시 대기"까지만 |
| 보고 | `report_daily` | 읽기 | 기준일 → `DailyReport` | 후보 수·제작 수·평균 점수·비용·게시 대기. 관리자 마케팅 칸이 읽는다 |

묶음 실행은 `run_daily_pipeline`(읽기 skill들을 순서대로 호출하고 마지막에 `produce_short`만 관문을 거친다). 하루 한 번 트리거는 **총괄/스케줄 방식이 정해지면 연결**한다(TODO). v0은 수동 호출과 목 스케줄 함수.

## 3. 데이터 모델 (스키마는 `agents/marketing/` 안, 계약 변경 없음)

- `TrendItem`: `item_id`, `source`, `title`(untrusted), `captured_at`, `metrics`(조회·증가율), `tags`
- `TrendCandidate`: `candidate_id`, `item_ids`(근거), `topic`, `score`(0~100, 코드 계산), `brand_fit`, `risk_flags[]`, `product_ids[]`
- `ShortPlan`: `plan_id`, `candidate_id`, `hook`, `cuts[{n, scene, caption}]`, `duration_s`, `hashtags[]`, `product_id`, `disclosure`(AI 생성 표기 문구)
- `ProducedShort`: `short_id`, `plan_id`, `asset_ref`, `mock`, `cost_krw`(정수 원), `produced_at`
- `Evaluation`: `short_id`, `rule_checks[{code, ok, detail}]`, `rubric{hook, brand_fit, clarity, safety}`, `total`, `verdict`(pass·revise·reject)
- `DailyReport`: `date`, `candidates`, `produced`, `avg_score`, `cost_krw`, `awaiting_publish`, `items[]`

금액은 정수 원, 시간은 UTC, ID는 접두사 `shrt_`·`plan_`처럼 **마케팅 폴더 안에서만** 쓴다(계약 `IdPrefix`에 넣을 필요가 생기면 `to:contracts` 요청).

## 4. 안전·규칙 (코드로 강제)

1. **외부 입력 = 데이터.** 밈·트렌드 문구는 `untrusted`로만 받고 프롬프트에 지시로 넣지 않는다(프롬프트 주입 방지). 출력에는 원문을 그대로 싣지 않고 요약·ID로 참조한다.
2. **금지 목록(코드)**: 실존 인물 얼굴·목소리 모방, 저작권 있는 음원·밈 이미지 재사용, 정치·성인·혐오 주제, 허위·과장 광고 문구, 경쟁사 비방. 걸리면 `risk_flags`와 `Evaluation`에서 reject.
3. **AI 생성·광고 표기**는 `ShortPlan.disclosure`에 항상 넣고, 없으면 평가 단계에서 reject.
   - **방침(재헌 결정, 2026-10-26)**: AI가 조금이라도 쓰였으면 광고 여부와 상관없이 항상 공개한다. 표시는 **사용자가 육안으로 알아볼 수 있어야 한다.** 표시광고법·Meta(Instagram) 정책·약관과 인공지능기본법 표시를 모두 충족해야 하고, 위배되면 게시하지 않는다.
   - **표시 위치를 따로 검사한다(하나라도 없으면 평가 reject)**: ① 게시글(캡션)에 AI 생성 안내문 ② 영상 안에 보이는 안내문(후처리 오버레이, 생성 모델이 그린 글자에 의존하지 않는다) ③ 계정의 AI 라벨(사람 확인 체크리스트).
   - **육안 확인 기준(제안값, 변호사·가이드라인 원문 확인 전)**: 영상 전 구간에 표시, 읽을 수 있는 크기와 배경 대비. 구체적 크기·시간은 가이드라인 원문으로 정하고 코드에 박지 않고 설정에 둔다.
   - 안내문 문구와 위치는 한 곳(설정)에서 관리한다.
   - 게시 전 확인할 외부 기준(C안 전): 인공지능기본법 제31조와 과기정통부 투명성 가이드라인, 표시광고법 심사지침, Meta Platform Terms·Instagram 정책. 읽기 전에는 게시 어댑터를 켜지 않는다.
4. **비용 상한**: 일일 건수·금액 상한을 안전 레이어 기준표에 둔다(제안값은 태윤 님과 정함). 상한을 넘으면 `produce_short`는 C로 보류.
5. **게시는 사람 승인**(C). 자동 게시는 하지 않는다.
6. 모든 상태 변경에 `context_id`·`idempotency_key`(`context_id:produce_short:plan_id`). 같은 키 재요청은 이전 결과를 돌려준다.
7. **Instagram 정책 준수**: 쿠폰은 참여 조건 없는 공개 쿠폰으로 하고 제약 사항을 반드시 게시한다. AI 라벨은 모든 게시물에 무조건 넣는다(글만 AI여도 예외 없음). 상업 행위는 이용약관에 따른다고 표시한다. 상세는 `instagram-policy-checklist.md`.

## 5. 목 모드 설계 (B안)

- `fixtures/trends.json`: 하루치 예시 트렌드 8~10개(출처·지표는 가짜, 개인정보·실제 계정 없음).
- 분석·평가는 **결정적**(같은 입력 → 같은 출력)이라 테스트가 쉽다. LLM 연결은 문장 초안·품질 루브릭에만, 실패하면 고정 템플릿으로 되돌린다(`fallback=true`).
- `produce_short`는 목에서 스토리보드 이미지 목록(`asset_ref`는 로컬 경로 형태의 가짜)과 `cost_krw`를 정해 둔 값으로 돌려준다. `SHORT_PROVIDER=mock`(기본) / `<서비스>`(C안).
- 관리자 마케팅 칸에는 `report_daily`와 `list_shorts`(읽기 skill)를 dashboard-api가 A2A로 읽어 보여 준다(재헌 몫, 가이드 단계 4).

## 6. PR 계획 (한 번에 하나씩, 400줄 이하)

| PR | 내용 | 다른 부서 영향 |
| --- | --- | --- |
| 1 | 스키마 + fixtures + `collect_trends`·`analyze_trends` | 없음 |
| 2 | `plan_short`·`evaluate_short` (규칙 점검 포함) | 없음 |
| 3 | `produce_short`(목)·`report_daily`·`list_shorts`·`run_daily_pipeline` | safety 등록(요청) |
| 4 | `publish_short`(C, "게시 대기"까지) + 비용 기준 | safety 기준표(태윤) |
| 5 | 관리자 마케팅 칸(읽기) | dashboard-api(병환) 요청 |
| C | 실제 영상 생성 어댑터(스위치 뒤) | 비용·계정·약관 결정 필요 |

각 PR의 Agent Card에 skill을 더한다(`allowed_requesters`는 `dashboard-api`(읽기)·`orchestrator`(일일 트리거) 위주로 최소화).

## 7. 다른 부서에 필요한 일 (요청은 GitHub 이슈로)

- **safety(태윤)**: `produce_short`(B)·`publish_short`(C) 액션 등록, 일일 비용·건수 상한 기준값. → `to:safety` 이슈
- **contracts**: 새 이벤트(`short.produced`·`short.published`)가 필요해지면 `to:contracts` 요청(v0은 이벤트 없이 저장·조회만).
- **dashboard-api(병환)**: 마케팅 칸용 읽기 경로. → `to:support`(dashboard-api) 이슈
- **data(재현)**: 게시 성과 지표를 다음 날 분석에 되먹이는 건은 C안 이후.

## 8. 열린 결정 (사람이 정할 것, 임의 기본값을 채우지 않는다)

1. 게시 플랫폼(YouTube Shorts·Instagram Reels·TikTok)과 계정 — **C안에서만 필요**
2. 캐릭터 이름·말투·금지어 — `TODO(재헌)`
3. 영상 생성 서비스와 일일 비용 상한 — C안 전에 팀(태윤)과 정함
4. 트렌드 출처(어떤 API·약관) — v0은 예시 데이터라 보류
5. 하루 트리거 방식(총괄 스케줄 vs 마케팅 자체 cron) — 총괄 설계 확정 후

## 9. 하지 않는 것 (이번 범위 밖)

실제 외부 API 호출·실제 영상 생성·실제 게시, 개인정보·실존 인물 데이터 수집, 이벤트 계약 추가, 게시 성과 학습.
