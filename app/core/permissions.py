# app/core/permission.py
from fastapi import Depends, HTTPException, params, status

from app.dependencies.auth import get_current_user
from app.models.user import User


# 관리자만 접근 가능
def admin_required(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return current_user


# 본인이거나 관리자일 경우만 접근 가능
def is_owner_or_admin(target_user_id: int) -> params.Depends:
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role != "ADMIN" and current_user.id != target_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="접근 권한이 없습니다.",
            )
        return current_user

    return Depends(dependency)
