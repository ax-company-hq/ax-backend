# dejafix 에이전트 설계
1. 누구의 어떤 일을 대신하나 (하루 일 3가지): 고객사 인프라·운영 담당의 장애 대응을 대신한다.
   - Zabbix 감지 결과로 장애 원인을 진단하고 의심 배포를 찾는다.
   - 원인이 배포면 직전 정상 버전으로 롤백한다.
   - 롤백 뒤 서비스 상태(헬스·에러율)가 회복됐는지 확인한다.
2. 스스로 판단하는 것 / 사람(safety 큐)에게 넘기는 것:
   - 스스로: 원인 진단(정해지지 않으면 `undetermined`), 의심 배포 후보, 복구 확인.
   - 넘기는 것: 롤백 실행은 B 등급이라 safety 검증(planning `verify_rollback_target`)을 거치고, 검증에서 보류되면 사람 확인으로 넘어간다. 진단이 `undetermined`이면 롤백하지 않고 이관한다.
3. 다른 부서에게 받는 요청 (누가·언제·무엇) = skill 목록:
   - `diagnose_incident`: orchestrator가 사건을 열자마자 원인 진단을 요청할 때.
   - `rollback_deployment`: orchestrator가 진단 결과로 롤백을 요청할 때(B). `to_version`은 A안(#239)대로 infra가 배포 이력에서 정한다.
   - `verify_recovery`: orchestrator가 롤백 뒤 복구 확인을 요청할 때.
4. 다른 부서에게 하는 요청 (누구에게·무엇을):
   - planning: 장애 시각 직전 머지된 PR 후보(`find_related_prs`).
   - safety: 롤백 실행 검증.
   - orchestrator: 감지 이벤트(`infra.anomaly_detected`)와 복구 결과.
5. 기억해야 하는 것 (memory 부서와 연결): 배포 이력과 정상 버전, 장애별 원인·롤백 결과, 같은 컴포넌트의 반복 장애.
6. 잘하는지 보는 지표: 감지에서 진단까지 걸린 시간, 롤백 뒤 서비스 상태 회복 시간, 롤백 성공률, 원인 미확정 비율. 업무 지표 회복은 data가 판정한다(겹치지 않게).
