# 서버 PC 접속 안내 (홈페이지·관리자)

서버 PC에서는 서버 전용 override `infra/compose.server.yml`을 함께 써서 nginx 게이트웨이 하나가 한 포트(기본 8080)로 고객 홈페이지·관리자·API를 서빙한다.
이때 외부에 열리는 포트는 이것 하나뿐이고, 나머지 서비스(총괄 8000, safety 8001, 각 에이전트 등)는 compose 내부 네트워크에서만 열린다.

기본 `infra/compose.yml`(팀원 로컬 개발)의 동작은 바뀌지 않는다. 로컬에서는 지금처럼 `docker compose -f infra/compose.yml up`으로 각 서비스 포트가 호스트에 열린다.

`infra/compose.server.yml`은 infra 요청 #296이 머지된 뒤에 생긴다. 그 전에는 아래 서버 PC 명령이 동작하지 않는다.

> **보안 경고 (먼저 읽기).** 게이트웨이의 `/api/`는 dashboard-api를 그대로 연결한다. dashboard-api의 쓰기 API(클레임 승인·거절, 보상 큐 처리, 문의 답변·상태 변경, 후보 카드 결정 등)에는 아직 인증이 없다.
> 따라서 **같은 LAN이나 Tailscale에 들어온 사람은 누구나 `/api/dashboard/...`로 직접 POST할 수 있다.** `/admin/`의 데모 로그인은 화면 입구만 막을 뿐 API는 막지 않는다.
> **외부(멘토·발표) 공개는 Cloudflare Access 같은 인증이 앞에 없으면 하지 않는다.** dashboard-api 쓰기 API에 사람 토큰 확인을 붙이는 작업은 별도 요청(`to:dashboard-api`)으로 넘겼다.

| 주소 | 화면 | 담당 |
| --- | --- | --- |
| `/` | 캣북스 홈페이지 (shop-web) | 재헌 |
| `/admin/` | 관리자: 회사 전체 · 승인 대기열 · 소통 · 부서 화면 (dashboard-web) | 병환 + 각 부서 담당 |
| `/admin/planning/` | 기획개발 (planning-web) | 예진 |
| `/api/` | dashboard-api (8020) 하나만. 브라우저는 총괄·safety·재무 에이전트를 직접 부르지 않는다 | 병환 |
| `/shop-api/` | shop-api (8010). 고객 화면용 예외 | 재헌 |

`/admin/`의 데모 로그인은 고객이 실수로 들어오지 않게 하는 정도이며 실제 인증이 아니다 (비밀번호가 화면 코드에 들어 있다).

## 서버 PC에서 켜기

1. `git pull` (ax-backend와 ax-frontend를 같은 폴더에 나란히 둔다)
2. `.env` 생성 (`.env.example` 복사 후 값 채우기. 비밀값은 커밋 금지). 게이트웨이 포트는 `AX_GATEWAY_PORT` (기본 8080)
3. `docker compose --env-file .env -f infra/compose.yml -f infra/compose.server.yml up -d --build` (compose는 `infra/.env`를 읽으므로 루트 `.env`는 `--env-file`로 넘긴다)
4. 확인: `http://localhost:8080/`, `/admin/`, `/admin/planning/`

`compose.server.yml`이 모든 서비스에 `restart: unless-stopped`를 붙이므로 서버 PC를 다시 켜면 Docker가 올라오는 대로 따라 올라온다. 내릴 때도 같은 두 파일로 `down`(`-v` 없이)을 쓴다.

## 방화벽 (Windows, 관리자 PowerShell)

```powershell
New-NetFirewallRule -DisplayName "AX gateway" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

`AX_GATEWAY_PORT`를 바꿨으면 그 포트로 연다. 다른 포트는 열지 않는다.

## 접속

| 어디서 | 주소 |
| --- | --- |
| 학원 (같은 네트워크) | 홈페이지 `http://서버PC_IP:8080/`, 관리자 `http://서버PC_IP:8080/admin/` |
| 집 (Tailscale) | `http://ax-server:8080/admin/` (무료 Personal 플랜은 6명까지) |

## 외부 접속 (멘토·발표)

Tailscale 6명이 꽉 차므로 외부 사람은 Cloudflare Tunnel + Cloudflare Access로 연다.

1. Cloudflare Zero Trust에서 Tunnel을 만들고 서버 PC에 `cloudflared`를 설치한다
2. Public hostname의 service를 `http://localhost:8080`으로 한다 (게이트웨이 하나만 연결)
3. 같은 hostname에 Access 정책(허용 이메일 목록)을 건다. **정책 없이 공개하지 않는다** (위 보안 경고)
4. 발표가 끝나면 Tunnel을 끈다

## 주의

- **`docker compose down -v` 금지.** named volume(`common-log`, `pgdata` 등)이 지워져 공통 로그와 사건 기록이 사라진다. 내릴 때는 `down`만 쓴다
- 서버 PC 절전·최대 절전을 끈다
- Zabbix가 80을 쓸 수 있어 게이트웨이는 8080을 유지한다
- 재무 화면이 safety·재무 에이전트를 부르는 호출은 dashboard-api 경유 경로(ax-backend #255, #259)가 머지돼야 서버 배포에서 동작한다. 그 전에는 해당 호출이 실패한다
- AX 개발실은 같은 서버 PC의 별도 compose 프로젝트(`:8090`)이며, 제품을 내려도 유지된다
