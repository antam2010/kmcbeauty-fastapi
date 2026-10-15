# @MX:NOTE: [AUTO] 이메일 대소문자 무시 규약의 단일 소스. MySQL(대소문자 무시
#            collation) → PostgreSQL(대소문자 구분) 전환 후, DB 변경(CITEXT·함수
#            인덱스) 없이 앱에서 소문자로 정규화한다. 쓰기는
#            schemas.user.NormalizedEmailStr, 읽기는 user_crud.get_user_by_email 이
#            이 함수를 거친다(한쪽만 바꾸면 가입·로그인 비교가 어긋난다).
def normalize_email(email: str) -> str:
    """이메일을 저장·조회 공통 정규형(앞뒤 공백 제거 + 소문자)으로 변환한다.

    PostgreSQL 은 문자열을 대소문자 구분으로 비교하므로, 저장(스키마)과
    조회(CRUD)가 모두 이 정규형을 써야 `A@x.com` 과 `a@x.com` 이 같은 계정이 된다.
    """
    return email.strip().lower()
