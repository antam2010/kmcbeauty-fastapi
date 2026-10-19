# SPEC-INFRA-001 태스크 분해 (tasks.md)

> Phase 1(분석·계획) 산출물. 실행 순서: **M1 · M2 병렬 → M3 → M4 → M5**.
> 인프라 YAML/문서 중심이므로 대부분 정적 검증(파싱/grep/lint)으로 확인하고,
> 실제 Swarm/GitHub 환경이 필요한 항목(AC-1·AC-5 등)은 "런타임 검증 유예(문서화된 절차 필수)"로 표기한다.

## Task Decomposition
SPEC: SPEC-INFRA-001

| Task ID | Description | Requirement | Dependencies | Planned Files | Status |
|---------|-------------|-------------|--------------|---------------|--------|
| T-001 | 환경 분리: `.env.prod` 도입, `docker-stack.yml`의 배포용 `env_file`을 `.env.prod`로 정합화, 오버레이 Redis DNS(`redis://redis:6379/0`) 사용, `.env.example` 주석·`readme.rst` 참조 정합 | REQ-INFRA-001 | 없음 | `.env.prod.example`, `docker-stack.yml`, `readme.rst` | done |
| T-002 | Firebase Docker secret: `docker-stack.yml`에 최상위 `secrets:` 정의 + 서비스 마운트, `FIREBASE_SERVICE_ACCOUNT_KEY_PATH`를 `/run/secrets/...`로 정렬 | REQ-INFRA-002 | T-001 | `docker-stack.yml`, `docs/deployment.md` | done |
| T-003 | 불변 이미지 태그 + 롤링 게이팅: `image:`를 `${IMAGE_TAG}` 치환으로 교체, `:latest` 제거(스크립트 포함), `update_config`에 `monitor`/`max_failure_ratio` 추가하고 `order: start-first` 유지 | REQ-INFRA-003, REQ-INFRA-004 | T-002 | `docker-stack.yml`, `scripts/start_swarm.sh`, `scripts/start_stage.sh`, `readme.rst`, `tests/test_infra_guards.py` | done |
| T-004 | 배포 시 Alembic 마이그레이션 단계 정의 + expand/contract(하위 호환 1 릴리스 윈도우) 규칙 문서화 | REQ-INFRA-005 | 없음 | `scripts/deploy_migrate.sh`, `docs/deployment.md` | done |
| T-005 | ruff `target-version = "py313"` 정합 + 신규 lint 경고 정리 여부 판단 | REQ-INFRA-007 | 없음 | `pyproject.toml`, `app/database.py`(UP043 trivial) | done |
| T-006 | CI 워크플로우: PR/push 시 `ruff check` → `ruff format --check` → `pytest`(Python 3.13, pip 캐시). 현재 테스트는 Redis/DB를 모두 모킹하므로 Redis 서비스 컨테이너 불필요 — 근거를 주석/문서로 명시 | REQ-INFRA-006 | T-005 | `.github/workflows/ci.yml`, `requirements.txt`(pytest-cov, coverage) | done |
| T-007 | CD 워크플로우: `main` 병합 시 GHCR 빌드·푸시(`ghcr.io/antam2010/kmcbeauty-api:<sha>`), SSH로 매니저 노드 `docker stack deploy`(IMAGE_TAG 주입), 배포 전 마이그레이션 단계 호출, (Optional) protected environment 수동 승인 | REQ-INFRA-008, REQ-INFRA-009, REQ-INFRA-010 | T-003, T-004, T-006 | `.github/workflows/cd.yml`, `docs/deployment.md` | done |
| T-008 | 롤백 절차 문서화: Swarm 자동 롤백(`failure_action: rollback`) 동작 + 수동 `docker service rollback` 절차 | REQ-INFRA-011 | T-007 | `docs/deployment.md` | done |
| T-009 | 개발 환경 현대화: `docker/compose/compose.dev.yml`(redis + 선택 mysql), `.pre-commit-config.yaml`(ruff lint+format), README 로컬 개발/테스트 절차, (Optional) `uv` 도입 평가 기록 | REQ-INFRA-012, REQ-INFRA-013, REQ-INFRA-014, REQ-INFRA-015 | T-005 | `docker/compose/compose.dev.yml`, `readme.rst`, `docs/deployment.md`(uv); `.pre-commit-config.yaml` DEFERRED(harness block) | partial |
| T-010 | 2노드 확장 준비(문서 수준): 워커 노드 조인 절차, `celery_beat`/`redis` placement 제약 또는 redis 외부화, NGINX 업스트림/오버레이 라우팅 고려사항 | REQ-INFRA-016, REQ-INFRA-017, REQ-INFRA-018 | T-007 | `docs/scaling.md`, `readme.rst` | done |

## 실행 순서 그래프

```
[M1 트랙]  T-001 → T-002 → T-003
                              ↘
           T-004 ──────────────→ T-007 → T-008
[M2 트랙]  T-005 → T-006 ──────↗       ↘
                    ↘                    T-010
[M4]                 T-009
```

- M1 트랙(T-001·T-002·T-003)은 `docker-stack.yml` 단일 파일을 공유하므로 순차 실행(쓰기 충돌 방지).
- T-004(M1, 마이그레이션 문서)와 M2 트랙(T-005→T-006)은 M1과 파일이 겹치지 않아 병렬 가능.
- T-007(CD)은 불변 태그(T-003)·마이그레이션 단계(T-004)·CI 그린(T-006)을 전제로 함.
- T-009(개발 환경)는 ruff 규칙을 pre-commit과 공유하므로 T-005 이후.
