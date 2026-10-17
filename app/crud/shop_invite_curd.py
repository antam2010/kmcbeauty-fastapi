import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.shop_invite import ShopInvite


def create_invite(db: Session, shop_id: int, expire_in: int) -> ShopInvite:
    invite_code = secrets.token_urlsafe(8)
    expired_at = datetime.now(UTC) + timedelta(seconds=expire_in)

    invite = ShopInvite(
        shop_id=shop_id,
        invite_code=invite_code,
        expired_at=expired_at,
    )
    db.add(invite)
    db.flush()
    return invite


def get_invite_by_shop_id(
    db: Session,
    shop_id: int,
) -> ShopInvite | None:
    """샵 ID로 초대코드 조회.

    :param db: DB 세션
    :param shop_id: 샵 ID
    :return: 초대코드 정보
    """
    return db.query(ShopInvite).filter(ShopInvite.shop_id == shop_id).first()


def get_valid_invite_by_shop_id(db: Session, shop_id: int) -> ShopInvite | None:
    """샵 ID로 유효한 초대코드 조회.

    :param db: DB 세션
    :param shop_id: 샵 ID
    :return: 유효한 초대코드 정보
    """
    # 현재 시간보다 유효한 초대코드만 조회
    return (
        db.query(ShopInvite)
        .filter(
            ShopInvite.shop_id == shop_id,
            ShopInvite.expired_at > datetime.now(UTC),
        )
        .first()
    )


def delete_invite_by_shop_id(db: Session, shop_id: int) -> None:
    """샵 ID로 초대코드 삭제.

    :param db: DB 세션
    :param shop_id: 샵 ID
    """
    db.query(ShopInvite).filter(ShopInvite.shop_id == shop_id).delete()
    db.flush()


def get_invite_by_code(
    db: Session,
    invite_code: str,
    *,
    for_update: bool = False,
) -> ShopInvite | None:
    """샵 코드로 초대코드 조회(만료 전 코드만).

    :param db: DB 세션
    :param invite_code: 샵 코드
    :param for_update: True 면 행을 잠근다(SELECT ... FOR UPDATE). 가입이 코드를
        소비할 때 같은 코드로 동시에 들어온 가입이 커밋까지 기다리게 한다.
    :return: 초대코드 정보
    """
    query = db.query(ShopInvite).filter(
        ShopInvite.invite_code == invite_code,
        ShopInvite.expired_at > datetime.now(UTC),
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def delete_invite(db: Session, invite: ShopInvite) -> None:
    """초대코드 한 건을 삭제한다(가입이 소비). 커밋 경계는 서비스가 소유한다.

    :param db: DB 세션
    :param invite: 삭제할 초대코드(가입 트랜잭션에서 잠근 행)
    """
    db.delete(invite)
    db.flush()
