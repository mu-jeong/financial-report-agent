"""One-click Quick Start launcher for non-developer users.

This script intentionally avoids third-party imports until after dependency
installation so it can run on a fresh Windows machine with only Python present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.configs.settings import quickstart_env_updates

VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
REQUIREMENTS_PATH = ROOT / "requirements.txt"

API_KEY_PLACEHOLDERS = {
    "",
    "your_openrouter_api_key_here",
    "sk-or-v1-your-key",
    "sk-or-v1-your-key-here",
}
QUICKSTART_PROGRESS_STEPS = 10
RUNTIME_DIRS = [
    ROOT / "logs",
    ROOT / "data",
    ROOT / "data" / "downloaded",
    ROOT / "reports",
]


def _configure_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def print_step(message: str) -> None:
    print(f"\n[Quick Start] {message}", flush=True)


class ProgressTracker:
    """Small dependency-free progress bar for the one-click launcher."""

    def __init__(self, total: int, *, width: int = 28) -> None:
        self.total = total
        self.width = width
        self.current = 0

    def advance(self, message: str) -> None:
        self.current = min(self.current + 1, self.total)
        filled = int(self.width * self.current / self.total)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = int(100 * self.current / self.total)
        print(f"[진행] [{bar}] {self.current}/{self.total} ({percent:3d}%) {message}", flush=True)


def run_command(
    command: list[str],
    *,
    description: str,
    progress: ProgressTracker | None = None,
) -> None:
    print_step(description)
    print("실행:", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    if progress is not None:
        progress.advance(description)


def ensure_python_version(progress: ProgressTracker | None = None) -> None:
    version = sys.version_info
    if version < (3, 10):
        raise RuntimeError(
            "Python 3.10 이상이 필요합니다. Python을 업데이트한 뒤 다시 실행하세요."
        )
    print_step(f"Python 확인 완료: {version.major}.{version.minor}.{version.micro}")
    if progress is not None:
        progress.advance("Python 버전 확인")


def ensure_virtualenv(progress: ProgressTracker | None = None) -> None:
    if VENV_PYTHON.exists():
        print_step("가상환경(.venv)이 이미 있습니다.")
        if progress is not None:
            progress.advance("가상환경 확인")
        return
    run_command(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        description="가상환경(.venv) 생성",
        progress=progress,
    )


def ensure_runtime_directories(progress: ProgressTracker | None = None) -> None:
    """Create runtime output directories used by Quick Start commands."""
    for directory in RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
    print_step("Quick Start runtime folders are ready.")
    if progress is not None:
        progress.advance("Runtime folders ready")


def install_dependencies(progress: ProgressTracker | None = None) -> None:
    if not REQUIREMENTS_PATH.exists():
        raise RuntimeError("requirements.txt 파일을 찾을 수 없습니다.")
    run_command(
        [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"],
        description="pip 업데이트",
        progress=progress,
    )
    run_command(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)],
        description="필수 패키지 설치/확인",
        progress=progress,
    )


def read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        if ENV_EXAMPLE_PATH.exists():
            shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
            print_step(".env 파일이 없어 .env.example을 복사했습니다.")
        else:
            ENV_PATH.write_text("", encoding="utf-8")
            print_step(".env 파일을 새로 만들었습니다.")
    return ENV_PATH.read_text(encoding="utf-8-sig").splitlines()


def parse_env(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def upsert_env(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Quick Start 자동 설정")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    return output


def ensure_env(progress: ProgressTracker | None = None) -> None:
    lines = read_env_lines()
    values = parse_env(lines)

    api_key = values.get("OPENROUTER_API_KEY", "").strip()
    updates: dict[str, str] = {}

    if api_key in API_KEY_PLACEHOLDERS:
        print("\nOpenRouter API 키가 필요합니다.")
        print("발급 방법: docs/OPENROUTER_API_KEY.md 또는 https://openrouter.ai/settings/keys")
        api_key = input("OpenRouter API 키를 붙여넣고 Enter를 누르세요: ").strip()
        if not api_key:
            raise RuntimeError("OpenRouter API 키가 입력되지 않았습니다.")
        updates["OPENROUTER_API_KEY"] = api_key

    # Non-developer Quick Start defaults. These are refreshed every run so the
    # data window always starts from the actual execution date.
    quickstart_env = quickstart_env_updates()
    updates.update(quickstart_env)

    if not values.get("OPENROUTER_DATA_COLLECTION"):
        updates["OPENROUTER_DATA_COLLECTION"] = "deny"
    if not values.get("CRAWLER_CATEGORIES"):
        updates["CRAWLER_CATEGORIES"] = "company"
    if not values.get("USE_RERANKER"):
        updates["USE_RERANKER"] = "false"

    new_lines = upsert_env(lines, updates)
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print_step("Quick Start 환경 설정 완료")
    print(f"기준일: {quickstart_env['CRAWLER_TARGET_DATE']}")
    print("수집 범위: 실행일 포함 이전 7일")
    if progress is not None:
        progress.advance("환경 설정")


def prepare_data(progress: ProgressTracker | None = None) -> None:
    run_command(
        [str(VENV_PYTHON), "-m", "src.retrieval.launcher_guard", "--write"],
        description="Retrieval runtime write gate",
    )
    run_command(
        [str(VENV_PYTHON), "-m", "src.core.report_crawler"],
        description="실행일 기준 이전 7일 리포트 수집",
        progress=progress,
    )
    run_command(
        [
            str(VENV_PYTHON),
            "-m",
            "src.core.embed_pipeline",
            "--all",
            "--continue-on-extraction-error",
        ],
        description="수집된 전체 리포트 임베딩/검색 인덱스 생성",
        progress=progress,
    )
    print_runtime_status(progress)


def print_runtime_status(progress: ProgressTracker | None = None) -> None:
    run_command(
        [
            str(VENV_PYTHON),
            "-c",
            (
                "from src.core.status import format_status_text; "
                "print(format_status_text())"
            ),
        ],
        description="데이터 상태 확인",
        progress=progress,
    )


def launch_gui(progress: ProgressTracker | None = None) -> None:
    print("\n" + "=" * 70)
    print("브라우저가 열리면 질문을 입력하세요.")
    print("예시 질문: 최근 리포트의 주요 투자 아이디어를 요약해줘.")
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    print("=" * 70 + "\n")
    if progress is not None:
        progress.advance("Streamlit GUI 실행 시작")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "streamlit", "run", "apps/gui/app.py"],
        cwd=ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finance LLM Quick Start")
    parser.add_argument(
        "--runtime-smoke",
        action="store_true",
        help="validate the installed retrieval runtime and exit without updates",
    )
    args = parser.parse_args(argv)
    if args.runtime_smoke:
        if not VENV_PYTHON.is_file():
            print("[error] .venv Python is unavailable for runtime smoke validation")
            return 1
        return subprocess.run(
            [str(VENV_PYTHON), "-m", "src.retrieval.launcher_guard"],
            cwd=ROOT,
            check=False,
        ).returncode

    _configure_console()
    print("=" * 70)
    print("Finance LLM Quick Start")
    print("개발 명령어를 몰라도 이 실행 한 번으로 결과 화면까지 준비합니다.")
    print("=" * 70)

    try:
        progress = ProgressTracker(QUICKSTART_PROGRESS_STEPS)
        ensure_python_version(progress)
        ensure_env(progress)
        ensure_virtualenv(progress)
        ensure_runtime_directories(progress)
        install_dependencies(progress)
        prepare_data(progress)
        launch_gui(progress)
        return 0
    except KeyboardInterrupt:
        print("\n사용자 요청으로 종료했습니다.")
        return 130
    except subprocess.CalledProcessError as exc:
        print("\n[오류] 실행 중인 명령이 실패했습니다.")
        print(f"실패한 명령: {' '.join(map(str, exc.cmd if isinstance(exc.cmd, list) else [exc.cmd]))}")
        print("문제 해결 문서: docs/QUICK_START.md, docs/OPENROUTER_API_KEY.md")
        return exc.returncode or 1
    except Exception as exc:
        print(f"\n[오류] {exc}")
        print("문제 해결 문서: docs/QUICK_START.md, docs/OPENROUTER_API_KEY.md")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
