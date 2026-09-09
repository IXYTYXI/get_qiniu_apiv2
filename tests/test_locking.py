"""Run on both native Windows and Unix CI runners."""
import subprocess
import sys
import pytest
from qiniu_get.pipeline import run_lock


def test_import_without_unix_fcntl():
    result = subprocess.run([sys.executable, '-c',
        "import sys; sys.modules['fcntl'] = None; import qiniu_get.pipeline"],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_another_process_cannot_acquire_held_lock(tmp_path):
    script = '''
import sys
from qiniu_get.pipeline import run_lock
try:
    with run_lock(sys.argv[1]):
        pass
except RuntimeError:
    sys.exit(23)
'''
    with run_lock(tmp_path):
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=15)
        assert result.returncode == 23
    assert subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=15).returncode == 0


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(ValueError):
        with run_lock(tmp_path):
            raise ValueError('work failed')
    with run_lock(tmp_path):
        pass


def test_lock_released_after_owner_terminated(tmp_path):
    script = '''
import sys
from qiniu_get.pipeline import run_lock
with run_lock(sys.argv[1]):
    print('READY', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'READY'
        child.terminate()
        child.wait(timeout=15)
        with run_lock(tmp_path):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)
        child.stdin.close()
        child.stdout.close()
