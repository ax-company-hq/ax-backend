# #197 pr_allowed_paths에 shop-web/ 추가

요청(planning): 화면 문구 같은 간단 요청이 `shop-web/` 변경이라 `generate_pr`·`merge_pr`이 모두 C로 보류된다.

결정: `pr_allowed_paths`(generate_pr)와 `merge_allowed_paths`(merge_pr)를 `["shop-api/", "shop-web/", "docs/"]`로 바꾼다. 줄·파일 상한(300·20)은 그대로. 값은 제안값.

- 결제·개인정보 등 위험 경로는 허용 경로와 별개로 `merge_irreversible`(`irreversible_count > 0`이면 항상 C)이 막는다. planning이 `shop-web/checkout/` 같은 경로를 되돌릴 수 없는 영역으로 셀지는 planning이 정한다.
- 별도 "보호 경로" 규칙(#170)은 이번에 만들지 않았다. 필요하면 후속 이슈로 논의한다.
- shop-web 담당(@areatmadm) 의견은 이슈에서 받는다. 반대 의견이 있으면 이 값을 되돌린다.
