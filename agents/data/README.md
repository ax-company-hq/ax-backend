# agents/data
담당: 재현 · 포트: 8106
## 1. 역할
데이터분석 에이전트. 지표 이상 탐지, 장애 영향 분석(실패 주문 수·영향 고객·피해 금액), 판매자 사기 위험 점수를 맡는다. 숫자는 shop-api 조회 결과로 코드가 계산하고, LLM은 해석만 한다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up data` / 목 모드: `DATA_MOCK=1`
## 3. 입출력 계약
contracts/ 참조 (TODO)
## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
(TODO: 담당자가 작성)
