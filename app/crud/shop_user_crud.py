import logging

from sqlalchemy.orm import Session, aliased, contains_eager

from app.core.limits import SHOP_USERS_MAX
from app.models.shop_user import ShopUser
from app.models.user import User

logger = logging.getLogger(__name__)


# @MX:ANCHOR: [AUTO] 샵 멤버십 판정 단일 진입점 — 탈퇴(소프트삭제) 유저는 멤버가 아니다.
# @MX:REASON: 대표원장 게이트 · 직원 관리 · 초대코드 · 푸시 인가 · 시술 담당자 검증이
#             모두 이 함수로 멤버 여부를 판정한다. 탈퇴해도 shop_user 행은 이력으로
#             남기므로 users 조인 + deleted_at IS NULL 을 빼면 탈퇴 유저가 멤버가 된다.
def get_shop_user(db: Session, shop_id: int, user_id: int) -> ShopUser | None:
    """샵 멤버십(shop_user) 단건 조회 — 탈퇴(소프트삭제) 유저는 제외한다.

    탈퇴해도 shop_user 행은 이력으로 남긴다. 그래서 users 를 조인해 활성 유저의
    매핑만 멤버로 본다(탈퇴 유저는 None).

    Args:
        db (Session): Database session.
        shop_id (int): 샵 ID.
        user_id (int): 유저 ID.

    Returns:
        ShopUser | None: 활성 유저의 매핑 또는 None.

    """
    return (
        db.query(ShopUser)
        .join(User, User.id == ShopUser.user_id)
        .filter(
            ShopUser.shop_id == shop_id,
            ShopUser.user_id == user_id,
            User.deleted_at.is_(None),
        )
        .first()
    )


def users_share_shop(db: Session, user_id: int, other_user_id: int) -> bool:
    """두 유저가 최소 하나의 샵 멤버십(shop_user)을 공유하는지 여부.

    shop_user 를 shop_id 로 자기조인해 두 유저의 매핑이 같은 샵에 있는지 본다.
    탈퇴(소프트삭제) 유저의 매핑은 이력일 뿐이므로 양쪽 모두 users 를 조인해 활성
    유저일 때만 공유로 본다. 유저 대상 푸시 전송 인가(같은 샵 멤버끼리만)에 사용한다.

    Args:
        db (Session): Database session.
        user_id (int): 기준 유저 ID(요청자).
        other_user_id (int): 상대 유저 ID(대상).

    Returns:
        bool: 두 유저가 모두 활성이고 공유 샵이 하나라도 있으면 True.

    """
    peer = aliased(ShopUser, name="peer")
    peer_user = aliased(User, name="peer_user")
    shared = (
        db.query(ShopUser.id)
        .join(peer, peer.shop_id == ShopUser.shop_id)
        .join(User, User.id == ShopUser.user_id)
        .join(peer_user, peer_user.id == peer.user_id)
        .filter(
            ShopUser.user_id == user_id,
            peer.user_id == other_user_id,
            User.deleted_at.is_(None),
            peer_user.deleted_at.is_(None),
        )
        .first()
    )
    return shared is not None


def create_shop_user(db: Session, shop_user_data: ShopUser) -> ShopUser:
    """샵 유저 생성.

    Args:
        db (Session): Database session.
        shop_user_data (ShopUser): Shop user data to create.

    Returns:
        ShopUser: The created shop user.

    """
    db.add(shop_user_data)
    db.flush()
    return shop_user_data


def get_shop_users_by_shop_id(db: Session, shop_id: int) -> list[ShopUser]:
    """팀 샵 유저의 샵 ID 목록 조회(탈퇴 유저 제외).

    Args:
        db (Session): Database session.
        shop_id (int): The ID of the shop to filter by.

    Returns:
        list[ShopUser]: List of shop users associated with the given shop ID.

    """
    # SPEC-PERF-001 REQ-PERF-004: 언바운드 조회 방어 상한. 구조적으로 소규모
    # (샵당 직원 수)지만 코드상 상한이 없으므로 방어 캡을 둔다. 응답 shape 는
    # 유지한다. 상한 도달 시 절단 가능성을 경고 로그로 남긴다.
    users = (
        db.query(ShopUser)
        # 탈퇴(소프트삭제) 유저는 목록에서 뺀다(shop_user 행은 이력으로 남는다).
        # user 관계는 필터를 건 이 조인으로 채운다(contains_eager, 중복 조인 없음).
        .join(User, User.id == ShopUser.user_id)
        .options(contains_eager(ShopUser.user))
        .filter(ShopUser.shop_id == shop_id, User.deleted_at.is_(None))
        # 캡(.limit) 전에 결정적 정렬을 보장한다(SPEC-PERF-001 REQ-PERF-004).
        # ORDER BY 없는 LIMIT 은 RDB 가 비결정적 순서를 반환할 수 있다.
        .order_by(ShopUser.id.desc())
        .limit(SHOP_USERS_MAX)
        .all()
    )
    if len(users) >= SHOP_USERS_MAX:
        logger.warning(
            "get_shop_users_by_shop_id hit row cap (%d) for shop_id=%s; "
            "result may be truncated",
            SHOP_USERS_MAX,
            shop_id,
        )
    return users


def get_shop_user_with_user(
    db: Session,
    shop_id: int,
    user_id: int,
) -> ShopUser | None:
    """샵-유저 단건을 user 관계와 함께 조회(응답 직렬화용, 탈퇴 유저 제외).

    직원 연결 수정의 대상 조회에도 쓰인다. get_shop_user 와 같이 탈퇴 유저의 매핑은
    없음(None)으로 본다.

    Args:
        db (Session): Database session.
        shop_id (int): 샵 ID.
        user_id (int): 유저 ID.

    Returns:
        ShopUser | None: user 가 eager-load 된 매핑 또는 None.

    """
    return (
        db.query(ShopUser)
        .join(User, User.id == ShopUser.user_id)
        .options(contains_eager(ShopUser.user))
        .filter(
            ShopUser.shop_id == shop_id,
            ShopUser.user_id == user_id,
            User.deleted_at.is_(None),
        )
        .first()
    )


def delete_shop_user(db: Session, shop_user: ShopUser) -> None:
    """샵-유저 매핑(멤버십) 물리 삭제.

    ShopUser 는 SoftDeleteMixin 을 갖지 않는 순수 연결 테이블이므로 매핑 자체를
    제거한다(유저 계정은 그대로 유지된다). 커밋 경계는 서비스 계층이 소유한다
    (SPEC-FIX-001 REQ-FIX-005): 여기서는 delete 만 수행하고 commit 은 하지 않는다.

    Args:
        db (Session): Database session.
        shop_user (ShopUser): 삭제할 매핑 인스턴스.

    """
    db.delete(shop_user)
