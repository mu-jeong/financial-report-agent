"""
db_manager.py
-------------
SQLite를 이용해 다운로드된 PDF 리포트 정보를 관리하는 모듈.

테이블 스키마:
    reports (
        id          INTEGER  PRIMARY KEY AUTOINCREMENT,
        report_type TEXT     NOT NULL,        -- 'company', 'industry', 'economy'
        report_date TEXT     NOT NULL,        -- 'YYYY-MM-DD'
        target_name TEXT,                     -- 종목명, 산업분류 등 (경제 리포트는 null/empty)
        title       TEXT     NOT NULL,        -- 리포트 제목
        broker      TEXT     NOT NULL,        -- 증권사
        file_name   TEXT     NOT NULL UNIQUE, -- 실제 파일명 (중복 방지 / Vector DB 연결 키)
                                              -- file_path는 os.path.join(save_dir, file_name) 으로 재조합
        is_embedded INTEGER  NOT NULL DEFAULT 0  -- 0: 미처리 / 1: Vector DB 임베딩 완료
    )

Vector DB 연동 흐름:
    1. 크롤러가 PDF 다운로드 → is_embedded=0 으로 INSERT
    2. LLM 파이프라인이 fetch_unembedded() 로 미처리 항목 조회
    3. os.path.join(save_dir, row["file_name"]) 으로 경로 재조합 → PDF 로딩
    4. 텍스트 추출 → 임베딩 생성 → Vector DB 저장
    5. mark_embedded(file_name) 호출 → is_embedded=1 로 업데이트
"""

import os
import sys
import sqlite3
from datetime import datetime

# 프로젝트 루트 경로를 참조할 수 있도록 설정
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.configs.config import DB_PATH, SAVE_DIR, get_logger

logger = get_logger(__name__)


def get_connection() -> sqlite3.Connection:
    """DB 커넥션 반환. Row를 dict처럼 접근 가능하도록 설정."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """테이블이 없으면 생성 (멱등 실행 가능)."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                report_type TEXT     NOT NULL,
                report_date TEXT     NOT NULL,
                target_name TEXT,
                title       TEXT     NOT NULL,
                broker      TEXT     NOT NULL,
                file_name   TEXT     NOT NULL UNIQUE,
                is_embedded INTEGER  NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    logger.info(f"[DB] 초기화 완료 → {DB_PATH}")


# ── 파일명 파싱 ───────────────────────────────────────────────────────────────

def parse_filename(file_name: str) -> dict | None:
    """
    파일명 '[유형]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf' 을 파싱해 dict 반환.
    파싱 실패 시 None 반환.
    """
    if not file_name.lower().endswith(".pdf"):
        return None

    name_without_ext = file_name[:-4]
    
    # 5개의 파트로 나눔: 종류, 날짜, 대상, 증권사, 제목
    parts = name_without_ext.split("_", 4)

    if len(parts) < 5:
        # 기존 종목명 포맷 등 하위호환성이 필요할 수 있으나, 현재 규칙상 5개 부문 필수
        return None

    r_type, date_str, target_name, broker, title = parts[0], parts[1], parts[2], parts[3], parts[4]

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

    return {
        "report_type": r_type, 
        "report_date": date_str, 
        "target_name": target_name, 
        "broker": broker,
        "title": title
    }


# ── 쓰기 ─────────────────────────────────────────────────────────────────────

def upsert_report(file_name: str) -> bool:
    """
    PDF 파일명을 파싱하여 DB에 INSERT (이미 있으면 무시).
    성공적으로 삽입되면 True, 중복이면 False 반환.
    file_path는 저장하지 않고 필요 시 os.path.join(save_dir, file_name) 으로 재조합.
    """
    parsed = parse_filename(file_name)
    if not parsed:
        logger.warning(f"[DB] ⚠️  파싱 실패, 건너뜀: {file_name}")
        return False

    with get_connection() as conn:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO reports
                    (report_type, report_date, target_name, title, broker, file_name)
                VALUES
                    (:report_type, :report_date, :target_name, :title, :broker, :file_name)
                """,
                {**parsed, "file_name": file_name},
            )
            conn.commit()
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            return inserted > 0
        except sqlite3.Error as e:
            logger.error(f"[DB] ❌ 오류 발생: {e}")
            return False


def mark_embedded(file_name: str) -> None:
    """Vector DB 임베딩 완료 후 호출 — is_embedded 를 1로 업데이트."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE reports SET is_embedded = 1 WHERE file_name = ?",
            (file_name,),
        )
        conn.commit()


