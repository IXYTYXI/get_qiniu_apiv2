"""Stream media to disk and atomically publish verified MP3 segments."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
from urllib.parse import urlparse
import httpx


class MediaError(RuntimeError):
    pass


class AudioCoverageError(MediaError):
    """Duration mismatch alone does not prove corrupt source bytes."""


class MediaProcessor:
    def __init__(self, *, client=None):
        self.http = client or httpx.Client(timeout=120, follow_redirects=True)

    def close(self):
        self.http.close()

    @staticmethod
    def _run(args):
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=86400).stdout
        except (subprocess.SubprocessError, OSError):
            # stderr can contain signed URLs; keep it out of console and persistent logs.
            raise MediaError(f'{args[0]} failed; check media validity, disk space and installed codecs') from None

    @staticmethod
    def fingerprint(path):
        with Path(path).open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()

    def duration(self, path):
        result = json.loads(self._run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                      '-of', 'json', str(path)]))
        duration = float(result['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError('Invalid media duration')
        return duration

    def decoded_audio_duration(self, source):
        # Count decoded samples on a fresh timeline; container packet durations can
        # differ from actual decoded audio. Fail closed on reported decode errors.
        output = self._run([
            'ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(source),
            '-map', '0:a:0', '-vn', '-af', 'asetpts=N/SR/TB',
            '-c:a', 'pcm_s16le', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'
        ])
        progress = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        try:
            duration = float(progress['out_time_us']) / 1_000_000
        except (KeyError, ValueError):
            raise MediaError('Decoded audio duration was not reported') from None
        if progress.get('progress') != 'end' or not math.isfinite(duration) or duration <= 0:
            raise MediaError('Decoded audio duration is invalid or incomplete')
        return duration

    def download(self, url, target, *, hls=False):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if urlparse(url).scheme not in {'https', 'http'}:
            raise MediaError('Only HTTP(S) media sources are accepted')
        if target.exists():
            try:
                self.duration(target)
                return target
            except (MediaError, ValueError, KeyError):
                target.unlink()
        temporary = target.with_name(target.stem + '.partial.mp4')
        try:
            if hls:
                self._run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-rw_timeout', '120000000',
                           '-i', url, '-map', '0:v?', '-map', '0:a:0', '-c', 'copy', str(temporary)])
            else:
                with self.http.stream('GET', url) as response:
                    response.raise_for_status()
                    with temporary.open('wb') as output:
                        for block in response.iter_bytes(1024 * 1024):
                            output.write(block)
                if not temporary.stat().st_size:
                    raise MediaError('Downloaded media is empty')
            self.duration(temporary)
            temporary.replace(target)
            return target
        except httpx.HTTPError:
            raise MediaError('Media download failed; rerun to obtain a fresh URL') from None
        finally:
            temporary.unlink(missing_ok=True)

    def segment(self, source, directory, live_id, seconds=3600):
        if seconds <= 0:
            raise ValueError('segment seconds must be positive')
        source, directory = Path(source), Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stat = source.stat()
        signature = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'seconds': seconds}
        manifest = directory / 'manifest.json'
        if manifest.exists():
            try:
                saved = json.loads(manifest.read_text())
                if saved['source'] == signature and saved['parts']:
                    parts = [directory / p['name'] for p in saved['parts']]
                    if all(p.stat().st_size == meta['size'] and self.fingerprint(p) == meta['sha256'] and self.duration(p) > 0
                           for p, meta in zip(parts, saved['parts'])):
                        return parts
            except (OSError, ValueError, KeyError, MediaError):
                pass
        manifest.unlink(missing_ok=True)
        # Only remove this program's own output pattern in this configuration directory.
        for old in directory.glob(f'{int(live_id)}_s{seconds}_part*.mp3'):
            old.unlink()
        pattern = directory / f'{int(live_id)}_s{seconds}_part%04d.mp3'
        self._run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                   '-map', '0:a:0', '-vn', '-c:a', 'libmp3lame', '-b:a', '128k',
                   '-f', 'segment', '-segment_time', str(seconds), '-reset_timestamps', '1', str(pattern)])
        parts = sorted(directory.glob(f'{int(live_id)}_s{seconds}_part*.mp3'))
        if not parts:
            raise MediaError('No audio segments were produced')
        durations = [self.duration(p) for p in parts]
        source_duration = self.duration(source)
        audio_duration = sum(durations)
        difference = audio_duration - source_duration
        tolerance = max(2, len(parts) * .15)
        decoded_duration = None
        if abs(difference) > tolerance:
            try:
                decoded_duration = self.decoded_audio_duration(source)
            except MediaError:
                raise AudioCoverageError(
                    'Audio coverage could not be verified by strict decoding; '
                    'source and segments retained for inspection'
                ) from None
            difference = audio_duration - decoded_duration
            if abs(difference) > tolerance:
                raise AudioCoverageError(
                    'Audio segments do not cover the decoded audio duration: '
                    f'source={source_duration:.3f}s, audio={audio_duration:.3f}s, '
                    f'decoded={decoded_duration:.3f}s, difference={difference:+.3f}s, '
                    f'tolerance={tolerance:.3f}s, parts={len(parts)}; '
                    'source and segments retained for inspection'
                )
        named_parts, metadata = [], []
        for part, duration in zip(parts, durations):
            digest = self.fingerprint(part)
            named = part.with_name(part.stem + '_' + digest[:12] + '.mp3')
            part.replace(named)
            named_parts.append(named)
            metadata.append({'name': named.name, 'size': named.stat().st_size,
                             'duration': duration, 'sha256': digest})
        parts = named_parts
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps({'source': signature, 'parts': metadata,
                                         'coverage': {'container_seconds': source_duration,
                                                      'decoded_seconds': decoded_duration,
                                                      'segments_seconds': audio_duration}}), encoding='utf-8')
        temporary.replace(manifest)
        return parts
