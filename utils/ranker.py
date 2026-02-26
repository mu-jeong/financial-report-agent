from configs.config import FAISS_DIR, get_logger

logger = get_logger(__name__)

class RankerSingleton:
    _instance = None
    _ranker = None
    _req_cls = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RankerSingleton, cls).__new__(cls)
        return cls._instance

    def get_ranker(self):
        if self._ranker is None:
            logger.info("⏳ [시스템] Reranking AI 모델을 환경 세팅 중입니다... (최초 1회 다운로드 소요)")
            from flashrank import Ranker, RerankRequest
            self._req_cls = RerankRequest
            self._ranker = Ranker(model_name="ms-marco-MultiBERT-L-12", cache_dir=FAISS_DIR)
            logger.info("✅ 모델 로딩 완료!")
        return self._ranker, self._req_cls

_ranker_singleton = RankerSingleton()

def get_ranker():
    """RankerSingleton을 활용하는 편의 함수"""
    return _ranker_singleton.get_ranker()
