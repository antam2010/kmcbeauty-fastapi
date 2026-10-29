# KMCBeauty — 제품 개요

> 뷰티샵 예약·시술 관리를 위한 멀티샵 SaaS API 백엔드

---

## 제품 목적

KMCBeauty는 뷰티샵(피부관리, 네일, 헤어 등) 사업자가 여러 지점을 단일 플랫폼에서 운영할 수 있도록 설계된 **샵 범위 멀티테넌시** API 서버다. 각 샵은 독립된 멤버 체계를 갖추고, 시술 예약부터 결제·완료 처리까지 전체 라이프사이클을 관리한다.

---

## 핵심 도메인

### 멀티테넌시 구조

| 계층 | 설명 |
|------|------|
| Shop | 최상위 테넌트 단위. 모든 리소스는 Shop에 귀속됨 |
| ShopUser | Shop 멤버십(대표원장 여부 포함). 샵 데이터 접근의 기준 |
| 역할 | MASTER(원장 — 샵 생성 가능) / MANAGER(초대 코드로 가입한 직원) |

- 멤버(MASTER·MANAGER 무관)는 예약·고객·시술 메뉴·대시보드를 함께 사용한다.
- 샵 정보 수정은 생성자만, 초대 코드·직원 관리는 대표원장만 할 수 있다.
- 멤버십이 해제되면 다음 요청부터 해당 샵에 접근할 수 없다.
- 초대 코드는 1회용이다 — 첫 가입이 소비하므로 직원마다 새로 발급한다 (만료 60초~30일, 기본 7일, JSON 본문 `expire_in` 초 단위).
- 탈퇴(소프트삭제)한 사용자의 멤버십 행은 이력으로 남지만, 멤버 판정·담당자 지정·푸시 대상·직원 목록에서 제외된다.

### 시술 라이프사이클

```
RESERVED ──(직원 방문 처리)──> VISITED ──(시술 종료 후 자동)──> COMPLETED
    │                                                          ▲
    ├──> NO_SHOW   (직원 처리)                                  │ 직원이 직접 완료 처리도 가능
    └──> CANCELLED (예약 취소)
```

- **RESERVED**: 예약됨
- **VISITED**: 방문 처리됨(직원). 시술 종료 시각이 지나면 Celery beat(30분 주기)가 COMPLETED 로 자동 전환
- **COMPLETED**: 시술 완료. 결제 수단이 카드·현금이면 실매출로 집계
- **NO_SHOW**: 노쇼 (직원 처리)
- **CANCELLED**: 예약 취소 (예약은 소프트삭제 대신 상태로 취소)

정책: 처리되지 않은 RESERVED 는 종료 시각이 지나도 자동 완료하지 않는다. 노쇼가 실매출(결제 수단 기본값 CARD)로 잡히는 것을 막기 위해서이며, 직원이 방문/노쇼를 고른다.

### 주요 기능

| 기능 | 설명 |
|------|------|
| 인증 | OAuth2 Password Flow + JWT 액세스/리프레시 토큰 (리프레시 회전·재사용 차단, 토큰 타입 검증). 비밀번호는 가입·변경 시 8자 이상 |
| 시술 메뉴 관리 | 메뉴 및 세부 항목(treatment_menu, treatment_menu_detail) |
| 고객 전화번호부 | 샵별 고객 연락처 관리(phonebook), 그룹별 집계 (그룹 없음은 null 그룹) |
| 결제 수단 | 시술 건별 결제 방식 기록 (CARD / CASH / UNPAID) |
| FCM 푸시 알림 | firebase-admin 연동, 디바이스 토큰 등록 및 같은 샵 멤버 대상 발송 |
| 대시보드 요약 | 샵별 일·월 통계·현황 요약 (Redis 캐시, 예약 변경 시 무효화) |
| 소프트 삭제 | `SoftDeleteMixin` 기반 논리 삭제 (전화번호부·샵·메뉴·유저). 계정 강제 탈퇴는 하드 삭제(DB ON DELETE 연쇄) |
| 속도 제한 | slowapi (운영 저장소 Redis), IP 기준 분당 — 로그인 5 · 가입 5 · 이메일 확인 20 · 토큰 재발급 30 · 초대 생성 10 · 푸시 발송 10 |
| 입력 검증 | 요청 길이·범위를 DB 컬럼에 맞춤(초과 시 422), 날짜 필터 1900~2100 · 페이지 ≤ 100000, DB 값 오류(DataError)는 400 |
| API 문서 | `/docs` · `/redoc` · `/openapi.json` · `/changelog` — `APP_ENV=production` 에서는 비공개 |

---

## API 라우터 목록

