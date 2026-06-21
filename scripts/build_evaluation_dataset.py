"""Build the local evaluation dataset fixture from reports.db.

The fixture is intentionally metadata-only: it stores questions, expected
routing/filtering behavior, source file names, and RDB aggregate expectations,
but never embeds PDF body text.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "reports.db"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "tests" / "fixtures" / "evaluation_dataset.json"

SOURCE_FIELDS = (
    "id",
    "report_type",
    "report_date",
    "target_name",
    "broker",
    "title",
    "file_name",
)

SELECTION_CRITERIA = [
    {
        "id": "local_reproducibility",
        "description": "현재 로컬 reports.db와 FAISS 인덱스에서 재현 가능한 is_embedded=1 리포트만 source fixture로 사용한다.",
    },
    {
        "id": "route_coverage",
        "description": "VectorDB 본문 검색형 질문과 RDB 집계형 질문을 모두 포함해 router 회귀를 볼 수 있게 한다.",
    },
    {
        "id": "filter_coverage",
        "description": "날짜, 기간, report_type, target_name, broker 필터와 '가장 최근' 류의 recency 의도를 분리해서 넣는다.",
    },
    {
        "id": "ranking_challenge",
        "description": "동일 종목·동일 날짜의 복수 증권사 리포트, 복수 산업 리포트, 경제 리포트 묶음을 포함해 retrieval/rerank 비교가 가능하게 한다.",
    },
    {
        "id": "monitoring_metric_relevance",
        "description": "Parsing, chunking, retrieval, rerank, generation model 변경 전후에 관측할 수 있는 source hit, filter hit, count exactness, latency/cost 태그를 붙인다.",
    },
    {
        "id": "privacy_and_size",
        "description": "PDF 본문과 대화 원문을 제외하고 메타데이터와 기대 결과만 저장해 repo fixture로 안전하게 관리한다.",
    },
]

STABILITY_POLICY = {
    "policy": "fixed_baseline_until_change_reason",
    "description": (
        "테스트셋은 한 번 기준선으로 정하면 변경 사유가 생기기 전까지 그대로 유지한다. "
        "최신 리포트가 추가되었다는 이유만으로 자동 갱신하지 않는다."
    ),
    "allowed_change_reasons": [
        "expected source PDF or reports.db row is no longer locally reproducible",
        "Monitoring Mode adds a core metric dimension not covered by existing cases",
        "acceptance criteria changed for parsing/chunking/retrieval/rerank/model evaluation",
        "fixture schema migration is required",
    ],
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise ValueError(f"Query returned no row: {query!r}")
    return row[0]


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def source_rows(
    conn: sqlite3.Connection,
    *,
    where: str,
    params: tuple[Any, ...],
    order_by: str = "report_date DESC, broker, id",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    selected = rows(
        conn,
        f"""
        SELECT {", ".join(SOURCE_FIELDS)}
        FROM reports
        WHERE is_embedded = 1 AND {where}
        ORDER BY {order_by}
        {limit_sql}
        """,
        params,
    )
    if not selected:
        raise ValueError(f"No source rows matched where={where!r}, params={params!r}")
    return selected


def rdb_result(conn: sqlite3.Connection, query: str) -> dict[str, Any]:
    cursor = conn.execute(query)
    columns = [description[0] for description in cursor.description]
    result_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return {
        "columns": columns,
        "rows": result_rows,
    }


def vector_case(
    *,
    case_id: str,
    question: str,
    expected_filters: dict[str, Any],
    expected_sources: list[dict[str, Any]],
    selection_reason: str,
    criteria_tags: list[str],
    monitoring_dimensions: list[str],
    checks: list[str],
) -> dict[str, Any]:
    return {
        "id": case_id,
        "type": "vectordb_retrieval",
        "question": question,
        "expected_route": "vectordb",
        "expected_filters": expected_filters,
        "expected_sources": expected_sources,
        "selection_reason": selection_reason,
        "criteria_tags": criteria_tags,
        "monitoring_dimensions": monitoring_dimensions,
        "checks": checks,
    }


def aggregate_case(
    *,
    case_id: str,
    question: str,
    expected_sql_intent: str,
    expected_result: dict[str, Any],
    selection_reason: str,
    criteria_tags: list[str],
    monitoring_dimensions: list[str],
    checks: list[str],
) -> dict[str, Any]:
    return {
        "id": case_id,
        "type": "rdb_aggregate",
        "question": question,
        "expected_route": "rdb",
        "expected_sql_intent": expected_sql_intent,
        "expected_result": expected_result,
        "selection_reason": selection_reason,
        "criteria_tags": criteria_tags,
        "monitoring_dimensions": monitoring_dimensions,
        "checks": checks,
    }


def build_dataset(conn: sqlite3.Connection) -> dict[str, Any]:
    total_reports = int(scalar(conn, "SELECT COUNT(*) FROM reports"))
    embedded_reports = int(scalar(conn, "SELECT COUNT(*) FROM reports WHERE is_embedded = 1"))
    max_report_date = scalar(
        conn,
        "SELECT MAX(report_date) FROM reports WHERE is_embedded = 1 AND report_date IS NOT NULL",
    )
    min_report_date = scalar(
        conn,
        "SELECT MIN(report_date) FROM reports WHERE is_embedded = 1 AND report_date IS NOT NULL",
    )

    naver_latest = source_rows(
        conn,
        where="report_type = ? AND target_name = ?",
        params=("company", "NAVER"),
        limit=1,
    )
    naver_same_day = source_rows(
        conn,
        where="report_type = ? AND report_date = ? AND target_name = ?",
        params=("company", "2026-06-09", "NAVER"),
        order_by="broker, id",
    )
    ls_latest = source_rows(
        conn,
        where="report_type = ? AND target_name = ?",
        params=("company", "LS"),
        limit=1,
    )
    jyp_latest = source_rows(
        conn,
        where="report_type = ? AND target_name = ?",
        params=("company", "JYP Ent."),
        limit=1,
    )
    semiconductor = source_rows(
        conn,
        where="report_type = ? AND report_date = ? AND target_name = ?",
        params=("industry", "2026-06-18", "반도체"),
    )
    retail_industry = source_rows(
        conn,
        where="report_type = ? AND report_date = ? AND target_name = ?",
        params=("industry", "2026-06-18", "유통"),
        order_by="broker, id",
    )
    economy_latest = source_rows(
        conn,
        where="report_type = ? AND report_date = ?",
        params=("economy", "2026-06-19"),
        order_by="broker, title, id",
    )
    game_industry = source_rows(
        conn,
        where="report_type = ? AND report_date = ? AND target_name = ?",
        params=("industry", "2026-06-19", "게임"),
    )

    cases = [
        vector_case(
            case_id="vector_company_latest_recency_001",
            question="NAVER의 가장 최근 리포트에서 핵심 내용을 요약해줘.",
            expected_filters={"report_type": "company", "target_name": "NAVER"},
            expected_sources=naver_latest,
            selection_reason="고빈도 종목의 최신성 선택이 recency/rerank 변경에 흔들리지 않는지 확인한다.",
            criteria_tags=["local_reproducibility", "filter_coverage", "monitoring_metric_relevance"],
            monitoring_dimensions=["retrieval", "rerank", "recency", "generation_model"],
            checks=["answer_cites_source", "latest_source_selected", "mentions_target_name"],
        ),
        vector_case(
            case_id="vector_company_multi_broker_same_day_001",
            question="2026년 6월 9일 NAVER 리포트들을 증권사별로 비교해줘.",
            expected_filters={
                "report_type": "company",
                "report_date_start": "2026-06-09",
                "report_date_end": "2026-06-09",
                "target_name": "NAVER",
            },
            expected_sources=naver_same_day,
            selection_reason="동일 종목·동일 날짜에 5개 증권사 리포트가 있어 Top-K, 중복 제거, 비교 답변 품질을 보기 좋다.",
            criteria_tags=["local_reproducibility", "filter_coverage", "ranking_challenge"],
            monitoring_dimensions=["retrieval", "rerank", "source_coverage"],
            checks=["multiple_sources", "broker_comparison", "date_filter_applied"],
        ),
        vector_case(
            case_id="vector_company_latin_ticker_001",
            question="LS의 가장 최근 리포트에서 실적 성장과 저가 매수 논리를 요약해줘.",
            expected_filters={"report_type": "company", "target_name": "LS"},
            expected_sources=ls_latest,
            selection_reason="짧은 영문 티커형 target_name은 필터 오탐/누락을 만들기 쉬워 metadata filter 회귀에 유용하다.",
            criteria_tags=["local_reproducibility", "filter_coverage"],
            monitoring_dimensions=["retrieval", "metadata_filter", "generation_model"],
            checks=["answer_cites_source", "target_filter_applied", "mentions_target_name"],
        ),
        vector_case(
            case_id="vector_company_punctuation_target_001",
            question="JYP Ent.의 최신 리포트에서 공연과 MD 수익화 포인트를 정리해줘.",
            expected_filters={"report_type": "company", "target_name": "JYP Ent."},
            expected_sources=jyp_latest,
            selection_reason="마침표와 영문이 섞인 종목명으로 query rewrite 및 metadata filter 정규화 품질을 점검한다.",
            criteria_tags=["local_reproducibility", "filter_coverage"],
            monitoring_dimensions=["query_rewrite", "metadata_filter", "retrieval"],
            checks=["target_filter_applied", "answer_cites_source", "mentions_target_name"],
        ),
        vector_case(
            case_id="vector_industry_semiconductor_exact_001",
            question="2026년 6월 18일 반도체 산업 리포트에서 Rubin Ultra와 HBM4E 변화 포인트를 요약해줘.",
            expected_filters={
                "report_type": "industry",
                "report_date_start": "2026-06-18",
                "report_date_end": "2026-06-18",
                "target_name": "반도체",
            },
            expected_sources=semiconductor,
            selection_reason="산업 리포트의 target_name·날짜·본문 키워드가 모두 명확해 parsing/chunking 변경 비교 기준으로 적합하다.",
            criteria_tags=["local_reproducibility", "route_coverage", "filter_coverage"],
            monitoring_dimensions=["parsing", "chunking", "retrieval"],
            checks=["industry_filter_applied", "date_filter_applied", "answer_cites_source"],
        ),
        vector_case(
            case_id="vector_industry_multi_same_day_001",
            question="2026년 6월 18일 유통 산업 리포트 2개를 비교해줘.",
            expected_filters={
                "report_type": "industry",
                "report_date_start": "2026-06-18",
                "report_date_end": "2026-06-18",
                "target_name": "유통",
            },
            expected_sources=retail_industry,
            selection_reason="같은 날짜의 같은 산업 리포트가 2개라 source recall과 비교형 답변 품질을 함께 볼 수 있다.",
            criteria_tags=["local_reproducibility", "ranking_challenge"],
            monitoring_dimensions=["retrieval", "rerank", "source_coverage"],
            checks=["multiple_sources", "industry_filter_applied", "date_filter_applied"],
        ),
        vector_case(
            case_id="vector_economy_latest_macro_001",
            question="2026년 6월 19일 매크로 리포트들을 바탕으로 하반기 미국 경제와 환율 이슈를 정리해줘.",
            expected_filters={
                "report_type": "economy",
                "report_date_start": "2026-06-19",
                "report_date_end": "2026-06-19",
            },
            expected_sources=economy_latest,
            selection_reason="target_name이 null인 경제 리포트 묶음으로 report_type/date 필터만으로 검색해야 하는 경로를 검증한다.",
            criteria_tags=["local_reproducibility", "route_coverage", "ranking_challenge"],
            monitoring_dimensions=["retrieval", "rerank", "source_coverage"],
            checks=["economy_filter_applied", "multiple_sources", "date_filter_applied"],
        ),
        vector_case(
            case_id="vector_industry_game_single_001",
            question="2026년 6월 19일 게임 산업 리포트의 핵심 내용을 요약해줘.",
            expected_filters={
                "report_type": "industry",
                "report_date_start": "2026-06-19",
                "report_date_end": "2026-06-19",
                "target_name": "게임",
            },
            expected_sources=game_industry,
            selection_reason="최신 일자의 단일 산업 리포트로 정확 필터와 단일 source citation 회귀를 확인한다.",
            criteria_tags=["local_reproducibility", "filter_coverage"],
            monitoring_dimensions=["retrieval", "generation_model"],
            checks=["single_source", "industry_filter_applied", "answer_cites_source"],
        ),
        aggregate_case(
            case_id="rdb_count_latest_date_001",
            question="2026년 6월 19일 등록된 리포트는 총 몇 건이야?",
            expected_sql_intent="count_reports_by_exact_date",
            expected_result=rdb_result(
                conn,
                "SELECT COUNT(*) AS count FROM reports WHERE report_date = '2026-06-19'",
            ),
            selection_reason="최신 스냅샷 일자의 단순 count로 router와 SQL guardrail의 기본 회귀를 확인한다.",
            criteria_tags=["local_reproducibility", "route_coverage"],
            monitoring_dimensions=["rdb", "router", "latency"],
            checks=["read_only_sql", "reports_table_only", "exact_count"],
        ),
        aggregate_case(
            case_id="rdb_count_by_report_type_001",
            question="현재 DB에서 리포트 유형별 건수를 알려줘.",
            expected_sql_intent="count_reports_grouped_by_report_type",
            expected_result=rdb_result(
                conn,
                "SELECT report_type, COUNT(*) AS count FROM reports GROUP BY report_type ORDER BY report_type",
            ),
            selection_reason="company/industry/economy 전체 분포를 고정해 데이터 상태 대시보드의 기준 수치로 쓴다.",
            criteria_tags=["local_reproducibility", "route_coverage"],
            monitoring_dimensions=["rdb", "data_readiness"],
            checks=["read_only_sql", "group_by_report_type", "exact_counts"],
        ),
        aggregate_case(
            case_id="rdb_calendar_latest_week_001",
            question="2026년 6월 15일부터 6월 19일까지 날짜별 리포트 수를 알려줘.",
            expected_sql_intent="count_reports_by_date_range",
            expected_result=rdb_result(
                conn,
                """
                SELECT report_date, COUNT(*) AS count
                FROM reports
                WHERE report_date BETWEEN '2026-06-15' AND '2026-06-19'
                GROUP BY report_date
                ORDER BY report_date
                """,
            ),
            selection_reason="날짜 범위 calendar 집계는 데이터 업데이트/누락 진단 화면의 핵심 지표다.",
            criteria_tags=["local_reproducibility", "filter_coverage", "monitoring_metric_relevance"],
            monitoring_dimensions=["rdb", "data_calendar", "data_readiness"],
            checks=["date_grouping", "calendar_data", "exact_counts"],
        ),
        aggregate_case(
            case_id="rdb_top_brokers_001",
            question="현재 DB에서 리포트가 가장 많은 증권사 상위 5곳을 알려줘.",
            expected_sql_intent="top_brokers_by_report_count",
            expected_result=rdb_result(
                conn,
                """
                SELECT broker, COUNT(*) AS count
                FROM reports
                GROUP BY broker
                ORDER BY count DESC, broker
                LIMIT 5
                """,
            ),
            selection_reason="broker별 분포는 편향된 retrieval/rerank 결과를 해석할 때 배경 지표로 필요하다.",
            criteria_tags=["local_reproducibility", "monitoring_metric_relevance"],
            monitoring_dimensions=["rdb", "corpus_distribution"],
            checks=["read_only_sql", "order_by_count_desc", "limit_5"],
        ),
        aggregate_case(
            case_id="rdb_embedding_readiness_001",
            question="현재 DB에서 임베딩 완료와 미완료 리포트 수를 알려줘.",
            expected_sql_intent="count_embedded_and_pending_reports",
            expected_result=rdb_result(
                conn,
                """
                SELECT
                  SUM(CASE WHEN is_embedded = 1 THEN 1 ELSE 0 END) AS embedded,
                  SUM(CASE WHEN is_embedded = 0 THEN 1 ELSE 0 END) AS pending,
                  COUNT(*) AS total
                FROM reports
                """,
            ),
            selection_reason="Monitoring Mode의 데이터 준비 상태/검색 가능 여부 대시보드를 보호하는 기준 집계다.",
            criteria_tags=["local_reproducibility", "monitoring_metric_relevance"],
            monitoring_dimensions=["rdb", "data_readiness", "monitoring_dashboard"],
            checks=["read_only_sql", "embedding_status_counts", "exact_counts"],
        ),
        aggregate_case(
            case_id="rdb_company_month_count_001",
            question="2026년 6월에 등록된 NAVER 기업 리포트는 몇 건이야?",
            expected_sql_intent="count_company_reports_for_target_in_month",
            expected_result=rdb_result(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM reports
                WHERE report_type = 'company'
                  AND target_name = 'NAVER'
                  AND report_date BETWEEN '2026-06-01' AND '2026-06-30'
                """,
            ),
            selection_reason="특정 종목+월+report_type 조합은 VectorDB 필터와 RDB 집계의 경계 회귀에 유용하다.",
            criteria_tags=["local_reproducibility", "filter_coverage", "route_coverage"],
            monitoring_dimensions=["router", "rdb", "metadata_filter"],
            checks=["read_only_sql", "target_filter", "month_filter", "exact_count"],
        ),
    ]

    return {
        "name": "finance_llm_local_eval_dataset",
        "version": 2,
        "generated_from": {
            "database": "data/reports.db",
            "table": "reports",
            "snapshot_date": max_report_date,
            "source_row_count": total_reports,
            "embedded_row_count": embedded_reports,
            "min_report_date": min_report_date,
            "max_report_date": max_report_date,
        },
        "description": (
            "현재 로컬 reports DB의 메타데이터에서 뽑은 Finance LLM 평가용 테스트셋입니다. "
            "PDF 본문은 포함하지 않고, 질문/기대 라우팅/기대 필터/기대 출처 파일명/RDB 기대 집계값만 포함합니다."
        ),
        "stability_policy": STABILITY_POLICY,
        "selection_criteria": SELECTION_CRITERIA,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build tests/fixtures/evaluation_dataset.json")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    with connect(args.db_path) as conn:
        dataset = build_dataset(conn)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(dataset['cases'])} cases to {args.output}")


if __name__ == "__main__":
    main()
