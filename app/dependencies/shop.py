from fastapi import Depends, status
from sqlalchemy.orm import Session

from app.crud.shop_crud import get_member_shop_by_id
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.user import User
from app.utils.redis.shop import get_selected_shop_redis, set_selected_shop_redis


# @MX:ANCHOR: [AUTO] shop-scoped 접근 멤버십 게이트 (BOLA 방지)
# @MX:REASON: 다수 라우터(treatment/summary/phonebook/treatment_menu)가 의존하는 접근
#             검증 단일 지점. get_member_shop_by_id 로 user.id 가 shop_user 멤버인 활성
#             샵만 반환해야 함(생성자 여부 무관). Redis 선택값은 신뢰하지 않고 요청마다
#             재검증하므로 멤버십이 해제되면 다음 요청부터 404 SHOP_NOT_FOUND
#             (SPEC-SECURITY-001 REQ-SEC-003).
def get_current_shop(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Shop:
    shop_id = get_selected_shop_redis(user.id)
    if not shop_id:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SHOP_NOT_SELECTED",
            hint="redis에 만료되거나 없음.",
        )

    shop = get_member_shop_by_id(db, user.id, shop_id)
    if not shop:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SHOP_NOT_FOUND",
            hint="그런 상점 없습니다.",
        )
    set_selected_shop_redis(user.id, shop.id)
    return shop
