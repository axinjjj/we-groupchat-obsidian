"""Windows portability tests for the staged port."""
from pathlib import Path


def load_tests(loader, standard_tests, pattern):
    tests_dir = Path(__file__).resolve().parent
    repository_root = tests_dir.parents[1]
    return loader.discover(
        str(tests_dir),
        pattern=pattern or "test_*.py",
        top_level_dir=str(repository_root),
    )
