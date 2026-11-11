"""Understanding produces plans; existing services retain authority over facts and safety."""

# The established service modules are imported by their canonical names.
import sys
from pathlib import Path
_scripts=str(Path(__file__).resolve().parents[2]/'scripts')
if _scripts not in sys.path:sys.path.insert(0,_scripts)
