import logging, sys, os
from celery import Celery
from celery.schedules import crontab
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo 
import asyncio

from config.database import get_connection
from api.alab_sheets_bot import trigger_calls
from api.sf_sheets_bot import trigger_sf_calls         # salesforce_job  ← NEW

# LOGGING 
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CELERY INIT 
broker_url = os.getenv("CELERY_BROKER_URL")
result_backend = os.getenv("CELERY_RESULT_BACKEND")

if not broker_url:
    raise RuntimeError("CELERY_BROKER_URL is not set")

if not result_backend:
    raise RuntimeError("CELERY_RESULT_BACKEND is not set")

celery = Celery(
    "scheduler",
    broker=broker_url,
    backend=result_backend,
)

celery.conf.timezone = "America/Los_Angeles"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# MAIN SCHEDULER 
@celery.task(bind=True, max_retries=3)
def run_scheduler(self):
    now_local = datetime.now(LOCAL_TZ)
    now_time_only = now_local.time()
    today = now_local.strftime("%A").lower()
    logger.info(f"Scheduler running at {now_local.isoformat()}")

    try:
        with get_connection() as conn:
            sheets = conn.execute(
                "SELECT * FROM sheets WHERE status=TRUE"
            ).fetchall()

            for sheet in sheets:
                sheet_id = sheet["id"]
                job_type = sheet["type"] or "google_sheet_job"

                logger.info(f"Checking sheet {sheet_id} (type={job_type})")

                schedules = conn.execute("""
                    SELECT * FROM sheet_schedules
                    WHERE sheet_id=%s AND lower(day_of_week)=%s
                """, (sheet_id, today)).fetchall()

                if not schedules:
                    logger.info(f"Skipping sheet {sheet_id} (no schedule today)")
                    continue

                for sched in schedules:
                    start_time = sched["start_time"]
                    end_time   = sched["end_time"]

                    if start_time == "00:00" and end_time == "00:00":
                        logger.info(f"Sheet {sheet_id} inactive today")
                        continue

                    start_dt = datetime.strptime(start_time, "%H:%M").time()
                    end_dt   = datetime.strptime(end_time,   "%H:%M").time()

                    if start_dt <= end_dt:
                        in_window = start_dt <= now_time_only <= end_dt
                    else:
                        in_window = now_time_only >= start_dt or now_time_only <= end_dt

                    if not in_window:
                        logger.info(
                            f"Sheet {sheet_id} outside window "
                            f"{start_time}-{end_time} (now {now_time_only.strftime('%H:%M')})"
                        )
                        continue

                    #  last-run guard (2-min cooldown) 
                    last_run_raw = sheet["last_run"]
                    last_run_dt  = None

                    if last_run_raw:
                        if isinstance(last_run_raw, str):
                            try:
                                last_run_dt = datetime.fromisoformat(last_run_raw)
                            except Exception as e:
                                logger.warning(f"Invalid last_run for sheet {sheet_id}: {e}")
                        elif hasattr(last_run_raw, 'year'):  # datetime object from DB
                            last_run_dt = last_run_raw

                    if last_run_dt:
                        if last_run_dt.tzinfo is None:
                            last_run_dt = last_run_dt.replace(tzinfo=timezone.utc)
                        last_run_local = last_run_dt.astimezone(LOCAL_TZ)
                        if (now_local - last_run_local) < timedelta(minutes=2):
                            logger.info(
                                f"Skipping sheet {sheet_id} "
                                f"(ran at {last_run_local.strftime('%H:%M')})"
                            )
                            continue

                    #  stamp last_run BEFORE dispatching to block duplicate fires 
                    conn.execute(
                        "UPDATE sheets SET last_run=%s WHERE id=%s",
                        (datetime.now(timezone.utc).isoformat(), sheet_id)
                    )
                    conn.commit()

                    #  dispatch to the right worker 
                    process_sheet.delay(sheet_id, job_type)
                    logger.info(f"Dispatched sheet {sheet_id} (type={job_type})")

    except Exception as e:
        logger.error(f"Scheduler failure: {e}", exc_info=True)
        raise self.retry(countdown=10)

# WORKER TASK 
@celery.task(bind=True, max_retries=3)
def process_sheet(self, sheet_id: int, job_type: str = "google_sheet_job"):
    """
    Route to the correct async workflow based on job_type.

      google_sheet_job  →  trigger_calls()       (alab_sheets_bot)
      salesforce_job    →  trigger_sf_calls()    (sf_sheets_bot)
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            if job_type == "salesforce_job":
                logger.info(f"Running salesforce_job for sheet_id={sheet_id}")
                loop.run_until_complete(trigger_sf_calls(sheet_id))
            else:
                logger.info(f"Running google_sheet_job for sheet_id={sheet_id}")
                loop.run_until_complete(trigger_calls(sheet_id))
        finally:
            loop.close()

        logger.info(f"Completed sheet_id={sheet_id} (type={job_type})")

    except Exception as e:
        logger.error(f"Sheet {sheet_id} error: {e}", exc_info=True)
        raise self.retry(countdown=20)


# BEAT SCHEDULE 
celery.conf.beat_schedule = {
    "run-every-2-minutes": {
        "task": "core.celery_app.run_scheduler",
        "schedule": crontab(minute="*/2"),
    },
}