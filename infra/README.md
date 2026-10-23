# infra

담당: 준하 · 포트: 없음 (전체 compose와 장애 주입)

## 실행 방법

레포 루트에서 실행한다. 먼저 루트 `.env.example`(공통 변수)을 `.env`로, 필요한 서비스 폴더의 `.env.example`(부서 변수)을 그 폴더의 `.env`로 복사해 값을 채운다. 각 서비스는 [루트 `.env`, 자기 폴더 `.env`] 순서로 읽고, 둘 다 없어도 compose는 뜬다 (Docker Compose 2.24 이상 필요).

```bash
cp .env.example .env
cp agents/data/.env.example agents/data/.env   # 예: data를 띄울 때
docker compose -f infra/compose.yml up              # 전체
docker compose -f infra/compose.yml up redis postgres data   # 필요한 것만
docker compose -f infra/compose.yml config          # 설정 검사
```

- 각 서버 패키지의 `compose.yml`을 `infra/compose.yml`이 `include:`로 불러온다. 서비스 이름은 폴더 이름이다.
- 공용 서비스: `redis`(이벤트 버스, Redis Streams), `postgres`(pgvector, memory·공통 로그·각 서비스 DB, 볼륨 `pgdata`).
- Postgres 기본 계정은 `ax` / `ax` / DB `ax`. 바꾸려면 `infra/.env`에 `COMPOSE_POSTGRES_*`를 적는다. compose 안에서의 주소는 `postgresql://ax:ax@postgres:5432/ax`, Redis는 `redis://redis:6379/0`.
- 목 모드: 서비스별 `<부서>_MOCK=1` (예: `DATA_MOCK=1`).
- 공통 로그(COM-08, #249): 모든 서비스에 `AX_COMMON_LOG_DB=/common-log/common_log.db`와 named volume `common-log`를 붙인다. 붙이는 파일은 `infra/common-log/<서비스>.yml`이고 `include`의 `path` 목록으로 각 서비스 compose 뒤에 합친다. SQLite WAL을 여러 컨테이너가 함께 쓰므로 Windows bind mount와 `:ro`(dashboard-api 포함)는 쓰지 않는다. `down -v`를 하면 공통 로그가 지워진다. 새 서비스를 추가하면 `common-log/<서비스>.yml`도 만든다.

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
