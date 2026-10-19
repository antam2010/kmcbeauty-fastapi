import logging

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.limits import USER_DEVICE_TOKENS_MAX
from app.models.device_push_token import DevicePushToken
from app.models.shop_user import ShopUser
from app.models.user import User

logger = logging.getLogger(__name__)


# 소유권 미검증 단건 조회(get_device_token_by_id)는 두지 않는다. 단건 접근은 항상
# 소유자 조건을 포함해 조회하여, 타인 토큰은 "없음"(404)으로 보이게 한다
# (존재 여부 은닉 · shop_crud.get_user_shop_by_id 와 같은 관례).
def get_user_device_token_by_id(
    db: Session,
    user_id: int,
    token_id: int,
) -> DevicePushToken | None:
    """유저가 소유한 디바이스 푸시 토큰을 ID로 조회(타인 토큰은 None)."""
    return (
        db.query(DevicePushToken)
        .filter(DevicePushToken.id == token_id, DevicePushToken.user_id == user_id)
        .first()
    )


def get_device_tokens_by_user(
    db: Session,
    user_id: int,
    is_active: bool = True,
) -> list[DevicePushToken]:
    """유저 ID로 디바이스 푸시 토큰 목록 조회.

    SPEC-PERF-001 REQ-PERF-004: `/device-tokens/me` 언바운드 조회 방어 상한.
    구조적으로 소규모(유저당 디바이스 수)지만 코드상 상한이 없으므로 방어 캡을
    둔다. 응답 shape(list[...])는 유지한다. 상한 도달 시 절단 가능성을 경고
    로그로 남긴다.
    """
    query = db.query(DevicePushToken).filter(DevicePushToken.user_id == user_id)
    if is_active:
        query = query.filter(DevicePushToken.is_active == is_active)
    # 캡(.limit) 전에 결정적 정렬을 보장한다(SPEC-PERF-001 REQ-PERF-004).
    # FCM 전송 경로도 이 함수를 공유하므로 안정적 순서가 운영에서도 의미가 있다.
    tokens = (
        query.order_by(DevicePushToken.id.desc()).limit(USER_DEVICE_TOKENS_MAX).all()
    )
    if len(tokens) >= USER_DEVICE_TOKENS_MAX:
        logger.warning(
            "get_device_tokens_by_user hit row cap (%d) for user_id=%s; "
            "result may be truncated",
            USER_DEVICE_TOKENS_MAX,
            user_id,
        )
    return tokens


def get_device_tokens_by_shop(
    db: Session,
    shop_id: int,
    is_active: bool = True,
) -> list[DevicePushToken]:
    """샵 ID로 디바이스 푸시 토큰 목록 조회 (현재 샵 멤버의 토큰만).

    토큰의 shop_id 는 등록 시점의 값이라 멤버에서 제외된 직원의 토큰에도 남아 있다.
    shop_user 와 (shop_id, user_id) 로 조인해 지금 멤버인 유저의 토큰만 반환한다.
    조인 키가 uq_shop_user(shop_id, user_id) 이므로 행이 중복되지 않는다.
    탈퇴(소프트삭제) 유저는 shop_user 행이 이력으로 남으므로 users 를 조인해 뺀다.
    """
    query = (
        db.query(DevicePushToken)
        .join(
            ShopUser,
            and_(
                ShopUser.shop_id == DevicePushToken.shop_id,
                ShopUser.user_id == DevicePushToken.user_id,
            ),
        )
        .join(User, User.id == ShopUser.user_id)
        .filter(DevicePushToken.shop_id == shop_id, User.deleted_at.is_(None))
    )
    if is_active:
        query = query.filter(DevicePushToken.is_active == is_active)
    return query.all()


def create_device_token(
    db: Session,
    device_token_data: dict,
) -> DevicePushToken:
    """디바이스 푸시 토큰 생성."""
    device_token = DevicePushToken(**device_token_data)
    db.add(device_token)
    db.flush()
    return device_token


def update_device_token(
    db: Session,
    device_token: DevicePushToken,
    update_data: dict,
) -> DevicePushToken:
    """디바이스 푸시 토큰 업데이트."""
    for key, value in update_data.items():
        if value is not None:
            setattr(device_token, key, value)
    db.flush()
    return device_token


def delete_device_token(db: Session, device_token: DevicePushToken) -> None:
    """디바이스 푸시 토큰 삭제."""
    db.delete(device_token)
    db.flush()


def deactivate_device_token(
    db: Session,
    device_token: DevicePushToken,
) -> DevicePushToken:
    """디바이스 푸시 토큰 비활성화."""
    device_token.is_active = False
    db.flush()
    return device_token


def deactivate_device_tokens_by_user(db: Session, user_id: int) -> int:
    """유저의 디바이스 푸시 토큰을 모두 비활성화(is_active=False)한다.

    탈퇴(소프트삭제) 트랜잭션 안에서 호출한다. 행을 지우지 않고 UPDATE 한 번으로
    끈다. 커밋 경계는 서비스가 소유한다(여기서는 commit 하지 않는다).

    Returns:
        int: 대상이 된 토큰 수.

    """
    return (
        db.query(DevicePushToken)
        .filter(DevicePushToken.user_id == user_id)
        .update({DevicePushToken.is_active: False})
    )


def get_or_create_device_token(  # noqa: PLR0913  # 토큰 레코드 필드가 본질적으로 6개다
    db: Session,
    user_id: int,
    shop_id: int | None,
    device_id: str | None,
    token: str,
    platform: str,
) -> DevicePushToken:
    """디바이스 푸시 토큰 조회 또는 생성 (토큰 기준)."""
    # 기존 토큰 조회
    existing = db.query(DevicePushToken).filter(DevicePushToken.token == token).first()

    if existing:
        # 기존 토큰 업데이트 — 같은 FCM 토큰(앱 설치 단위)을 마지막으로 등록한 유저가
        # 소유자가 된다(같은 기기에서 다른 계정으로 로그인한 경우의 인계).
        # 호출부는 user_id=요청자, shop_id=요청자가 멤버인 샵만 넘기므로
        # 이 덮어쓰기로 타인 계정·타 샵에 토큰을 심을 수는 없다.
        existing.user_id = user_id
        existing.shop_id = shop_id
        existing.device_id = device_id
        existing.platform = platform
        existing.is_active = True
        db.flush()
        return existing

    # 새 토큰 생성
    new_token = DevicePushToken(
        user_id=user_id,
        shop_id=shop_id,
        device_id=device_id,
        token=token,
        platform=platform,
        is_active=True,
    )
    db.add(new_token)
    db.flush()
    return new_token