| 라우터 모듈 | 경로 접두사 | 주요 역할 |
|------------|-----------|---------|
| `auth` | `/auth` | 로그인, 토큰 재발급(refresh), 로그아웃 |
| `user` | `/users` | 가입, 내 정보 조회·수정·탈퇴, 이메일 중복 확인 |
| `shop` | `/shops` | 샵 생성·조회·수정, 샵 선택, 초대 코드, 샵 멤버 관리 |
| `phonebook` | `/phonebooks` | 고객 연락처 CRUD, 그룹별 집계, 전화번호 중복 확인 |
| `treatment` | `/treatments` | 시술 예약 CRUD·상태 변경 |
| `treatment_menu` | `/treatment-menus` | 시술 메뉴 + 세부 항목(`/{menu_id}/details`) 관리 |
| `device_push_token` | `/device-tokens` | FCM 토큰 등록·관리, 푸시 발송 |
| `summary` | `/summary` | 대시보드 요약 통계 (`/summary/dashboard`) |
| `api_change` (app/docs) | `/changelog` | API 변경 이력 |

---

## 비기능 요구사항

| 항목 | 현황 |
|------|------|
| 가용성 | 단일 서버 docker compose(컨테이너 1개씩, `restart: unless-stopped`, 배포 시 헬스체크 대기). 배포 중 api 가 잠깐 중단되고 자동 롤백은 없다(deploy.sh 가 롤백 명령 안내) |
| 성능 | uvloop + httptools + orjson 적용, Redis 캐싱 |
| 모니터링 | Sentry SDK (traces_sample_rate 0.2, PII 자동 첨부·지역변수 전송 차단, 리프레시 토큰 키 스크럽, 로컬/디버그 비활성) |
| 보안 | argon2 기본 해시 + bcrypt 레거시 지원, Fernet 대칭 암호화, 토큰 타입 검증, 테넌트 격리(멤버십·교차 참조 검증), 민감 API 요청 횟수 제한, 1회용 초대 코드 |
| 데이터 안전성 | 소프트 삭제로 실수 삭제 방지, 세션 타임존 UTC 고정 |

---

## 알려진 제약사항

- 앱(kmcbeauty-native) 예약 수정 화면에 '방문함(VISITED)' 선택지가 없다. 앱에 추가하기 전까지는 자동 완료가 사실상 동작하지 않으며, 직원이 '완료'를 직접 처리해야 실매출에 잡힌다.
- 앱의 비밀번호 검사가 아직 4자 기준이다(`profile.tsx`, `InviteSignupForm.tsx`). 8자로 맞추기 전까지 4~7자 입력은 서버에서 422 로 거절된다.
- 앱은 초대 코드 조회 404(사용됨·없음)를 "활성 코드 없음"으로 처리해야 한다.
- `GET /summary/dashboard` 를 `target_date` 없이 호출하면 422 (앱은 항상 전달).
- 운영 서버(server-mini2 관리)에는 아직 배포 전이다. 첫 배포(docker compose) 절차는 `docs/deployment.md` 10절을 따른다.
- FK `ON DELETE` 보강 후보: `shop.user_id`, `treatment.phonebook_id`
- 마지막 대표원장을 보호하는 장치가 없다 (정책 결정 필요).
- 테스트는 DB 를 모킹하며 CI 에 PostgreSQL 이 없다. PostgreSQL 호환은 로컬 실DB 스모크로 확인한다.

> SPEC-INFRA-001(2026-07-07)으로 해소된 이슈: REDIS_URL localhost 결함, Firebase 시크릿 마운트 누락, `.env.prod` 부재
> SPEC-INFRA-002(2026-07-07)으로 해소된 이슈: `.env.prod` 평문 시크릿 집중, os.getenv 산개(단일 진실원천 부재), GitHub 시크릿 수기 관리, Docker secret 회전 절차 부재
> 2026-07 해소: 테스트 실패 5건(fastapi-pagination·pydantic 직렬화), ruff lint 부채(1,146건 → 0건)
> 2026-10 해소:
> - MySQL→PostgreSQL 전환 (드라이버·베이스라인·GROUP BY·세션 TZ·이메일 대소문자)
> - 보안: 교차 테넌트 참조·메뉴 상세·푸시 인가 결함, JWT 타입 미검증·탈퇴 세션 잔존·Sentry PII
> - 배포: 스크립트 실행 비트, CI 게이트, 배포 직렬화, Redis 망 격리, 프록시 신뢰
> - 기능: 매니저 샵 접근, 대시보드 캐시 충돌, 집계 단위, 검색 페이지, 자동완료 누락·노쇼 정책, 강제 탈퇴 500
> - 보안 후속: 탈퇴 사용자 제외, /refresh 타입 검증, 세션 정리 순서, 오류·로그 노출, 요청 횟수 제한, 초대 코드 1회용, 운영 문서 비공개, Sentry 지역변수
> - 입력 견고성: 길이·범위 422, 경계 입력 4xx, 응답 시각 UTC 표기, 비밀번호 8자, 초대 만료 본문 수신, DataError 400
