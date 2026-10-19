import hashlib
import os
import re
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
import sys
from urllib.parse import urlparse

# 프로젝트 루트 경로를 참조할 수 있도록 설정
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.retrieval.update_lock import RetrievalUpdateLock
from src.retrieval.identity import normalize_relative_path

REPORT_CATEGORY_URLS = {
    "company": "/api/stockSecurity/researches/v2/company",
    "industry": "/api/stockSecurity/researches/v2/industry",
    "economy": "/api/stockSecurity/researches/v2/economy",
}

NAVER_REQUEST_TIMEOUT_SECONDS = 30
NAVER_RESEARCH_PAGE_SIZE = 50
_WINDOWS_FILENAME_LIMIT = 255
_WINDOWS_NORMAL_PATH_LIMIT = 259
_POSIX_FILENAME_LIMIT_FALLBACK = 255
_POSIX_PATH_LIMIT_FALLBACK = 4095
_TRUNCATED_TITLE_HASH_LENGTH = 12
_IS_WINDOWS = os.name == "nt"


def _path_length_limits(directory: str | os.PathLike[str]) -> tuple[int, int]:
    """Return conservative filename-component and full-path limits."""

    if _IS_WINDOWS:
        return _WINDOWS_FILENAME_LIMIT, _WINDOWS_NORMAL_PATH_LIMIT

    def read_limit(name: str, fallback: int, *, includes_nul: bool = False) -> int:
        try:
            value = os.pathconf(directory, name)
        except (AttributeError, OSError, ValueError):
            return fallback
        if not isinstance(value, int) or value <= 0:
            return fallback
        return value - 1 if includes_nul else value

    return (
        read_limit("PC_NAME_MAX", _POSIX_FILENAME_LIMIT_FALLBACK),
        read_limit("PC_PATH_MAX", _POSIX_PATH_LIMIT_FALLBACK, includes_nul=True),
    )


def _filename_fits(
    directory: str | os.PathLike[str],
    file_name: str,
) -> bool:
    component_limit, path_limit = _path_length_limits(directory)
    full_path = os.path.abspath(os.path.join(directory, file_name))
    if _IS_WINDOWS:
        component_length = len(file_name.encode("utf-16-le")) // 2
        path_length = len(full_path.encode("utf-16-le")) // 2
    else:
        component_length = len(os.fsencode(file_name))
        path_length = len(os.fsencode(full_path))
    return component_length <= component_limit and path_length <= path_limit


def _report_file_path(
    directory: str | os.PathLike[str],
    prefix: str,
    title: str,
    *,
    registered_paths: frozenset[str] = frozenset(),
) -> str:
    """Preserve registered paths even when their PDF needs to be downloaded again."""

    legacy_title = title[:50] + ("..." if len(title) > 50 else "")
    legacy_name = f"{prefix}{legacy_title}.pdf"
    legacy_path = os.path.join(directory, legacy_name)
    # SAVE_DIR can be overridden; retrieval still uses the logical 'downloaded'
    # prefix. Compare canonical identities without changing physical spelling.
    if normalize_relative_path(f"downloaded/{legacy_name}") in registered_paths:
        return legacy_path

    new_path = _new_report_file_path(directory, prefix, title)
    if normalize_relative_path(f"downloaded/{Path(new_path).name}") in registered_paths:
        return new_path
    if os.path.exists(legacy_path):
        return legacy_path
    return new_path


def _new_report_file_path(
    directory: str | os.PathLike[str],
    prefix: str,
    title: str,
) -> str:
    """Fit new titles to filesystem limits with a stable hash when truncated."""

    full_name = f"{prefix}{title}.pdf"
    if _filename_fits(directory, full_name):
        return os.path.join(directory, full_name)

    digest = hashlib.sha256(title.encode("utf-8", errors="surrogatepass")).hexdigest()
    suffix = f"~{digest[:_TRUNCATED_TITLE_HASH_LENGTH]}"
    minimal_name = f"{prefix}{suffix}.pdf"
    if not _filename_fits(directory, minimal_name):
        raise ValueError(
            "Report filename metadata does not fit the filesystem path limit: "
            f"{os.path.abspath(directory)}"
        )

    low = 0
    high = len(title)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = f"{prefix}{title[:midpoint]}{suffix}.pdf"
        if _filename_fits(directory, candidate):
            low = midpoint
        else:
            high = midpoint - 1

    return os.path.join(directory, f"{prefix}{title[:low]}{suffix}.pdf")


