import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# 1. 파일 경로 설정
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent.parent

SAVE_DIR = os.path.join(BASE_DIR, "data", "downloaded")
REPORT_PDF_DIR = os.getenv("REPORT_PDF_DIR", SAVE_DIR)
DB_PATH = os.path.join(BASE_DIR, "data", "reports.db")
FAISS_DIR = os.path.join(BASE_DIR, "data", "vector_db")
CONVERSATION_DB_PATH = os.path.join(BASE_DIR, "data", "conversations.db")

# ==============================================================================
# 2. API 키 및 인증
# ==============================================================================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "")
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "finance_llm")
OPENROUTER_DATA_COLLECTION = os.getenv("OPENROUTER_DATA_COLLECTION", "deny").strip().lower()

# ==============================================================================
# 3. LLM 및 파이프라인 상수 설정
# ==============================================================================
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "baai/bge-m3")  # embedding model
GENERATION_MODEL = os.getenv("GENERATION_MODEL", "deepseek/deepseek-v4-flash")

PARENT_CHUNK_SIZE = 2000 # Parent-Child에서 부모 청크 크기
CHILD_CHUNK_SIZE = 500   # Parent-Child에서 자식 청크 크기 (검색용)
CHUNK_SIZE = 1500      # 기본 텍스트 스플리터 청크 최대 글자 수 (일반 모드)
CHUNK_OVERLAP = 150    # 텍스트 스플리터 청크 간 겹치는(Overlap) 글자 수
TEST_LIMIT = 10         # 처리할 파일 수 제한 (0이면 제한 없음)
SEARCH_TOP_K = 20       # FAISS 검색 시 반환할 결과 개수
USE_RERANKER = os.getenv("USE_RERANKER", "false").strip().lower() in {"1", "true", "yes", "on"}   # Reranker를 이용한 문서 재정렬 기능 활성화 여부
RERANK_PROVIDER = os.getenv("RERANK_PROVIDER", "openrouter").strip().lower()
RERANK_MODEL = os.getenv("RERANK_MODEL", "cohere/rerank-v3.5")
RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "60"))
RERANK_CANDIDATE_MULTIPLIER = int(os.getenv("RERANK_CANDIDATE_MULTIPLIER", "3"))
RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "0.15"))
EXTRACTION_ENGINE = "pymupdf" # [marker, pymupdf, opendataloader] - PDF text extraction engine
USE_PARENT_CHILD = True  # Parent-Child Chunking 활성화 여부

# ==============================================================================
# 4. 로깅 설정 (Logging)
# ==============================================================================
import logging

LOG_FILE = os.path.join(BASE_DIR, "logs", "finance_llm.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()  # 콘솔에도 동시 출력
    ]
)

# 서드파티 라이브러리 로깅 레벨 제한 (콘솔 도배 방지)
noisy_loggers = [
    "httpx", "httpcore", "faiss.loader", "faiss", "urllib3",
    "loader", "models", "_client"
]
for logger_name in noisy_loggers:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# ==============================================================================
# 5. 크롤러 설정 (Crawler Settings)
# ==============================================================================
# 'LATEST' (최신 날짜 기준) 또는 'SPECIFIC_DATE' (특정 날짜 지정)
CRAWLER_MODE = os.getenv("CRAWLER_MODE", "LATEST")

# 수집할 리포트 카테고리. 기본값은 company입니다.
# 여러 개는 쉼표로 구분합니다: company,industry,economy
# 전체 수집은 all을 사용합니다.
CRAWLER_CATEGORIES = os.getenv("CRAWLER_CATEGORIES", "company")

# 'SPECIFIC_DATE' 모드일 때 다운로드할 날짜 (YYYY-MM-DD)
# 'LATEST' 모드일 때도 특정 시작 기준일로 사용할 수 있습니다.
CRAWLER_TARGET_DATE = os.getenv("CRAWLER_TARGET_DATE", "2024-03-02")

# 목표 수집 건수. 0이면 건수 목표를 사용하지 않습니다.
CRAWLER_TARGET_COUNT = int(os.getenv("CRAWLER_TARGET_COUNT", "0"))

# 기준일 포함 최근 N+1일을 수집합니다. 예: 7이면 기준일과 이전 7일.
# 0이면 날짜 범위를 확장하지 않습니다.
CRAWLER_LOOKBACK_DAYS = int(os.getenv("CRAWLER_LOOKBACK_DAYS", "0"))

# CRAWLER_TARGET_COUNT가 설정된 경우, 충분한 건수를 찾기 위해 최대 며칠 전까지
# 내려갈지 제한하는 안전장치입니다.
CRAWLER_MAX_LOOKBACK_DAYS = int(os.getenv("CRAWLER_MAX_LOOKBACK_DAYS", "30"))

def get_logger(name: str):
    return logging.getLogger(name)
