"""Run the retained migration guards through the standard offline test suite."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "references/migrations/senty-20261107/test_migration_guards.py"
_spec = importlib.util.spec_from_file_location("senty_migration_guards", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
MigrationGuards = _module.MigrationGuards
