from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery("tripops", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.task_routes = {
    "tripops.text_fast": {"queue": "text_fast_queue"},
    "tripops.reasoning": {"queue": "reasoning_queue"},
    "tripops.vision": {"queue": "vision_queue"},
    "tripops.image": {"queue": "image_queue"},
}


@celery_app.task(name="tripops.text_fast")
def text_fast_task(payload: dict) -> dict:
    return {"model": settings.qwen_fast_model, "status": "accepted", "payload": payload}


@celery_app.task(name="tripops.reasoning")
def reasoning_task(payload: dict) -> dict:
    return {"model": settings.qwen_reasoning_model, "status": "accepted", "payload": payload}


@celery_app.task(name="tripops.vision")
def vision_task(payload: dict) -> dict:
    return {"model": settings.qwen_vision_model, "status": "accepted", "payload": payload}


@celery_app.task(name="tripops.image")
def image_task(payload: dict) -> dict:
    return {"endpoint": settings.imagegen_api_url, "status": "accepted", "payload": payload}
