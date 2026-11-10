import os
from pathlib import Path
import tempfile
from unittest import mock


def isolate_state_home(test_case):
    temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".pattern-buffer-state-")
    test_case.addCleanup(temporary.cleanup)
    state_home = Path(temporary.name) / "state"
    state_home = Path(state_home).resolve()
    environment = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)})
    environment.start()
    test_case.addCleanup(environment.stop)
    test_case.xdg_state_home = state_home
    return state_home
