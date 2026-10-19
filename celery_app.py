from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

# Redis 브로커/백엔드 URL은 중앙 settings 에서 단일 정의한다(REQ-INFRA-103).
# - Swarm 에서는 항상 서비스 이름(redis)으로 접근하므로 기본값이 서비스 이름이다.
# - 로컬 개발 등에서는 REDIS_URL 환경변수로 오버라이드할 수 있다.
celery_app = Celery(
    "worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["worker.tasks.treatment_task"],
)

celery_app.conf.timezone = "Asia/Seoul"


celery_app.conf.beat_schedule = {
    # 스케줄이 */30(30분마다)이므로 키 이름도 실제 주기에 맞춘다.
    "auto-complete-treatment-every-30min": {
        "task": "worker.tasks.treatment_task.auto_complete_treatment",
        "schedule": crontab(minute="*/30"),
    },
}
