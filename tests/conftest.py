import sys
import os

_tests_dir = os.path.dirname(__file__)
_project_root = os.path.dirname(_tests_dir)

# Project root: so tests can import recurring_tasks directly.
sys.path.insert(0, _project_root)
# Tests dir: so tests can import shared helpers.
sys.path.insert(0, _tests_dir)
