"""
pytest configuration for TaxAI parser tests.
"""
import sys
from pathlib import Path
import pytest

# Thêm project root vào sys.path để `from src.parsing...` hoạt động trong test reparse
sys.path.insert(0, str(Path(__file__).parent.parent))


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help=(
            "Ghi đè snapshot files trong tests/golden/ bằng output mới nhất từ parser. "
            "Chỉ dùng khi cố ý update GOLDEN sau khi verify improvement."
        ),
    )


@pytest.fixture
def update_snapshots(request) -> bool:
    """True nếu pytest được chạy với --update-snapshots."""
    return request.config.getoption("--update-snapshots")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "reparse: marks tests that actually re-parse from PDF (slow, ~2-5 min). "
        "Run with: pytest -m reparse tests/test_parser_regression.py",
    )
    config.addinivalue_line(
        "markers",
        "snapshot: marks tests that compare full JSON output against golden snapshots. "
        "Run with: pytest -m snapshot tests/test_parser_regression.py",
    )
    config.addinivalue_line(
        "markers",
        "integration: marks tests that call real Gemini API (requires GEMINI_API_KEY). "
        "Run with: pytest -m integration tests/test_regression_30.py",
    )
