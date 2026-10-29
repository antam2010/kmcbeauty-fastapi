# SPEC-INFRA-001 구현 계획 (plan.md)

> 배포 하드닝 · CI/CD · 개발 환경 현대화. 시간 추정 대신 우선순위 기반 마일스톤 순서로 기술한다.

## 마일스톤 순서 및 의존성

```
M1 (Swarm 하드닝, High)
  └─ 무중단 배포의 전제 조건. M3 이전에 완료되어야 함.
M2 (CI, High)
  └─ M1과 병렬 가능. 품질 게이트 선확보.
M3 (CD, High)  ← M1 + M2 완료 후
  └─ 불변 태그(REQ-003)와 CI(M2)에 의존.
M4 (개발 환경, Medium)  ← M2와 도구 공유(ruff/pre-commit), 병렬 가능
M5 (2노드 준비, Low)  ← M3의 레지스트리 배포에 의존, 문서 수준
```

권장 실행 순서: **M1 · M2 병렬 → M3 → M4 → M5**.

---

## M1 — Swarm 하드닝

기술적 접근:

- **환경 분리 (REQ-001)**: 로컬은 `.env`, Swarm은 `.env.prod`(또는 Docker configs/secrets)로 분리한다. `docker-stack.yml`의 `env_file`을 배포 시 `.env.prod`로 지정하고, 그 안의 `REDIS_URL`을 `redis://redis:6379/0`으로 설정한다. `.env.example`에 두 환경의 차이를 주석으로 명시하고 `readme.rst`의 `.env.prod` 참조를 실제 파일과 정합화한다.
- **Firebase 시크릿 (REQ-002)**: `docker-stack.yml`에 `secrets:` 최상위 정의와 서비스별 마운트를 추가한다. 컨테이너 내 시크릿 경로(`/run/secrets/...`)를 `FIREBASE_SERVICE_ACCOUNT_KEY_PATH`가 가리키도록 값을 정렬한다.
- **불변 태그 (REQ-003)**: `docker-stack.yml`의 `image:`를 `${IMAGE_TAG}` 환경 치환으로 바꾸고, 배포 시 `IMAGE_TAG=ghcr.io/<owner>/kmcbeauty-api:<sha>`를 주입한다. `:latest` 하드코딩 제거.
- **HEALTHCHECK 게이팅 (REQ-004)**: 기존 `HEALTHCHECK`와 `update_config: order: start-first`를 유지하되, `update_config`에 `monitor`/`max_failure_ratio`를 명시해 헬스 통과 전 다음 레플리카로 넘어가지 않도록 한다.
- **마이그레이션 단계 (REQ-005)**: 배포 파이프라인에서 스택 업데이트 이전에 `alembic upgrade head`를 매니저 노드(또는 일회성 태스크)에서 실행하는 단계를 정의한다. 마이그레이션 하위 호환 규칙(1 릴리스 윈도우)을 문서화한다.

## M2 — CI (GitHub Actions)

기술적 접근:

- `.github/workflows/ci.yml` 신규 작성: `on: [pull_request, push]`, Python 3.13 setup, pip 캐시, `ruff check` → `ruff format --check` → `pytest`.
- 테스트가 Redis를 요구하면 `services: redis` 컨테이너를 정의하고 `conftest.py`가 참조하는 환경 변수를 주입한다.
- `pyproject.toml`의 ruff `target-version`을 `py313`으로 수정 (REQ-007). 이 변경으로 새 lint 경고가 발생하는지 확인하고 필요 시 별도 정리 작업으로 분리한다.

## M3 — CD (GitHub Actions)

기술적 접근:

- `.github/workflows/cd.yml` 신규 작성: `on: push: branches: [main]`. GHCR 로그인(`GITHUB_TOKEN` 또는 PAT) → `docker build` → `docker push ghcr.io/<owner>/kmcbeauty-api:${{ github.sha }}`.
- 배포 잡: SSH 액션으로 매니저 노드 접속 → `IMAGE_TAG=... docker stack deploy -c docker-stack.yml kmcbeauty` 실행. 배포 전 마이그레이션 단계(REQ-005) 호출.
- (Optional) GitHub `environment: production`에 필수 리뷰어를 설정해 수동 승인 게이트 추가 (REQ-010).
- 롤백 문서(REQ-011): Swarm 자동 롤백 동작 + 수동 `docker service rollback kmcbeauty_kmcbeauty_api` 절차를 배포 문서에 기술한다.

## M4 — 개발 환경 현대화

기술적 접근:

- `docker/compose/compose.dev.yml` 신규 작성(현재 디렉토리 비어 있음): `redis:7-alpine` + (선택) `mysql` 서비스, 로컬 포트 노출. 앱은 로컬 uvicorn으로 실행하거나 compose에 포함.
- `.pre-commit-config.yaml` 추가: ruff lint + format 훅. README에 `pre-commit install` 안내.
- README에 로컬 개발/테스트 실행 절차 정리 (REQ-014).
- (Optional/Low) `uv` 도입 평가: 도입 시 이점/마이그레이션 비용을 짧게 기록하고 채택 여부만 결정 (REQ-015).

## M5 — 2노드 확장 준비 (문서 수준)

기술적 접근:

- 워커 노드 조인 절차(`docker swarm join-token worker`) 문서화 (REQ-016).
- `celery_beat`/`redis` 단일 인스턴스 서비스에 대한 `placement.constraints`(예: 특정 노드 라벨 고정) 또는 redis 외부화 옵션 문서화 (REQ-017).
- 2노드에서의 NGINX 업스트림/오버레이 라우팅 고려사항 문서화 (REQ-018).

---

## 리스크 및 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| ruff `py313` 전환으로 다량의 신규 lint 경고 발생 | CI 레드 | 경고 정리를 M2에서 별도 커밋으로 분리, 필요 시 규칙 일시 완화 |
| 마이그레이션이 하위 호환을 깨뜨림(컬럼 삭제/rename) | 롤링 중 구버전 레플리카 오류 → 다운타임 | REQ-005 하위 호환 규칙(expand/contract 2단계) 강제, acceptance에서 검증 |
| SSH 배포 키 유출 | 매니저 노드 침해 | GitHub 저장소/환경 시크릿 사용, 최소 권한 배포 계정, 키 회전 문서화 |
| GHCR 이미지 가시성/권한 오설정 | 노드에서 pull 실패 | 매니저 노드에 GHCR registry 로그인 사전 구성, 배포 스모크 테스트 |
| 단일 `.env` 재도입으로 결함 재발 | Swarm에서 Redis/FCM 재고장 | `.env.prod` 분리 + `.env`를 배포에서 사용 금지, README 명시 |
| 로컬 MySQL 컨테이너를 운영에 오용 | 운영 DB 컨테이너화(범위 밖) | compose.dev.yml에 "dev only" 명시, docker-stack.yml에는 MySQL 미포함 유지 |

## 전문가 상담 권장 (Conditional)

- **expert-devops**: Swarm 시크릿/placement, GitHub Actions CD의 SSH 배포·GHCR 인증 설계 검토 (M1, M3, M5).
- **expert-backend**: Alembic expand/contract 마이그레이션의 하위 호환 전략 검토 (REQ-005).
