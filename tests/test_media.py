import json
import subprocess
import shutil
import pytest
from qiniu_get.media import MediaProcessor


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
def test_real_video_to_audio_segments_and_resume(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:r=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '5',
                    '-c:v', 'mpeg4', '-c:a', 'aac', str(source)], check=True)
    media = MediaProcessor()
    parts = media.segment(source, tmp_path / 'parts', '7', 2)
    assert len(parts) == 3
    lengths = [media.duration(p) for p in parts]
    assert sum(lengths) == pytest.approx(5, abs=.3)
    assert max(lengths) < 2.15
    mtimes = [p.stat().st_mtime_ns for p in parts]
    assert media.segment(source, tmp_path / 'parts', '7', 2) == parts
    assert mtimes == [p.stat().st_mtime_ns for p in parts]
    parts[1].write_bytes(b'broken')
    media.segment(source, tmp_path / 'parts', '7', 2)
    assert media.duration(parts[1]) > 1


def test_empty_download_never_becomes_completed_file(tmp_path):
    import httpx
    media = MediaProcessor(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b''))))
    with pytest.raises(RuntimeError):
        media.download('https://example.com/x.mp4', tmp_path / 'video.mp4')
    assert not (tmp_path / 'video.mp4').exists()


def test_parts_have_content_fingerprint(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '1', '-c:a', 'aac', str(source)], check=True)
    parts = MediaProcessor().segment(source, tmp_path / 'parts', '9', 3600)
    import hashlib
    assert hashlib.sha256(parts[0].read_bytes()).hexdigest()[:12] in parts[0].name


def test_coverage_failure_reports_durations_and_keeps_unpublished_parts(tmp_path):
    from qiniu_get.media import AudioCoverageError
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=black:s=64x64:r=10:duration=8', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', '-c:v', 'mpeg4', '-c:a', 'aac',
                    str(source)], check=True)
    media = MediaProcessor()
    try:
        with pytest.raises(AudioCoverageError, match=r'source=8\.000s.*audio=.*difference=.*tolerance='):
            media.segment(source, tmp_path / 'parts', 10, 3600)
        assert source.exists()
        assert not (tmp_path / 'parts' / 'manifest.json').exists()
        assert list((tmp_path / 'parts').glob('*.mp3'))
    finally:
        media.close()
