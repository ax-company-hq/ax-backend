# infra

담당: 준하 · 포트: 없음 (전체 compose와 장애 주입)

## 실행 방법

레포 루트에서 실행한다. 먼저 `.env.example`을 `.env`로 복사해 값을 채운다 (`.env`가 없어도 compose는 뜬다).

```bash
cp .env.example .env
docker compose -f infra/compose.yml up              # 전체
docker compose -f infra/compose.yml up redis postgres data   # 필요한 것만
docker compose -f infra/compose.yml config          # 설정 검사
```

- 각 서버 패키지의 `compose.yml`을 `infra/compose.yml`이 `include:`로 불러온다. 서비스 이름은 폴더 이름이다.
- 공용 서비스: `redis`(이벤트 버스, Redis Streams), `postgres`(pgvector, memory·공통 로그·각 서비스 DB, 볼륨 `pgdata`).
- Postgres 기본 계정은 `ax` / `ax` / DB `ax`. compose 안에서의 주소는 `postgresql://ax:ax@postgres:5432/ax`, Redis는 `redis://redis:6379/0`.
- 목 모드: 서비스별 `<부서>_MOCK=1` (예: `DATA_MOCK=1`).

## 포트

| 서비스 | 포트 |
| --- | --- |
| orchestrator | 8000 |
| safety | 8001 |
| memory | 8002 |
| shop-api | 8010 |
| dashboard-api | 8020 |
| agents/planning | 8101 |
| agents/support | 8102 |
| agents/marketing | 8103 |
| agents/hr | 8104 |
| agents/finance | 8105 |
| agents/data | 8106 |
| agents/infra-dejafix | 8107 |
| redis (이벤트 버스) | 6379 |
| postgres | 5432 |
| ax-frontend shop-web / dashboard-web | 3000 / 3001 |

새 서비스를 추가하면 루트 `CLAUDE.md`의 포트 표부터 갱신하고 이 표도 맞춘다.

## 프론트까지 띄우기

두 레포를 같은 폴더에 나란히 clone한다.

```
ax-company-hq/
├─ ax-backend/
└─ ax-frontend/
```

```bash
# 백엔드
cd ax-backend && docker compose -f infra/compose.yml up
# 프론트 (다른 터미널)
cd ../ax-frontend && npm install && npm run dev
```

프론트 `.env`:

```
VITE_SHOP_API_URL=http://localhost:8010
VITE_DASHBOARD_API_URL=http://localhost:8020
```

## 장애 주입

`fault-injection/` 참조.
