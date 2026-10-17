"""요청 스키마 입력 범위 상수 — DB 정수 컬럼 범위와 맞춘다.

PostgreSQL 은 컬럼 범위를 넘는 값을 DataError 로 거절한다. 스키마가 범위를 검증하지
않으면 그 값은 500 이 되므로 요청 검증 단계(422)에서 막는다.

psycopg 방언은 정수 바인드에 ::INTEGER / ::SMALLINT 캐스트를 붙인다. 그래서 int4
범위를 넘는 id 는 INSERT 뿐 아니라 조회 조건(WHERE id = ...)에서도 500 이다.
문자열 컬럼 길이(String(n))는 각 스키마 필드의 max_length 에 그대로 적는다.

조회 조건의 날짜 · 페이지 번호도 여기서 범위를 정한다(서버 계산이 넘치지 않게).
"""

from datetime import date

# @MX:NOTE: [AUTO] 요청 스키마의 id · 정수 필드 상한 단일 소스. 컬럼 타입을 바꾸면
#            (예: BigInteger) 이 값과 해당 스키마의 le= 를 함께 바꾼다.
# PostgreSQL integer(int4) 최댓값. 시리얼 PK · FK · integer 컬럼 공통.
PG_INT_MAX = 2**31 - 1

# PostgreSQL smallint(int2) 최댓값(treatment_item.session_no).
PG_SMALLINT_MAX = 2**15 - 1

# 날짜 조회 조건(쿼리 파라미터)의 허용 범위. 필터는 KST 날짜를 UTC 경계로 바꾸고 끝
# 날짜에 하루를 더한다(app/utils/query.py, 대시보드는 월말까지). 범위가 없으면
# 0001-01-01 · 9999-12-31 같은 값이 파이썬 date 범위(1~9999년)를 넘어
# OverflowError(500)였다. 이 범위 밖은 422 다.
FILTER_DATE_MIN = date(1900, 1, 1)
FILTER_DATE_MAX = date(2100, 12, 31)

# 목록 페이지 번호(page) 상한. fastapi-pagination 의 OFFSET 은 size * (page - 1) 이다
# (size 는 라이브러리가 1~100 으로 제한). SQLAlchemy 는 이 값을 크기에 따라 ::INTEGER ·
# ::BIGINT 로 캐스트해 바인드하므로, 상한이 없으면 OFFSET 이 bigint 를 넘는 page 가
# DataError(500)였다. 100 * (PAGE_MAX - 1) 은 int4 안쪽이다.
PAGE_MAX = 100_000
