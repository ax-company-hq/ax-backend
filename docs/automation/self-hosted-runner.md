# 서버 PC self-hosted runner (실서버 실행)

시나리오 A를 실서버(docker compose)로 돌리는 `real-run.yml`이 쓰는 서버 PC runner의 설치·운영·안전 조건이다.
자동화 전체 설계는 `docs/automation-spec.md`가 원본이고, 여기에는 서버 PC에서 해야 할 일만 적는다.

- **runner 등록은 재현이 직접 한다.** 에이전트·봇은 등록하지 않는다.
- 스위치(repo 변수) `AX_REAL_RUN`이 `on`이 아니면 `real-run.yml`의 모든 job이 건너뛰어진다. 코드가 머지돼도 켜기 전에는 아무것도 돌지 않는다.

## 켜기 전 조건 (모두 충족해야 `AX_REAL_RUN=on`)

1. PR-C(`real-run.yml`, 이 문서)가 dev에 머지돼 있다
2. dashboard-api가 총괄·safety를 서버 쪽에서 대신 부르는 경로(#255, #259)가 머지돼 있다
3. 총괄 보류 엔드포인트 보안(#265: `holds`에 사람 토큰 확인이 없는 문제)이 닫혀 있다 (orchestrator 담당 준하). 닫히기 전에는 켜지 않는다
4. 서버 PC에 runner를 설치하고 이 문서의 "서버 PC 준비" 점검을 마쳤다
5. 서버 PC `.env`가 준비돼 있다 (키는 서버 PC에만 둔다)

## 안전 조건 (public 레포)

레포가 public이라 누구나 PR·이슈를 올릴 수 있다. self-hosted runner는 일하는 PC에서 코드를 실행하므로 외부 코드가 서버 PC에 닿지 않게 아래를 지킨다. 4번까지는 코드와 CI가 강제한다.

1. **`pull_request` 계열 트리거 금지.** 서버 PC 워크플로(`real-run.yml`, 이후 `devroom-update.yml`)의 트리거는 `schedule`·`workflow_dispatch`(·`workflow_run`)뿐이다. `pull_request`, `pull_request_target`, `issue_comment`, `issues`, `fork`가 있으면 `real_guard.py check-runner-label`이 `automation-tests`를 실패시킨다.
2. **라벨 고정.** `ax-server` 라벨은 허용 목록(`real-run.yml`, `devroom-update.yml`)의 워크플로만 쓴다. 다른 워크플로가 쓰면 같은 검사가 실패한다. 라벨은 runner 등록 때 `self-hosted,ax-server`로만 단다.
3. **수동 실행은 code owner만.** `real-run.yml`의 첫 job(`guard`, ubuntu)이 실행자를 `CODEOWNERS`의 `/.github/` 소유자(재현·준하·태윤)와 대조한다. 아니면 실패하고 서버 PC job은 시작하지 않는다.
4. **checkout은 항상 `ref: dev`.** 머지된 코드만 서버 PC에서 돈다. 브랜치·fork 코드는 오지 않는다.
5. **fork PR 워크플로 승인 설정.** GitHub 설정 > Actions > General > "Fork pull request workflows from outside collaborators"를 **"Require approval for all outside collaborators"**로 둔다 (self-hosted는 fork PR에서 절대 쓰지 않지만 이중 방어).
6. **runner 그룹.** 가능하면 이 레포 전용(repo-level) runner로만 등록하고 조직 공용 그룹에 넣지 않는다. 조직 runner group을 쓸 수 있는 요금제라면 group의 Selected workflows를 `real-run.yml@refs/heads/dev`로 제한한다 (라벨 검사를 우회한 워크플로도 서버 PC에 못 오게 하는 마지막 방어).
7. **`secrets`를 워크플로에 쓰지 않는다.** 키는 서버 PC `.env`에만 있다. 워크플로는 환경변수 전체를 출력하지 않는다.
8. **수동 실행은 dev ref만, 재실행은 재실행한 사람 기준.** `guard`와 `run` job은 `github.ref == refs/heads/dev`일 때만 돌고, 실행자는 `github.triggering_actor`(재실행한 사람)로 두 번 확인한다. 서버 PC에서 도는 actions는 커밋 SHA로 고정한다.
9. **라벨 우회 차단.** `runs-on`에 `${{ }}` 식을 쓰면 모든 워크플로에서 실패하고, 허용 목록 밖에서 `self-hosted`를 쓰는 것도 실패한다 (`real_guard.py`).

## 서버 PC 준비 (Windows)

- **전용 Windows 계정**을 만든다 (예 `ax-runner`, 일반 사용자, 관리자 아님). runner와 `.env`는 이 계정 폴더에 둔다. 개인 계정·개인 파일과 분리한다.
- **Docker Desktop + WSL2**: WSL2 백엔드로 설치하고 전용 계정이 Docker를 쓸 수 있게 한다 (`docker` 그룹 또는 Docker Desktop 사용자 권한). Docker Desktop은 계정 로그인 시 자동 시작되게 한다.
- 설치된 것: Git for Windows(bash 포함), Python 3.12, `uv`. `docker compose version`, `uv --version`이 전용 계정에서 동작해야 한다.
- 절전·최대 절전 끄기 (전원 옵션: 절전 안 함, 디스플레이만 끄기). 야간 02:00 실행이 깨어 있어야 한다.
- 포트: compose가 쓰는 8000~8107·8010·8020·6379·5432와 게이트웨이(8080)·개발실(8090)이 비어 있어야 한다. Zabbix가 80을 쓸 수 있으므로 게이트웨이는 8080을 유지한다.
- `.env`는 **서버 PC에만** 둔다 (`infra/compose.yml`이 읽는 레포 루트 `.env`). 커밋 금지, 채팅·이슈·로그에 붙이지 않는다. real_run.py는 `.env`에서 `*_TOKEN`과 `AX_ZABBIX_URL` 이름만 읽어 e2e와 Zabbix 확인에 쓰고 값을 출력하지 않는다.
- Zabbix 감지: `.env`에 `AX_ZABBIX_URL`(예 `http://서버PC:포트`)을 넣는다. 첫 실행 전 스크립트가 `apiinfo.version`으로 접속을 확인하고, 안 되면 감지 단계만 목(`AX_REAL_DETECT=mock`)으로 돌리며 결과에 "감지: 목"으로 표시한다.
- 직전 결과(연속 횟수 계산)는 `%USERPROFILE%\.ax-real-run\real-result.json`에 둔다 (`AX_REAL_STATE_DIR`로 변경 가능).

## runner 설치 (재현이 직접)

1. 레포 Settings > Actions > Runners > New self-hosted runner (Windows x64)에서 안내하는 대로 전용 계정 폴더에 내려받는다 (레포 단위).
2. 설정할 때:
   - 라벨: `self-hosted,ax-server` (기본 `windows,x64`는 그대로 붙어도 된다)
   - **`--ephemeral`**: job 하나를 끝내면 runner가 스스로 등록 해제되는 일회용. 이전 job의 파일이 다음 job에 남지 않는다
   - 예: `.\config.cmd --url https://github.com/<조직>/ax-backend --token <등록 토큰> --labels self-hosted,ax-server --ephemeral`
3. `--ephemeral`은 job 하나 뒤 종료되므로, 매 job마다 다시 등록·실행하는 작은 반복 스크립트(`config.cmd` -> `run.cmd`)를 **전용 계정**의 작업 스케줄러(로그인 시 시작)에 둔다. 등록 토큰은 매번 새로 발급해야 하므로 PAT 보관 방식은 재현이 정한다. 이 문서에는 토큰·PAT 값을 적지 않는다.
4. 첫 확인은 `AX_REAL_RUN=on`으로 바꾼 뒤 Actions에서 `real-run`을 수동 실행(`workflow_dispatch`, code owner)해 본다.

## 평소 동작

- 월~금 02:00 KST에 1회 (금요일은 3회 연속), 수동 실행 가능. 각 run: `docker compose -p ax-real -f infra/compose.yml up -d --build` -> 헬스 대기 -> 결제 장애 주입(`infra/fault-injection`) -> `AX_E2E_MODE=real` e2e -> 장애 해제 -> `docker compose -p ax-real ... down -v`.
- **compose 프로젝트는 `ax-real`로 분리한다.** `infra/compose.yml`의 `name: ax-backend`는 서버 PC 상시 스택(데모용 `up -d`)이 쓰므로, 같은 이름이면 `up --build`가 상시 컨테이너를 덮어쓰고 `down -v`가 공통 로그·DB 볼륨을 지운다. `-p ax-real`이면 컨테이너·볼륨이 따로라 `down -v`는 ax-real 것만 지운다 (공통 로그 `common-log`도 이 실행 전용 사본).
- **상시 스택이 떠 있으면 실행하지 않는다.** 시작 전에 `docker compose -p ax-backend ps -q`가 비어 있지 않으면 "상시 스택이 떠 있음, 포트 충돌"을 남기고 실패로 끝낸다 (같은 포트 8000~8107 등을 쓰기 때문). real-run이 도는 시간(02:00 KST 전후)에는 상시 데모를 내려 두거나 포트를 분리한다. 개발실(`-p ax-devroom`)은 별도라 영향이 없다.
- 결과: `real-result` 아티팩트(`real-result.json`, `real-detail.json`). 개발실 갱신(`devroom-update`)이 내려받는다. LLM 호출 수·토큰은 수집 경로가 생기면 `real-detail.json`의 `llm_usage`에 채운다 (지금은 null과 사유).
- mock에서는 통과하는데 실서버에서 실패한 단계는 담당 부서에 `bug`, `from:real-run` 이슈가 생긴다 (같은 단계의 열린 이슈가 있으면 댓글).
- 게이트웨이(`:8080`)가 떠 있고 모든 단계가 통과한 날에는 Playwright로 `/`와 `/admin/`을 오가며 녹화해 `demo-recording-YYYYMMDD` 아티팩트로 90일 보관한다. 게이트웨이가 없으면 건너뛴다.

## 끄는 법 (빠른 순)

1. **스위치**: repo 변수 `AX_REAL_RUN`을 `off`로 (또는 변수 삭제). 다음 실행부터 모든 job이 건너뛰어진다. 진행 중 실행은 Actions에서 취소한다.
2. **workflow 비활성화**: Actions > real-run > Disable workflow.
3. **runner 제거**: 서버 PC에서 `.\config.cmd remove --token <제거 토큰>` 후 작업 스케줄러의 반복 스크립트를 중지한다. 레포 Settings > Runners에서 지워도 된다.
4. **compose 정리**: 실행 중 끊겼다면 서버 PC에서 `docker compose -p ax-real -f infra/compose.yml down -v` (job 끝에도 항상 같은 정리를 한 번 더 한다). 상시 스택(`ax-backend`)은 건드리지 않는다.
5. 침해가 의심되면 runner 제거 -> `.env`의 키 전부 교체 -> 전용 계정 비밀번호 교체 순으로 한다.

## 알려진 제한

- 장애 주입: `infra/fault-injection/payment_fault.py inject|clear` 스크립트가 아직 없다 (infra 요청). 없으면 장애 주입 없이 단독 점수만 돈다.
- 연결(총괄 12단계) 실서버 점수는 아직 pending이다. 지금은 단독 점수만 real로 판정한다.
- Windows에서 `docker compose up --build`는 첫 실행에 오래 걸린다 (이미지 캐시가 쌓이면 줄어든다). 시간 예산: run 하나 최대 70분(up 25 + 헬스 5 + e2e 20 + down 10), job 제한 240분, 다음 run이 220분 안에 못 끝날 것 같으면 시작하지 않는다 (금요일 3회 중 일부가 건너뛰어질 수 있고 결과에 사유가 남는다).
- Gemini(LLM) 사용량 호출 수·토큰은 아직 집계하지 않는다 (#282, agent-template 요청). 집계 경로가 생기면 `real-detail.json`의 `llm_usage`에 채운다.
