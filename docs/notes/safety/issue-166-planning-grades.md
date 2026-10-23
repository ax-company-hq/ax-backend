# #166 기획개발 액션 등급·자동 기준

## 반영한 것
- `registry.toml`: `write_spec`·`split_tasks`·`review_pr`·`send_message`·`confirm_spec`은 A, `merge_pr`은 B(검증자 = safety 코드 규칙).
- `thresholds.toml`: 이슈 2번 표의 규칙 전부. 값은 제안값 그대로(PR 크기·경로는 `generate_pr`와 동일).
- 규칙 연산자에 `<`·`<=` 추가 (`basis_count < 1` 표현용).
- `thresholds.toml`의 `version`은 올리지 않았다(테스트가 1로 고정). 사람이 PR 머지 때 올릴지 정한다.

## planning이 보낼 params (정수, 없거나 정수가 아니면 C)
- `confirm_spec`: `open_questions`, `warnings`, `basis_count`, `irreversible_count`
- `merge_pr`: `changed_lines`, `changed_files`, `changed_paths`(문자열 목록), `review_mismatch_count`, `linked_incident_count`, `irreversible_count`

## 되돌릴 수 없는 일 (제안 목록, safety가 정한다)
결제 데이터 삭제·변경, 개인정보 열람·삭제·반출, 실제 송금·정산 연동, 외부 고객 대상 발송. 이 영역에 닿으면 planning이 `irreversible_count`를 1 이상으로 보내고, 허용 경로 밖 변경도 별도로 C가 된다. 목록 확정은 사람 확인 후.

## 3번 (검증자·학습 기준) — 보류
- 이번엔 구현하지 않았다. C 건을 planning이 아닌 검증자가 판단하는 구조(재무 `review_hold` 재사용)와 학습된 기준의 위치는 재무·contracts와 협의가 필요하다.
- 의견: 학습 기준은 `thresholds.toml`을 확장하지 않고 별도 파일로 두는 편이 낫다. 이 파일은 사람만 바꾸는 원칙이라, 자동으로 생기는 기준과 섞이면 SAF-02 경계가 흐려진다. 자동 해제 규칙도 별도 파일에서 처리한다.
- "A·B로 내리는" 자동 하향은 안전 레이어가 스스로 등급을 낮추는 일이라, 사람 승인 없이 가능한지 팀 결정이 먼저 필요하다.