def _is_valid_pdf_file(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* contains a readable, non-empty PDF document."""

    import fitz

    try:
        with open(path, "rb") as source_file:
            if source_file.read(5) != b"%PDF-":
                return False
        with fitz.open(filename=str(path), filetype="pdf") as document:
            return document.page_count > 0 and not document.needs_pass
    except (OSError, RuntimeError, ValueError):
        return False


def _save_pdf_atomically(
    destination: str | os.PathLike[str],
    content: bytes,
) -> None:
    """Validate and atomically publish downloaded PDF bytes."""

    destination_path = Path(destination)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_path.parent,
            prefix=".report-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if not _is_valid_pdf_file(temp_path):
            raise ValueError("응답이 유효한 PDF 문서가 아닙니다.")

        os.replace(temp_path, destination_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def guard_before_report_download():
    """Fail closed before crawler dependencies, network, or source writes start."""

    from src.configs.config import DATA_ROOT
    from src.retrieval.runtime_guard import guard_before_retrieval_write

    return guard_before_retrieval_write(
        DATA_ROOT,
        allow_empty_preflight=True,
    )


def normalize_report_categories(categories: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize crawler category selection.

    Args:
        categories: Comma-separated string or iterable of category names.
            Supported values are company, industry, economy, and all.

    Returns:
        Ordered list of category names. Defaults to ["company"].
    """
    if categories is None or categories == "":
        raw_categories = ["company"]
    elif isinstance(categories, str):
        raw_categories = [part.strip().lower() for part in categories.split(",")]
    else:
        raw_categories = [str(part).strip().lower() for part in categories]

    raw_categories = [category for category in raw_categories if category]
    if not raw_categories:
        return ["company"]
    if "all" in raw_categories:
        return list(REPORT_CATEGORY_URLS)

    selected: list[str] = []
    invalid: list[str] = []
    for category in raw_categories:
        if category not in REPORT_CATEGORY_URLS:
            invalid.append(category)
            continue
        if category not in selected:
            selected.append(category)

    if invalid:
        allowed = ", ".join([*REPORT_CATEGORY_URLS, "all"])
        raise ValueError(f"Unsupported report categories: {', '.join(invalid)}. Allowed: {allowed}")

    return selected or ["company"]


def _parse_target_date(target_date_str: str | None) -> date | None:
    if not target_date_str:
        return None
    return datetime.strptime(target_date_str, "%Y-%m-%d").date()


def _crawl_start_date(end_date: date, lookback_days: int, target_count: int, max_lookback_days: int) -> date:
    """Resolve the oldest date to scan inclusively.

    - lookback_days explicitly means "collect this many previous days too".
    - target_count mode needs a safety window even when lookback_days is 0, so
      it can keep going past the first available report date until enough rows
      are collected.
    """
    effective_lookback = max(lookback_days, max_lookback_days if target_count > 0 else 0)
    return end_date - timedelta(days=effective_lookback)


def classify_report_date(report_date: date, start_date: date, end_date: date) -> str:
    """Classify a report date against the inclusive crawl window.

    Returns:
        - "skip_newer": keep scanning; the row is newer than the requested end.
        - "process": download/count the row.
        - "stop_older": stop this category; later pages will be even older.
    """
    if report_date > end_date:
        return "skip_newer"
    if report_date < start_date:
        return "stop_older"
    return "process"


def download_naver_reports(
    target_date_str=None,
    target_count: int = 0,
    lookback_days: int = 0,
    max_lookback_days: int = 30,
    categories: str | list[str] | tuple[str, ...] | None = None,
):
    """Download reports while holding the native retrieval update fence."""

    from src.configs.config import DATA_ROOT

    with RetrievalUpdateLock(Path(DATA_ROOT)):
        guard_before_report_download()
        from src.core.report_corpus import list_active_source_paths

        registered_paths = list_active_source_paths(data_root=DATA_ROOT)
        return _download_naver_reports_locked(
            target_date_str,
            target_count=target_count,
            lookback_days=lookback_days,
            max_lookback_days=max_lookback_days,
            categories=categories,
            registered_paths=registered_paths,
        )


def _download_naver_reports_locked(
    target_date_str=None,
    target_count: int = 0,
    lookback_days: int = 0,
    max_lookback_days: int = 30,
    categories: str | list[str] | tuple[str, ...] | None = None,
    *,
    registered_paths: frozenset[str] = frozenset(),
):
    import requests

    total_processed = 0
    failed_download_count = 0
    first_download_error: str | None = None
    base_url = "https://stock.naver.com"
    stop_all_categories = False
    target_count = max(0, int(target_count or 0))
    lookback_days = max(0, int(lookback_days or 0))
    max_lookback_days = max(lookback_days, int(max_lookback_days or 0))
    
    selected_categories = normalize_report_categories(categories)

    # 선택된 리포트 유형별 URL 및 카테고리 정의
    # (카테고리명, URL)
    report_categories = [
        (category, base_url + REPORT_CATEGORY_URLS[category])
        for category in selected_categories
    ]
    print(f"수집 카테고리: {', '.join(selected_categories)}")

    def sanitize(text: str) -> str:
        """Windows 파일명에 사용할 수 없는 특수문자 제거 및 길이 제한"""
        text = re.sub(r'[\\/:*?"<>|]', '', text)  # 금지 문자 제거
        text = text.strip()                          # 양쪽 공백 제거
        return text
    
    # 봇 차단 방지를 위한 User-Agent 설정
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 다운로드한 PDF를 저장할 폴더 생성
    from src.configs.config import SAVE_DIR
    save_dir = SAVE_DIR
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # 날짜 설정 로직
    target_date = _parse_target_date(target_date_str)
    start_date = None
    if target_date_str:
        start_date = _crawl_start_date(target_date, lookback_days, target_count, max_lookback_days)
        print(f"[{start_date} ~ {target_date}] 범위의 리포트를 다운로드합니다.")
    else:
        print("작성일을 지정하지 않아 '가장 최근 날짜'의 리포트만 다운로드합니다.")

    if target_count > 0:
        print(f"목표 처리 건수: {target_count}건")
    if lookback_days > 0:
        print(f"명시 수집 기간: 기준일 포함 최근 {lookback_days + 1}일")

    # 가장 최근 날짜를 추적하기 위한 변수
    global_latest_date = None

    for r_type, list_url in report_categories:
        if stop_all_categories:
            break

        print(f"\n==========================================")
        print(f"👉 탐색 시작: {r_type.upper()} ({list_url})")
        print(f"==========================================")
        
        page = 0
        stop_crawling = False
        seen_report_ids: set[str] = set()
        
        while not stop_crawling:
            # print(f"--- {page}페이지 탐색 중 ---")
            params: dict[str, int | str] = {
                "index": page,
                "size": NAVER_RESEARCH_PAGE_SIZE,
            }
            if isinstance(target_date, date) and isinstance(start_date, date):
                params.update(
                    startDate=start_date.isoformat(),
                    endDate=target_date.isoformat(),
                )
            res = requests.get(
                list_url,
                headers=headers,
                params=params,
                timeout=NAVER_REQUEST_TIMEOUT_SECONDS,
            )
            res.raise_for_status()
            try:
                payload = res.json()
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Naver research list returned invalid JSON: {r_type} page {page}"
                ) from exc

            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Naver research list returned an invalid object: {r_type} page {page}"
                )
            rows = payload.get("items")
            has_next = payload.get("hasNext")
            if not isinstance(rows, list) or not isinstance(has_next, bool):
                raise RuntimeError(
                    f"Naver research list schema changed: {r_type} page {page}"
                )
            if not rows and has_next:
                raise RuntimeError(
                    f"Naver research pagination made no progress: {r_type} page {page}"
                )
            new_report_ids = 0
            
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError(
                        f"Naver research item schema changed: {r_type} page {page}"
                    )
                report_id = str(row.get("nid") or "").strip()
                if not report_id:
                    raise RuntimeError(
                        f"Naver research item is missing nid: {r_type} page {page}"
                    )
                if report_id in seen_report_ids:
                    continue
                seen_report_ids.add(report_id)
                new_report_ids += 1
                
                date_text = str(row.get("writeDate") or "").strip()
                    
                try:
                    # 새 연구 API는 ISO 날짜(YYYY-MM-DD)를 반환한다.
                    report_date = datetime.strptime(date_text, "%Y-%m-%d").date()
                except ValueError as exc:
                    raise RuntimeError(
                        f"Naver research item has an invalid writeDate: {r_type}/{report_id}"
                    ) from exc
                
                # 타겟 날짜가 명시되지 않은 경우, 첫 번째로 발견한 게시물의 날짜를 기준일로 설정
                if not target_date_str and global_latest_date is None:
                    global_latest_date = report_date
                    target_date = global_latest_date
                    start_date = _crawl_start_date(target_date, lookback_days, target_count, max_lookback_days)
                    print(f"[{start_date} ~ {target_date}] 범위의 리포트를 다운로드합니다.")
                
                if not isinstance(target_date, date) or not isinstance(start_date, date):
                    continue

                date_action = classify_report_date(report_date, start_date, target_date)
                if date_action == "skip_newer":
                    continue
                if date_action == "stop_older":
                    stop_crawling = True
                    break

                # 타겟명, 제목, 증권사 추출
                if r_type == "company":
                    target_name = str(row.get("itemName") or "").strip()
                elif r_type == "industry":
                    target_name = str(row.get("industryKoreanName") or "").strip()
                elif r_type == "economy":
                    target_name = "null"  # 경제는 타겟이 없음
                title_text = str(row.get("title") or "").strip()
                broker = str(row.get("brokerName") or "").strip()
                if not title_text or not broker or (r_type != "economy" and not target_name):
                    raise RuntimeError(
                        f"Naver research item is missing filename metadata: {r_type}/{report_id}"
                    )

                # 파일명 규칙: '[유형]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf'
                # 언더스코어(_)를 파싱 토큰으로 쓰기 때문에, 각 데이터 내의 언더스코어는 하이픈(-)으로 치환
                s_type = sanitize(r_type).replace('_', '-')
                s_target = sanitize(target_name).replace('_', '-')
                s_broker = sanitize(broker).replace('_', '-')
                s_title = sanitize(title_text).replace('_', '-')
                
                # 기존 파일명은 유지하고, 새 파일은 OS 경로 한도까지 제목을 보존
                file_prefix = f"{s_type}_{report_date}_{s_target}_{s_broker}_"
                try:
                    file_path = _report_file_path(
                        save_dir, file_prefix, s_title, registered_paths=registered_paths,
                    )
                except ValueError as exc:
                    failed_download_count += 1
                    if first_download_error is None:
                        first_download_error = f"{file_prefix}: {exc}"
                    print(f"  Report filename failed: {exc}")
                    continue
                file_name = os.path.basename(file_path)

                # 유효한 기존 파일만 중복 다운로드 대상으로 인정
                if not _is_valid_pdf_file(file_path):
                    print(f"  ✅ 다운로드: [{s_type}|{s_broker}] {s_target[:10]} - {s_title[:30]}")
                    try:
                        detail_res = requests.get(
                            f"{list_url}/{report_id}",
                            headers=headers,
                            timeout=NAVER_REQUEST_TIMEOUT_SECONDS,
                        )
                        detail_res.raise_for_status()
                        try:
                            detail = detail_res.json()
                        except (TypeError, ValueError) as exc:
                            raise ValueError("상세 API가 올바른 JSON을 반환하지 않았습니다.") from exc
                        if not isinstance(detail, dict):
                            raise ValueError("상세 API 응답 형식이 올바르지 않습니다.")
                        pdf_url = detail.get("attachUrl")
                        parsed_pdf_url = urlparse(pdf_url) if isinstance(pdf_url, str) else None
                        if (
                            parsed_pdf_url is None
                            or parsed_pdf_url.scheme not in {"http", "https"}
                            or not parsed_pdf_url.netloc
                        ):
                            raise ValueError("상세 API 응답에 유효한 첨부 URL이 없습니다.")
                        pdf_res = requests.get(
                            pdf_url,
                            headers=headers,
                            timeout=NAVER_REQUEST_TIMEOUT_SECONDS,
                        )
                        pdf_res.raise_for_status()
                        _save_pdf_atomically(file_path, pdf_res.content)
                        total_processed += 1
                    except Exception as e:
                        failed_download_count += 1
                        if first_download_error is None:
                            first_download_error = f"{file_name}: {e}"
                        print(f"  ❌ 다운로드 실패: {e}")
                else:
                    print(f"  ⏭ 이미 존재: {file_name}")
                    total_processed += 1

                if target_count > 0 and total_processed >= target_count:
                    print(f"  🎯 목표 처리 건수 도달: {total_processed}/{target_count}건")
                    stop_crawling = True
                    stop_all_categories = True
                    break

            # API가 더 이상 페이지를 제공하지 않으면 종료한다.
            if stop_crawling or not has_next:
                break
            if new_report_ids == 0:
                raise RuntimeError(
                    f"Naver research pagination made no progress: {r_type} page {page}"
                )
                
            page += 1  # 다음 페이지로 이동

    if failed_download_count:
        print(
            "⚠️ 리포트 다운로드가 일부 실패했습니다. 정상 파일 처리는 계속합니다. "
            f"(실패: {failed_download_count}건, 성공 또는 기존 파일: {total_processed}건, "
            f"첫 오류: {first_download_error})"
        )

    # Stable subprocess summary: item failures are distinct from fatal scan errors.
    print(f"Naver research download complete: processed={total_processed} failed={failed_download_count}")
    if not failed_download_count:
        print(f"\n✅ 모든 카테고리 다운로드가 완료되었습니다. (처리된 리포트: {total_processed}건)")
    return total_processed

