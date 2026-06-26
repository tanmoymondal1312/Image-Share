import logging
import shutil
from apscheduler.schedulers.background import BackgroundScheduler
from config import UPLOAD_DIR
from database import get_db

logger = logging.getLogger(__name__)


def cleanup_expired():
    with get_db() as conn:
        expired = conn.execute(
            "SELECT id FROM image_groups WHERE expires_at <= datetime('now')"
        ).fetchall()

        for row in expired:
            group_dir = UPLOAD_DIR / row["id"]
            if group_dir.exists():
                shutil.rmtree(group_dir)
            conn.execute("DELETE FROM image_groups WHERE id = ?", (row["id"],))
            logger.info("Cleaned up expired group: %s", row["id"])


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(cleanup_expired, "interval", minutes=5, id="cleanup")
    scheduler.start()
    return scheduler
