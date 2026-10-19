import re

PHONE_REGEX = re.compile(r"^(\d{2,3})-?(\d{3,4})-?(\d{4})$")


def is_valid_korean_phone_number(phone: str) -> bool:
    """전화번호가 유효한 한국 전화번호 형식인지 확인한다.

    허용 예:
    - 01012345678
    - 010-1234-5678
    - 02-123-4567
    - 031-1234-5678
    """
    return bool(PHONE_REGEX.match(phone.strip()))


def normalize_korean_phone_number(phone: str) -> str:
    """전화번호를 000-0000-0000 형식으로 변환한다."""
    match = PHONE_REGEX.match(phone.strip())
    if not match:
        return phone.strip()
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