# ==========================================
# 실행 부분
# ==========================================

if __name__ == "__main__":
    guard_before_report_download()
    from src.configs.config import (
        CRAWLER_CATEGORIES,
        CRAWLER_LOOKBACK_DAYS,
        CRAWLER_MAX_LOOKBACK_DAYS,
        CRAWLER_MODE,
        CRAWLER_TARGET_COUNT,
        CRAWLER_TARGET_DATE,
    )
    from datetime import timedelta, timezone, datetime

    if CRAWLER_MODE == 'SPECIFIC_DATE':
        print(f"\n[System] 🔍 지정된 날짜({CRAWLER_TARGET_DATE}) 기준 리포트 탐색 중...")
        processed_count = download_naver_reports(
            CRAWLER_TARGET_DATE,
            target_count=CRAWLER_TARGET_COUNT,
            lookback_days=CRAWLER_LOOKBACK_DAYS,
            max_lookback_days=CRAWLER_MAX_LOOKBACK_DAYS,
            categories=CRAWLER_CATEGORIES,
        )
        if processed_count > 0:
            print(f"\n[System] 🎉 {CRAWLER_TARGET_DATE} 일자의 데이터 {processed_count}건을 성공적으로 받아왔습니다!")
        else:
            print(f"\n[System] ⚠️ {CRAWLER_TARGET_DATE} 일자에는 데이터가 없습니다.")
    else:
        # LATEST 모드: 오늘 날짜부터 시작해서 데이터가 발견될 때까지 하루씩 뒤로 감
        KST = timezone(timedelta(hours=9))
        current_date = datetime.now(KST).date()

        print(f"[System] KST 기준 오늘 날짜: {current_date}")

        if CRAWLER_TARGET_COUNT > 0 or CRAWLER_LOOKBACK_DAYS > 0:
            target_date_str = current_date.strftime("%Y-%m-%d")
            print(f"\n[System] 🔍 {target_date_str} 기준 리포트 탐색 중...")
            processed_count = download_naver_reports(
                target_date_str,
                target_count=CRAWLER_TARGET_COUNT,
                lookback_days=CRAWLER_LOOKBACK_DAYS,
                max_lookback_days=CRAWLER_MAX_LOOKBACK_DAYS,
                categories=CRAWLER_CATEGORIES,
            )
            print(f"\n[System] ✅ 처리된 데이터: {processed_count}건")
            sys.exit(0)
        
        while True:
            target_date_str = current_date.strftime("%Y-%m-%d")
            print(f"\n[System] 🔍 {target_date_str} 기준 리포트 탐색 중...")
            
            processed_count = download_naver_reports(
                target_date_str,
                categories=CRAWLER_CATEGORIES,
            )
            
            if processed_count > 0:
                print(f"\n[System] 🎉 {target_date_str} 일자의 데이터 {processed_count}건을 성공적으로 받아왔습니다! 크롤링을 종료합니다.")
                break
            else:
                print(f"\n[System] ⚠️ {target_date_str} 일자에는 데이터가 없습니다. 전날로 넘어가서 다시 시도합니다.")
                current_date -= timedelta(days=1)
