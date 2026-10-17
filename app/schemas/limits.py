"""요청 스키마 입력 범위 상수 — DB 정수 컬럼 범위와 맞춘다.

PostgreSQL 은 컬럼 범위를 넘는 값을 DataError 로 거절한다. 스키마가 범위를 검증하지
않으면 그 값은 500 이 되므로 요청 검증 단계(422)에서 막는다.

psycopg 방언은 정수 바인드에 ::INTEGER / ::SMALLINT 캐스트를 붙인다. 그래서 int4
범위를 넘는 id 는 INSERT 뿐 아니라 조회 조건(WHERE id = ...)에서도 500 이다.
문자열 컬럼 길이(String(n))는 각 스키마 필드의 max_length 에 그대로 적는다.
"""

# @MX:NOTE: [AUTO] 요청 스키마의 id · 정수 필드 상한 단일 소스. 컬럼 타입을 바꾸면
#            (예: BigInteger) 이 값과 해당 스키마의 le= 를 함께 바꾼다.
# PostgreSQL integer(int4) 최댓값. 시리얼 PK · FK · integer 컬럼 공통.
PG_INT_MAX = 2**31 - 1

# PostgreSQL smallint(int2) 최댓값(treatment_item.session_no).
PG_SMALLINT_MAX = 2**15 - 1
