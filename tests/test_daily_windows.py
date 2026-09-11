import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows PowerShell integration')
SCRIPT = Path(__file__).resolve().parents[1] / 'run-daily-windows.ps1'


def execute(script, *args, cwd=None):
    return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-File', str(script), *args], cwd=cwd, capture_output=True,
                          text=True, timeout=30)


def test_plan_uses_script_directory_and_propagates_dry_run(tmp_path):
    result = execute(SCRIPT, '-TargetDate', '2026-08-24', '-DryRun', '-PrintPlan', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert Path(plan['working_directory']) == SCRIPT.parent
    assert plan['target_date'] == '2026-08-24'
    assert '--dry-run' in plan['arguments']
    assert plan['arguments'][-2] == 'audio'


def test_default_date_is_yesterday_in_shanghai():
    before = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1)).isoformat()
    result = execute(SCRIPT, '-PrintPlan')
    after = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1)).isoformat()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['target_date'] in (before, after)


def test_missing_config_produces_logs_and_nonzero_exit(tmp_path):
    script = tmp_path / SCRIPT.name
    shutil.copyfile(SCRIPT, script)
    result = execute(script, '-TargetDate', '2026-08-24', '-PythonExe', sys.executable)
    assert result.returncode == 1
    records = list((tmp_path / 'logs').glob('*.result.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8-sig'))
    assert record['exit_code'] == 1
    assert 'config.toml' in Path(record['stderr_log']).read_text(encoding='utf-8')


@pytest.mark.parametrize('workers', ['1', '3'])
def test_plan_forwards_worker_limit(workers):
    result = execute(SCRIPT, '-Workers', workers, '-PrintPlan')
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)['arguments']
    assert str(args[args.index('--workers') + 1]) == workers
