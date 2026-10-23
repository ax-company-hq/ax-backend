# 롤백 `to_version` 출처 (이슈 #237)

## 상태
infra 부서(`infra/`) 쪽 제안이다. 계약(`contracts/`)과 `agents/infra-dejafix`는 이 부서가 고치지 않는다. 최종 결정과 계약 PR은 예진 님(DJF 담당)이 확인해야 한다.

## 제안: A (infra가 롤백 대상 버전을 정한다)
- 총괄은 진단 결과의 `service`·`version`을 `service`·`from_version`으로만 넘긴다.
- `rollback_deployment`가 직전 정상 배포를 찾아 `to_version`을 정한다. 검증자 planning의 `verify_rollback_target`이 그 대상 배포를 확인한다.
- 이유
  - B는 `SuspectDeployment`(응답)를 바꿔야 하고, 진단이 롤백 대상까지 알려 주면 진단·실행 책임이 섞인다.
  - C는 planning에 배포 이력 skill이 없어 새 계약과 새 skill이 둘 다 필요하다.
  - 데모에서 롤백은 장애 주입 해제 호출이라(SHOP-06, DJF-04) 직전 버전은 DejaFix 쪽 이력에서 구하는 것이 가장 가깝다.

## 필요한 계약 변경 (contracts 부서)
`RollbackDeploymentParams.to_version: str` → `str | None = None`. 비어 있으면 infra가 정하고, 응답에 실제 롤백 대상 버전(`rolled_back_to`)을 돌려준다. 값이 오면 그 값을 쓰되 직전 정상 배포와 다르면 거절한다.

## 남는 문제
- `to_version`이 비어 있을 때 planning `verify_rollback_target`에 넘길 값은 infra가 정한 뒤의 `to_version`이어야 한다. 검증 입력이 `to_version`을 요구하므로(`agents/planning/.../handlers.py:361`), 정한 뒤 execute 요청을 만들어야 한다.
- 직전 정상 배포를 못 찾으면 `rejected_by_review`가 아니라 `invalid_input` 계열 outcome으로 끝내고 실행하지 않는다 (fail-closed).
