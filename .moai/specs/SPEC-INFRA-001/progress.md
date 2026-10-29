# SPEC-INFRA-001 Progress

- Started: 2026-07-06
- Mode: TDD (sub-agent), harness=standard
- Phase 0.9 complete: detected python (pyproject.toml) → moai-lang-python 스킬 컨텍스트
- Phase 0.95 complete: Scale-based mode = Full Pipeline 후보 (files ≥ 10, domains: infra/ci/docs) → Standard 하니스로 실행

## Phase 2 complete (2026-07-06)

### 생성 파일 (files_created)
- `tests/test_infra_guards.py` (T-000, RED→GREEN 가드: `:latest` 부재 검증)
- `.env.prod.example` (T-001, 배포 env 템플릿)
- `scripts/deploy_migrate.sh` (T-004, 배포 전 alembic upgrade)
- `.github/workflows/ci.yml` (T-006)
- `.github/workflows/cd.yml` (T-007)
- `docker/compose/compose.dev.yml` (T-009)
- `docs/deployment.md` (T-002/004/007/008/009-uv, 배포·시크릿·마이그레이션·롤백·CI/CD·uv 평가)
- `docs/scaling.md` (T-010, 2노드 확장)

### 수정 파일 (files_modified)
- `docker-stack.yml` (불변 IMAGE_TAG, secrets 블록+마운트, api healthcheck, update_config monitor/max_failure_ratio, env_file→.env.prod)
- `scripts/start_swarm.sh`, `scripts/start_stage.sh` (:latest 제거, IMAGE_TAG 주입, .env.prod 가드)
- `pyproject.toml` (ruff target-version py312→py313)
- `app/database.py` (UP043 trivial: Generator[Session])
- `requirements.txt` (pytest-cov==6.1.1, coverage==7.6.10 추가)
- `readme.rst` (배포/로컬 개발 워크플로우/2노드 참조 정합, isort·black→ruff)
- `tasks.md` (Status 갱신)

### 해소된 DEFERRED
- `.pre-commit-config.yaml`: 서브에이전트 샌드박스 차단 → 오케스트레이터가 직접 생성 완료 (done).

## Phase 2.8a evaluator-active (2026-07-06)

- 1차 판정: FAIL (Security HARD — `.env.prod` gitignore 누락)
- 수정 사이클 1: `.gitignore`(+.env.prod/.env.stage/.env.dev), ci.yml(permissions: contents: read, push→main 한정), test_infra_guards.py(.github/workflows 스캔 추가), docker-stack.yml(celery_beat update_config rollback) 적용

### 테스트 결과
- pytest tests/: 76 passed, 5 failed (5건은 fastapi-pagination 0.15.15 환경 비호환 — 인프라 SPEC 이전부터 존재, 범위 밖)
- tests/test_infra_guards.py: 2 passed (GREEN)
- ruff py313 신규 findings: UP043 1건 → 수정 완료(0건). 기존 lint 부채(~700건)·format(11파일)은 SPEC 범위 밖 별도 정리 필요
- grep -rn ':latest' docker-stack.yml scripts/ → 0 matches (DoD 충족)
- YAML/compose 파싱: ci.yml/cd.yml/compose.dev.yml/docker-stack.yml 모두 OK

### 런타임 검증 유예 (라이브 Swarm/GitHub 필요)
- AC-1(무중단 폴링), AC-2(오버레이 Redis 실연결), AC-3(FCM 시크릿 실마운트), AC-5(GHCR 푸시+SSH 배포), AC-6(자동 롤백) — 문서화된 절차로 대체(docs/deployment.md)