def sync_from_directory(directory: str = SAVE_DIR) -> None:
    """
    지정 폴더의 PDF를 전부 스캔하여 DB에 동기화.
    이미 등록된 파일은 INSERT OR IGNORE로 건너뜀.
    잦은 연결을 방지하기 위해 단일 커넥션으로 executemany(Batch) 처리합니다.
    """
    if not os.path.isdir(directory):
        logger.warning(f"[DB] 폴더를 찾을 수 없습니다: {directory}")
        return

    pdf_files = [f for f in os.listdir(directory) if f.lower().endswith(".pdf")]
    logger.info(f"[DB] {len(pdf_files)}개의 PDF 발견 → 동기화 시작")

    parsed_list = []
    for file_name in sorted(pdf_files):
        parsed = parse_filename(file_name)
        if parsed:
            parsed['file_name'] = file_name
            parsed_list.append(parsed)
        else:
            logger.warning(f"[DB] ⚠️  파싱 실패, 건너뜀: {file_name}")

    if not parsed_list:
        logger.info("[DB] 동기화할 유효한 파일이 없습니다.")
        return

    with get_connection() as conn:
        try:
            # 삽입 전 데이터 개수 확인
            before_count = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            
            conn.executemany(
                """
                INSERT OR IGNORE INTO reports
                    (report_type, report_date, target_name, title, broker, file_name)
                VALUES
                    (:report_type, :report_date, :target_name, :title, :broker, :file_name)
                """,
                parsed_list
            )
            conn.commit()
            
            # 삽입 후 데이터 개수 확인
            after_count = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            inserted = after_count - before_count
            skipped = len(parsed_list) - inserted
            logger.info(f"[DB] 동기화 완료 — 신규: {inserted}건 / 중복 스킵: {skipped}건")
        except sqlite3.Error as e:
            logger.error(f"[DB] ❌ DB 배치 동기화 중 오류 발생: {e}")


# ── 조회 ─────────────────────────────────────────────────────────────────────

def fetch_all(order_by: str = "report_date DESC") -> list[sqlite3.Row]:
    """전체 리포트 목록 조회."""
    with get_connection() as conn:
        return conn.execute(
            f"SELECT * FROM reports ORDER BY {order_by}"
        ).fetchall()


def fetch_unembedded() -> list[sqlite3.Row]:
    """임베딩이 안 된 리포트만 조회 — LLM 파이프라인 진입점."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM reports WHERE is_embedded = 0 ORDER BY report_date DESC"
        ).fetchall()


def fetch_by_target(target_name: str) -> list[sqlite3.Row]:
    """대상명(종목명/산업분류 등)으로 조회 (부분 일치)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM reports WHERE target_name LIKE ? ORDER BY report_date DESC",
            (f"%{target_name}%",),
        ).fetchall()


def fetch_by_date_range(start: str, end: str) -> list[sqlite3.Row]:
    """날짜 범위로 조회 (YYYY-MM-DD 형식)."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM reports
            WHERE report_date BETWEEN ? AND ?
            ORDER BY report_date DESC
            """,
            (start, end),
        ).fetchall()


# ── 단독 실행 시 동기화 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    sync_from_directory(SAVE_DIR)

    print("\n[미리보기] 최근 등록된 리포트 5건:")
    for row in fetch_all()[:5]:
        print(f"  {row['report_type']} | {row['report_date']} | {row['target_name']} | {row['broker']} | {row['title'][:30]}")

    unembedded = fetch_unembedded()
    print(f"\n[Vector DB 대기 중] 미처리 리포트: {len(unembedded)}건")
