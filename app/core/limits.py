"""언바운드 리스트 엔드포인트 방어용 조회 상한 상수.

SPEC-PERF-001 REQ-PERF-004.

페이지네이션(`Page[...]`)이 없는 `list[...]` 응답 엔드포인트는 전체 행을 그대로
반환하므로, 데이터가 비정상적으로 커지면 단일 응답이 무한정 커질 수 있다. 이 모듈은
그런 경로에 적용하는 방어적(server-side) 조회 상한을 한 곳에서 정의한다.

- 상한은 응답 shape 를 바꾸지 않는다(여전히 `list[...]`). API-001 계약을 보존한다.
- 각 CRUD 조회는 `.limit(<상한>)` 을 적용하고, 반환 행 수가 상한과 같으면
  절단(truncation) 가능성을 경고 로그로 남긴다(운영 관측용).
- 이미 코드베이스는 `treatment_crud.get_treatments_to_autocomplete` 에서
  `.limit(100)` 방어 캡 관례를 사용한다. 본 상수는 그 관례를 명시적으로 확장한 것이다.

각 상한 근거:
- PHONEBOOK_GROUP_ITEMS_MAX: 샵별 전화번호부(연락처)는 바쁜 매장에서 크게 늘 수
  있는 가장 넓은 성장 경로이므로 다른 값보다 높게 둔다.
- SHOP_USERS_MAX / MENU_DETAILS_MAX / USER_DEVICE_TOKENS_MAX: 구조적으로 소규모
  (샵당 직원 수, 메뉴당 상세 수, 유저당 디바이스 수)지만 코드상 상한이 없어
  방어 캡을 둔다.
"""

# 전화번호부 그룹(with_items=true) 전체 항목 조회 상한.
PHONEBOOK_GROUP_ITEMS_MAX = 1000

# 특정 샵의 유저(직원) 목록 조회 상한.
SHOP_USERS_MAX = 500

# 특정 메뉴의 상세 항목 목록 조회 상한.
MENU_DETAILS_MAX = 500

# 특정 유저의 활성 디바이스 푸시 토큰 목록 조회 상한.
USER_DEVICE_TOKENS_MAX = 500
