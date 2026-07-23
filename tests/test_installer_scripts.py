import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
INSTALLER_SH = ROOT / "scripts" / "install.sh"
POWERSHELL = shutil.which("powershell.exe")


def _helper_prefix() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    prefix, marker, _rest = text.partition("# --- Config")
    assert marker
    return prefix


def _run_powershell(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script_path = tmp_path / "installer-helper-test.ps1"
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_windows_installer_stops_connector_before_replacing_source():
    text = INSTALLER.read_text(encoding="utf-8")
    refresh = text.index("# Refresh the app folder")
    stop = text.index("Stop-RunningDeepboxConnectors -VenvPython $VenvPy", refresh)
    remove = text.index("Remove-DirectoryWithRetry -Path $Src", refresh)
    copy = text.index("Copy-Item -Recurse -Force", refresh)
    assert refresh < stop < remove < copy
    assert "Command lines are inspected but never printed" in text


def test_windows_installer_creates_stable_command_outside_app():
    text = INSTALLER.read_text(encoding="utf-8")
    command_body = text.split('$commandBody = @"', 1)[1].split('"@', 1)[0]

    assert "$Command = Join-Path $Bin 'deepbox.cmd'" in text
    assert 'if /I "%~1"=="upgrade" goto upgrade' in command_body
    assert '-I -u -m connector.cli %*' in command_body
    assert command_body.count("powershell.exe") == 1
    assert "DEEPBOX_INSTALL_ONLY=1" in command_body
    assert "PYTHONPATH" not in command_body
    assert "Remove-DirectoryWithRetry" not in command_body
    assert "deepbox-app.pth" in text
    assert "Path(sys.prefix).parent / 'app'" in text
    assert "Add-UserPathEntry -Path $Bin" in text


def test_unix_installer_creates_stable_command_outside_app():
    text = INSTALLER_SH.read_text(encoding="utf-8")
    command_body = text.split("cat > \"$COMMAND\" <<'EOF'", 1)[1].split("\nEOF", 1)[0]

    assert 'COMMAND="${BIN}/deepbox"' in text
    assert 'if [ "${1:-}" = "upgrade" ]; then' in command_body
    assert '-I -u -m connector.cli "$@"' in command_body
    assert command_body.count("curl -fsSL") == 1
    assert "DEEPBOX_INSTALL_ONLY=1" in command_body
    assert "PYTHONPATH" not in command_body
    assert 'rm -rf "$SRC"' not in command_body
    assert "deepbox-app.pth" in text
    assert "Path(sys.prefix).parent / 'app'" in text
    assert "deepbox command path" in text


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_connector_process_matcher_is_scoped_to_venv_and_module(tmp_path):
    target = r"C:\Users\Test User\.deepbox\venv\Scripts\python.exe"
    other = r"C:\Python312\python.exe"
    cases = [
        {"ExecutablePath": target, "CommandLine": f'"{target}" -u -m connector'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -u -m connector.cli connect'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m pip list'},
        {"ExecutablePath": other, "CommandLine": f'"{other}" -m connector'},
        {"ExecutablePath": None, "CommandLine": f'"{target}" -m connector --mode supervisor'},
        {"ExecutablePath": target, "CommandLine": f'"{target}" -m connector_tools'},
    ]
    payload = json.dumps(cases).replace("'", "''")
    target_ps = target.replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\n$target = '{target_ps}'\n"
        + f"$cases = ConvertFrom-Json '{payload}'\n"
        + "$results = @($cases | ForEach-Object { "
        + "Test-DeepboxConnectorProcess -Process $_ -VenvPython $target })\n"
        + "ConvertTo-Json -Compress -InputObject @($results)\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        True,
        True,
        False,
        False,
        True,
        False,
    ]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_process_tree_includes_only_connector_descendants(tmp_path):
    processes = [
        {"ProcessId": 10, "ParentProcessId": 1},
        {"ProcessId": 11, "ParentProcessId": 10},
        {"ProcessId": 12, "ParentProcessId": 11},
        {"ProcessId": 20, "ParentProcessId": 1},
    ]
    payload = json.dumps(processes).replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\n$processes = ConvertFrom-Json '{payload}'\n"
        + "$ids = @(Get-DeepboxProcessTreeIds -Processes $processes "
        + "-RootProcessIds 10) | Sort-Object\n"
        + "ConvertTo-Json -Compress -InputObject @($ids)\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [10, 11, 12]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_installer_discovers_and_stops_a_running_venv_connector(tmp_path):
    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    venv_python = venv / "Scripts" / "python.exe"
    work = tmp_path / "work"
    package = work / "connector"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "Path('child.pid').write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    sleeper = subprocess.Popen(
        [str(venv_python), "-m", "connector"],
        cwd=work,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_pid_file = work / "child.pid"
    child_pid = None
    child_stopped = False
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        assert sleeper.poll() is None
        target_ps = str(venv_python).replace("'", "''")
        result = _run_powershell(
            _helper_prefix()
            + f"\nStop-RunningDeepboxConnectors -VenvPython '{target_ps}'\n",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        sleeper.wait(timeout=5)
        assert "Existing connector stopped." in result.stdout
        child_check = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert child_check.returncode == 0
        child_stopped = True
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        if child_pid is not None and not child_stopped:
            subprocess.run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_remove_directory_helper_removes_an_unlocked_tree(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "connector.py").write_text("pass\n", encoding="utf-8")
    target_ps = str(target).replace("'", "''")
    result = _run_powershell(
        _helper_prefix()
        + f"\nRemove-DirectoryWithRetry -Path '{target_ps}' "
        + "-Attempts 2 -DelayMilliseconds 1\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_remove_directory_helper_retries_a_transient_cwd_lock(tmp_path):
    target = tmp_path / "locked-app"
    target.mkdir()
    ready = tmp_path / "locker-ready"
    env = os.environ.copy()
    env["DEEPBOX_TEST_LOCK_PATH"] = str(target)
    env["DEEPBOX_TEST_READY_PATH"] = str(ready)
    locker = subprocess.Popen(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Set-Location -LiteralPath $env:DEEPBOX_TEST_LOCK_PATH; "
            "[IO.File]::WriteAllText($env:DEEPBOX_TEST_READY_PATH, 'ready'); "
            "Start-Sleep -Milliseconds 700",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        target_ps = str(target).replace("'", "''")
        result = _run_powershell(
            _helper_prefix()
            + f"\nRemove-DirectoryWithRetry -Path '{target_ps}' "
            + "-Attempts 20 -DelayMilliseconds 100\n",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert not target.exists()
    finally:
        if locker.poll() is None:
            locker.kill()
        locker.wait(timeout=5)
