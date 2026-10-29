"""목록 응답 Page 타입 — fastapi-pagination Page 에 페이지 번호 상한을 더한다.

add_pagination 은 응답 모델(Page)의 Params 로 page · size 쿼리를 검증한다. 라이브러리
Page 를 그대로 쓰면 page 에 상한이 없다(size 는 1~100). 상한은 라이브러리가 지원하는
CustomizedPage + UseParamsFields 로만 더하고, 응답 모양과 OpenAPI 스키마 이름
(Page_XxxResponse_)은 UseName 으로 그대로 둔다.
"""

from fastapi import Query
from fastapi_pagination import Page as LibraryPage
from fastapi_pagination.customization import CustomizedPage, UseName, UseParamsFields

from app.schemas.limits import PAGE_MAX

# @MX:ANCHOR: [AUTO] Page[...] 목록 엔드포인트 전체의 page · size 검증 단일 지점.
# @MX:REASON: GET /shops · /treatments · /phonebooks · /treatment-menus 가 쓴다
#             (fan_in 4). fastapi_pagination.Page 를 직접 쓰면 page 상한이 빠져 OFFSET
#             이 bigint 를 넘는 page 가 500 이 된다(tests/test_date_page_bounds_fix.py).
Page = CustomizedPage[
    LibraryPage,
    UseName("Page"),
    UseParamsFields(page=Query(1, ge=1, le=PAGE_MAX, description="Page number")),
]
