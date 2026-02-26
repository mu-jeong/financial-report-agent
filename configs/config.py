import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# 1. 파일 경로 설정
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent

SAVE_DIR = os.path.join(BASE_DIR, "downloaded")
DB_PATH = os.path.join(BASE_DIR, "reports.db")
FAISS_DIR = os.path.join(BASE_DIR, "faiss_db")

# ==============================================================================
# 2. API 키 및 인증
# ==============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ==============================================================================
# 3. LLM 및 파이프라인 상수 설정
# ==============================================================================
EMBEDDING_MODEL = "models/gemini-embedding-001"  # 임베딩용
GENERATION_MODEL = "gemini-2.5-flash"            # 텍스트 생성용 (RAG)

CHUNK_SIZE = 1500      # 텍스트 스플리터 청크 최대 글자 수
CHUNK_OVERLAP = 150    # 텍스트 스플리터 청크 간 겹치는(Overlap) 글자 수
TEST_LIMIT = 10         # 처리할 파일 수 제한 (0이면 제한 없음)
SEARCH_TOP_K = 5       # FAISS 검색 시 반환할 결과 개수
USE_RERANKER = False   # FlashRank를 이용한 문서 재정렬 기능 활성화 여부

# ==============================================================================
# 4. 로깅 설정 (Logging)
# ==============================================================================
import logging

LOG_FILE = os.path.join(BASE_DIR, "finance_llm.log")

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
    "httpx", "httpcore", "google.genai", "google.genai._api_client", 
    "google.genai.models", "google_genai", "google_genai.models", 
    "google_genai._api_client", "faiss.loader", "faiss", "urllib3",
    "loader", "models", "_client"
]
for logger_name in noisy_loggers:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

def get_logger(name: str):
    return logging.getLogger(name)
