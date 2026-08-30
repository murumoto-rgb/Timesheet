"""Run the real launcher flow against disposable paths and no-network tools."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("old_copies", [1, 2])
def test_first_run_cannot_silently_split_or_select_an_old_connection(tmp_path, old_copies):
    app = tmp_path / "new-install"
    (app / ".git").mkdir(parents=True)
    search_root = tmp_path / "old-installs"
    source_files = {}
    for number in range(old_copies):
        old = search_root / f"Timesheet-{number}"
        old.mkdir(parents=True)
        for name in (".env", "qbo_tokens.json", "qbo_operations.json", "qbo_push.json", "qbo_audit.json"):
            path = old / name
            value = f"synthetic private {name} for company {number}\n"
            path.write_text(value)
            source_files[path] = value

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    stubs = {
        "curl": "exit 1\n",  # no running app, no network
        "xcode-select": "exit 0\n",
        "git": 'if [ "$3" = "branch" ]; then echo main; fi\nexit 0\n',
        "open": 'touch "$TIMESHEET_TEST_STARTED"\nexit 99\n',
        "python3.13": 'touch "$TIMESHEET_TEST_STARTED"\nexit 99\n',
        "python3": 'touch "$TIMESHEET_TEST_STARTED"\nexit 99\n',
    }
    for name, body in stubs.items():
        path = mock_bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o700)

    # Only deployment paths change; execute the launcher's actual control flow.
    source = (Path(__file__).resolve().parents[1] / "Timesheet.command").read_text()
    source = source.replace('APP_DIR="$HOME/TimesheetApp"', 'APP_DIR="$TIMESHEET_TEST_APP_DIR"')
    source = source.replace('for d in "$HOME/Desktop" "$HOME/Downloads" "$HOME"; do',
                            'for d in "$TIMESHEET_TEST_SEARCH_DIR"; do')
    launcher = app / "Timesheet.command"
    launcher.write_text(source)
    started = tmp_path / "unexpected-start"
    env = {**os.environ, "PATH": f"{mock_bin}{os.pathsep}{os.environ['PATH']}",
           "TIMESHEET_TEST_APP_DIR": str(app), "TIMESHEET_TEST_SEARCH_DIR": str(search_root),
           "TIMESHEET_TEST_STARTED": str(started)}
    result = subprocess.run(["/bin/bash", str(launcher)], input="\n", text=True,
                            capture_output=True, timeout=10, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No settings or accounting data have been copied" in result.stdout
    assert "Move my old Timesheet installation safely" in result.stdout
    assert not (app / ".env").exists()
    assert not list(app.glob("qbo_*.json"))
    assert not started.exists()
    assert all(path.read_text() == value for path, value in source_files.items())
    assert "synthetic private" not in result.stdout + result.stderr
