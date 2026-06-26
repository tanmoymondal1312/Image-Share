import logging
import shutil
from apscheduler.schedulers.background import BackgroundScheduler
from config import UPLOAD_DIR
from database import get_db

logger = logging.getLogger(__name__)


def cleanup_expired():
    with get_db() as conn:
        # Step 1: delete each expired image file + row
        expired = conn.execute(
            "SELECT id, group_id, filename FROM images WHERE expires_at <= datetime('now')"
        ).fetchall()

        for img in expired:
            fp = UPLOAD_DIR / img["group_id"] / img["filename"]
            try:
                if fp.exists():
                    fp.unlink()
            except OSError:
                pass
            conn.execute("DELETE FROM images WHERE id = ?", (img["id"],))
            logger.info("Removed expired image: %s", img["filename"])

        # Step 2: delete groups that now have zero images
        empty = conn.execute("""
            SELECT id FROM image_groups
            WHERE id NOT IN (SELECT DISTINCT group_id FROM images)
        """).fetchall()

        for g in empty:
            group_dir = UPLOAD_DIR / g["id"]
            if group_dir.exists():
                shutil.rmtree(group_dir, ignore_errors=True)
            conn.execute("DELETE FROM image_groups WHERE id = ?", (g["id"],))
            logger.info("Removed empty group: %s", g["id"])

        # Step 3: sync group metadata from remaining images
        conn.execute("""
            UPDATE image_groups SET
                image_count = (SELECT COUNT(*) FROM images WHERE group_id = image_groups.id),
                expires_at  = COALESCE(
                    (SELECT MAX(expires_at) FROM images WHERE group_id = image_groups.id),
                    expires_at
                )
        """)


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(cleanup_expired, "interval", minutes=5, id="cleanup")
    scheduler.start()
    return scheduler
