# SPEC-INFRA-001 인수 기준 (acceptance.md)

> 모든 기준은 객관적으로 검증 가능해야 한다(명령 출력, 파일 존재, 폴링 결과, 종료 코드).

## Given-When-Then 시나리오

### AC-1 — 무중단 재배포 (REQ-INFRA-003, 004, 005 / 핵심)

- **Given** Swarm 스택이 SHA 태그 이미지로 정상 기동되어 있고, 외부에서 `/health`를 1초 간격으로 폴링 중이다.
- **When** 새로운 커밋 SHA 태그로 스택을 연속 2회 재배포한다(`docker stack deploy`).
- **Then** 폴링된 `/health` 요청 중 실패(비 200 또는 연결 실패)가 **0건**이어야 한다. 최종적으로 모든 레플리카가 새 SHA 이미지 다이제스트로 교체되어 있어야 한다(`docker service inspect`로 확인).

### AC-2 — Swarm 오버레이에서 Redis 연결 (REQ-INFRA-001)

- **Given** Swarm 배포 설정(`.env.prod` 또는 Docker config)이 적용된 스택.
- **When** API/worker 컨테이너가 기동되어 Redis에 연결을 시도한다.
- **Then** 연결이 성공하며(레이트리밋/캐시/Celery 브로커 정상), 컨테이너 로그와 설정에서 Redis 엔드포인트가 `redis:6379`(서비스 DNS)로 해석되어야 한다. `localhost:6379` 참조가 배포 환경에 존재하지 않아야 한다.

### AC-3 — Firebase 시크릿 마운트 (REQ-INFRA-002)

- **Given** `docker-stack.yml`에 Firebase Docker secret이 정의·마운트된 스택.
- **When** API 컨테이너 내부에서 `FIREBASE_SERVICE_ACCOUNT_KEY_PATH` 경로를 확인한다.
- **Then** 해당 경로에 읽기 가능한 자격 증명 파일이 존재하고, FCM 초기화가 오류 없이 완료되어야 한다(앱 기동 로그에 Firebase 초기화 실패 없음).

### AC-4 — CI가 PR에서 게이트 역할 (REQ-INFRA-006, 007)

- **Given** GitHub Actions CI 워크플로우가 저장소에 존재한다.
- **When** PR을 생성하거나 커밋을 푸시한다.
- **Then** `ruff check` + `ruff format --check` + `pytest`가 Python **3.13**에서 실행되고, 하나라도 실패하면 워크플로우가 실패(레드)로 표시되어야 한다. `pyproject.toml`의 `target-version`이 `py313`이어야 한다.

### AC-5 — main 병합 시 GHCR 푸시 및 배포 (REQ-INFRA-008, 009)

- **Given** CD 워크플로우가 존재하고 매니저 노드 SSH/GHCR 시크릿이 구성되어 있다.
- **When** `main`에 병합이 발생한다.
- **Then** `ghcr.io/<owner>/kmcbeauty-api:<sha>` 이미지가 GHCR에 게시되고, 매니저 노드에서 해당 SHA 태그로 `docker stack deploy`가 실행되어 서비스가 새 이미지로 갱신되어야 한다.

### AC-6 — 배포 실패 시 자동 롤백 (REQ-INFRA-011)

- **Given** 헬스체크를 통과하지 못하는(의도적으로 손상된) 이미지를 배포한다.
- **When** 롤링 업데이트가 진행된다.
- **Then** Swarm이 `failure_action: rollback`에 따라 자동으로 직전 정상 태스크로 롤백하고, `/health` 폴링에서 지속적 다운타임이 발생하지 않아야 한다. 수동 롤백 절차가 문서에 기재되어 있어야 한다.

### AC-7 — 로컬 개발 환경 (REQ-INFRA-012, 013, 014)

- **Given** 신규 개발자가 저장소를 클론한다.
- **When** README의 절차대로 `docker compose -f docker/compose/compose.dev.yml up`과 `pre-commit install`을 수행한다.
- **Then** Redis(및 선택 MySQL)가 기동되어 로컬에서 앱/테스트가 동작하고, 커밋 시 pre-commit ruff 훅이 실행되어야 한다.

---

## 엣지 케이스

- **마이그레이션 하위 호환 위반**: 컬럼 삭제/rename을 단일 릴리스에 포함하면 롤링 중 구버전 레플리카가 실패한다 → REQ-005 expand/contract 2단계 강제. 배포 리허설에서 구·신 스키마 동시 동작을 확인한다.
- **latest 태그 잔존**: `docker-stack.yml` 또는 스크립트에 `:latest`가 남아 있으면 실패 처리(grep 검증).
- **`.env` 배포 오용**: 배포 경로에서 로컬 `.env`가 사용되면 REDIS_URL 결함 재발 → 배포 스크립트/워크플로우가 `.env.prod`만 참조하는지 검증.
- **GHCR pull 권한 누락**: 매니저 노드가 이미지를 pull하지 못하는 경우 배포가 즉시 실패해야 하며(무한 재시도 금지), 로그에 원인이 남아야 한다.
- **celery_beat 다중화**: 2노드 확장 시 beat가 2개로 뜨면 스케줄 중복 실행 → placement 제약(REQ-017)으로 단일 인스턴스 보장.

## 품질 게이트 기준 (TRUST 5)

- **Tested**: 기존 특성화/보안/성능 테스트(13개 파일)가 CI에서 통과. AC-1의 무중단 폴링 검증을 배포 리허설 절차로 수행.
- **Readable**: 워크플로우/compose/스택 파일에 목적 주석. README 개발 워크플로우 문서화.
- **Unified**: ruff lint + format가 CI와 pre-commit에서 동일 규칙(`py313`)으로 강제.
- **Secured**: 시크릿은 Docker secret / GitHub 시크릿으로만 주입. 저장소에 자격 증명 평문 커밋 금지.
- **Trackable**: 모든 변경은 SPEC-INFRA-001 및 REQ-ID를 커밋/PR에 참조.

## Definition of Done

- [ ] REQ-INFRA-001~005 반영: Swarm 배포가 무중단(AC-1), Redis(AC-2)·FCM(AC-3) 정상.
- [ ] REQ-INFRA-006~007 반영: CI 워크플로우 존재, py313 정합, PR 게이트 동작(AC-4).
- [ ] REQ-INFRA-008~011 반영: main 병합 시 GHCR 푸시 + 배포(AC-5), 자동/수동 롤백(AC-6).
- [ ] REQ-INFRA-012~014 반영: 로컬 compose + pre-commit + README(AC-7).
- [ ] REQ-INFRA-016~018 문서화: 노드 조인 · placement · NGINX 업스트림.
- [ ] Exclusions 준수: K8s/관리형 컨테이너/운영 DB 컨테이너화/신규 APM 미도입.
- [ ] `docker-stack.yml`·스크립트에 `:latest` 하드코딩 부재(grep 0건).
