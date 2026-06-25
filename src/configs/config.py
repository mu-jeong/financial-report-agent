import logging
from pathlib import Path

from dotenv import load_dotenv

from src.configs.settings import (
    BASE_DIR,
    LOG_FILE_DEFAULT,
    get_config_value,
)

load_dotenv()

# ==============================================================================
# 1. 파일 경로 설정
# ==============================================================================
SAVE_DIR = get_config_value("SAVE_DIR")
REPORT_PDF_DIR = get_config_value("REPORT_PDF_DIR")
DB_PATH = get_config_value("DB_PATH")
FAISS_DIR = get_config_value("FAISS_DIR")
CONVERSATION_DB_PATH = get_config_value("CONVERSATION_DB_PATH")
MONITORING_MODE = get_config_value("MONITORING_MODE")

# ==============================================================================
# 2. API 키 및 인증
# ==============================================================================
OPENROUTER_API_KEY = get_config_value("OPENROUTER_API_KEY")
OPENROUTER_APP_URL = get_config_value("OPENROUTER_APP_URL")
OPENROUTER_APP_TITLE = get_config_value("OPENROUTER_APP_TITLE")
OPENROUTER_DATA_COLLECTION = get_config_value("OPENROUTER_DATA_COLLECTION")

# ==============================================================================
# 3. LLM 및 파이프라인 상수 설정
# ==============================================================================
EMBEDDING_MODEL = get_config_value("EMBEDDING_MODEL")
GENERATION_MODEL = get_config_value("GENERATION_MODEL")

PARENT_CHUNK_SIZE = get_config_value("PARENT_CHUNK_SIZE")
CHILD_CHUNK_SIZE = get_config_value("CHILD_CHUNK_SIZE")
CHUNK_SIZE = get_config_value("CHUNK_SIZE")
CHUNK_OVERLAP = get_config_value("CHUNK_OVERLAP")
TEST_LIMIT = get_config_value("TEST_LIMIT")
SEARCH_TOP_K = get_config_value("SEARCH_TOP_K")
USE_RERANKER = get_config_value("USE_RERANKER")
RERANK_PROVIDER = get_config_value("RERANK_PROVIDER")
RERANK_MODEL = get_config_value("RERANK_MODEL")
RERANK_TIMEOUT = get_config_value("RERANK_TIMEOUT")
RERANK_CANDIDATE_MULTIPLIER = get_config_value("RERANK_CANDIDATE_MULTIPLIER")
RECENCY_WEIGHT = get_config_value("RECENCY_WEIGHT")
EXTRACTION_ENGINE = get_config_value("EXTRACTION_ENGINE")
USE_PARENT_CHILD = get_config_value("USE_PARENT_CHILD")

# ==============================================================================
# 4. 로깅 설정 (Logging)
# ==============================================================================
LOG_FILE = LOG_FILE_DEFAULT()


def _ensure_log_parent(log_file: str) -> None:
    """Create the log directory before configuring FileHandler."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)


_ensure_log_parent(LOG_FILE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# 서드파티 라이브러리 로깅 레벨 제한 (콘솔 도배 방지)
noisy_loggers = [
    "httpx",
    "httpcore",
    "faiss.loader",
    "faiss",
    "urllib3",
    "loader",
    "models",
    "_client",
]
for logger_name in noisy_loggers:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# ==============================================================================
# 5. 크롤러 설정 (Crawler Settings)
# ==============================================================================
CRAWLER_MODE = get_config_value("CRAWLER_MODE")
CRAWLER_CATEGORIES = get_config_value("CRAWLER_CATEGORIES")
CRAWLER_TARGET_DATE = get_config_value("CRAWLER_TARGET_DATE")
CRAWLER_TARGET_COUNT = get_config_value("CRAWLER_TARGET_COUNT")
CRAWLER_LOOKBACK_DAYS = get_config_value("CRAWLER_LOOKBACK_DAYS")
CRAWLER_MAX_LOOKBACK_DAYS = get_config_value("CRAWLER_MAX_LOOKBACK_DAYS")


def get_logger(name: str):
    return logging.getLogger(name)
