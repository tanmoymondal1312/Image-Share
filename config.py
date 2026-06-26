import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "images.db"

MAX_IMAGES_PER_GROUP = 5
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
IMAGE_EXPIRY_HOURS = 1
ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
SHORT_CODE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
SHORT_CODE_LENGTH = 4
SITE_URL = os.getenv("SITE_URL", "https://image-share.mediaghor.com")
SITE_NAME = "ImageShare"

UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
