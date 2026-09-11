from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass
class Config:
    api_base_url: str = 'https://pyapi.youinsh.com/livestreamapi'
    output_dir: Path = Path('data')
    segment_seconds: int = 3600
    base_token: str = ''
    audio_table: str = ''
    danmaku_tables: dict = field(default_factory=dict)
    lark_cli: str = 'lark-cli'
    lark_profile: str | None = None
    auth: str = 'cli'
    open_base: str = 'https://open.feishu.cn'


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as handle:
        data = tomllib.load(handle)
    media, base = data.get('media', {}), data.get('feishu', {})
    seconds = media.get('segment_seconds', 3600)
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError('media.segment_seconds must be an integer between 1 and 86400')
    output = Path(media.get('output_dir', 'data'))
    auth = base.get('auth', 'cli')
    if auth not in ('cli', 'app'):
        raise ValueError('feishu.auth must be "cli" or "app"')
    return Config(api_base_url=data.get('qiniu', {}).get('base_url', Config.api_base_url),
                  output_dir=output if output.is_absolute() else path.parent / output,
                  segment_seconds=seconds, base_token=base.get('base_token', ''),
                  audio_table=base.get('audio_table', ''), danmaku_tables=base.get('danmaku_tables', {}),
                  lark_cli=base.get('cli', 'lark-cli'), lark_profile=base.get('profile') or None,
                  auth=auth, open_base=base.get('open_base', 'https://open.feishu.cn'))
