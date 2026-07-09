# 2노드 확장 가이드 (SPEC-INFRA-001, M5)

단일 노드 Swarm 을 2노드로 확장할 때의 절차와 제약. **문서 수준 준비**이며,
실제 확장 시 이 문서를 기준으로 배치 제약과 라우팅을 먼저 점검한다.

---

## 1. 워커 노드 조인 절차 (REQ-INFRA-016)

매니저 노드에서 조인 토큰을 발급한다:

```bash
# 매니저에서 워커용 조인 토큰과 명령을 확인
docker swarm join-token worker
```

출력된 명령을 신규 워커 노드에서 실행한다:

```bash
# 워커 노드에서
docker swarm join --token <WORKER-JOIN-TOKEN> <MANAGER-IP>:2377
```

확인:

```bash
docker node ls        # 매니저에서 노드 2개(Ready/Active) 확인
```

사전 요구:

- 두 노드 간 방화벽 개방: 2377/tcp(클러스터 관리), 7946/tcp+udp(노드 통신), 4789/udp(오버레이 VXLAN)
- 두 노드 모두 GHCR pull 로그인 구성(`docker login ghcr.io`) — 그렇지 않으면 해당 노드에서
  이미지 pull 이 실패해 태스크가 스케줄되지 못한다.
- `shared_network_prod` 오버레이는 매니저에서 생성되어 있으면 클러스터 전체에서 사용 가능.

---

## 2. 배치 제약 — 단일 인스턴스 서비스 (REQ-INFRA-017)

2노드로 확장하면 단일 인스턴스여야 하는 서비스가 잘못된 노드로 재스케줄되거나
중복 기동될 위험이 있다. 특히:

- **`celery_beat`**: 반드시 1개만 떠야 한다. 2개가 뜨면 스케줄 태스크가 **중복 디스패치**된다.
- **`redis`**: 단일 인스턴스 + 볼륨(`redis_data`). 다른 노드로 재스케줄되면 데이터가
  분리(split)되고 브로커 상태가 깨진다.

### 옵션 A: 노드 라벨 + placement 제약(권장, 최소 변경)

특정 노드에 라벨을 부여하고 단일 인스턴스 서비스를 그 노드에 고정한다.

```bash
# 매니저에서: data 역할 노드에 라벨 부여
docker node update --label-add role=data <NODE-1>
```

`docker-stack.yml` 의 `redis`, `celery_beat` `deploy` 에 다음을 추가(확장 시점에 적용):

```yaml
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.labels.role == data
```

- `redis` 는 볼륨이 있는 노드에 고정되어 데이터 일관성을 유지한다.
- `celery_beat` 를 동일 노드에 고정해 단일 스케줄러를 보장한다.
- `kmcbeauty_api`, `celery_worker` 는 제약 없이 두 노드에 분산(무상태이므로 안전).

### 옵션 B: Redis 외부화

`redis` 를 스택에서 제거하고 관리형/전용 Redis 로 외부화한 뒤,
`.env.prod` 의 `REDIS_URL` 을 외부 엔드포인트로 지정한다. 이 경우 placement 제약은
`celery_beat` 에만 필요하다. 노드 증설이 잦거나 Redis 가용성이 중요하면 권장.

---

## 3. NGINX 업스트림 고려사항 (REQ-INFRA-018)

- 외부 NGINX 는 `shared_network_prod` 오버레이에 연결되어 서비스 DNS
  `kmcbeauty_api:8000` 으로 라우팅한다. **Swarm 내장 로드밸런서(VIP)** 가 오버레이에서
  두 노드에 분산된 `kmcbeauty_api` 레플리카로 자동 분배하므로, NGINX 는 개별 노드 IP 가
  아니라 서비스 DNS(VIP) 하나만 업스트림으로 두면 된다.

  ```nginx
  upstream kmcbeauty_api {
      server kmcbeauty_api:8000;   # 오버레이 VIP — Swarm 이 레플리카로 분산
  }
  ```

- NGINX 자체가 오버레이 밖(호스트)에 있다면, `docker network connect shared_network_prod <nginx-container>`
  로 오버레이에 붙이거나, NGINX 를 스택 서비스로 편입해 동일 오버레이에서 서비스 DNS 를 해석하게 한다.
- 헬스체크: 업스트림에 `/health` 기반 능동 헬스체크(가능 시)를 두어 롤링 중 미준비 레플리카로의
  라우팅을 줄인다. Swarm VIP + 서비스 헬스체크가 1차 방어선이고, NGINX 헬스체크는 보조.
- 2노드에서도 호스트 포트는 공개하지 않는다(현 구조 유지). 모든 유입은 NGINX → 오버레이 서비스 DNS.

---

## 4. 확장 전 체크리스트

- [ ] 두 노드 방화벽 포트(2377/7946/4789) 개방
- [ ] 두 노드 모두 GHCR pull 로그인 완료
- [ ] `redis`/`celery_beat` 에 placement 제약 적용(또는 Redis 외부화)
- [ ] `celery_beat` 가 정확히 1개인지 확인(`docker service ps kmcbeauty_celery_beat`)
- [ ] NGINX 가 오버레이 서비스 DNS(VIP)로 라우팅하는지 확인
- [ ] 무중단 재배포 리허설(AC-1): `/health` 1초 폴링 중 연속 2회 배포, 실패 0건 확인
