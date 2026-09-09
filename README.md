# get_qiniu_apiv2

独立的直播采集程序：调用有因直播 API 获取回放地址和历史弹幕，下载视频，用 FFmpeg 提取并切分 MP3，将音频附件和弹幕写入飞书多维表格。

**没有妙记、语音转写、数据库或 HTTP 服务端。** 这是可部署在 macOS/Linux 上的命令行采集程序。

## 数据流

```text
直播列表/指定直播 ID
├── 历史弹幕 API → 分页取完 → JSON/CSV 留档 → 分类弹幕表
└── 回放 API → MP4 下载（或 HLS 合并）→ FFmpeg MP3 切片 → 直播视频表音频附件
```

一场直播一行，名称格式为 `直播ID 标题`，日期为上海时区开播日期，所有切片追加到这一行的 `音频` 附件字段。只写 `名称`、`日期`、`音频`，不会触碰目标表中的转写字段、视频字段或按钮。

## 安装

需要 Python 3.11+、FFmpeg/ffprobe、已安装并完成**用户身份登录**的 `lark-cli`。飞书采用 CLI 官方 shortcut，附件超过 20 MB 时由 CLI 分片上传，单文件上限 2 GB；1 小时 128 kbps MP3 约 58 MB。

```bash
# macOS
brew install ffmpeg
# Debian / Ubuntu
# sudo apt-get update && sudo apt-get install -y ffmpeg

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp config.example.toml config.toml
cp .env.example .env
```

`lark-cli` 的安装/登录按当前发行版说明操作，先运行 `lark-cli --help` 和 `lark-cli auth --help`。程序显式使用 `--as user`；不自动切换应用身份。可在配置中指定已登录的 `profile`。服务器需要单独安装并授权 CLI，不能假定桌面登录会自动同步。

将上游凭证填入 `.env`，不要提交到 Git：

```dotenv
QINIU_APP_ID=你的上游应用ID
QINIU_APP_SECRET=你的上游应用密钥
QINIU_ENTERPRISE_ID=你的企业ID
```

这些是有因直播接口凭证，并非通用七牛对象存储 AK/SK，也不是飞书 app ID。`.env` 从 `config.toml` 所在目录加载；现有环境变量优先。

## 配置多维表格

在 `config.toml` 填入 Base token、音频表 ID、四张分类弹幕表 ID。不要把整个 URL 当成 token。

可用以下只读命令获取 ID：

```bash
lark-cli base +url-resolve --url '你的多维表格链接' --as user --format json
lark-cli base +table-list --base-token YOUR_BASE_TOKEN --as user --format json
```

本地交付时的 `config.toml` 已针对用户给定的 Base 配置；此文件已被 Git 忽略，远程仓库只包含通用模板。

已有表可以直接使用；没有表时，请先建立下述结构。若新建 Base，请存放到用户指定的飞书文件夹。程序自身不创建 Base 或云盘文件，附件直接属于目标 Base。

| 数据表 | 字段 | 类型 |
| --- | --- | --- |
| 直播视频 | 名称、日期 | 文本 |
| 直播视频 | 音频 | 附件 |
| 小学弹幕 / 初中弹幕 / 高中弹幕 / 初高弹幕 | 内容、时间、用户、用户ID、直播ID | 文本 |

弹幕时间为 `YYYY-MM-DD HH:mm:ss`，上游 timestamp 按 Unix 毫秒解释。分类按标题包含 `小学`、`初中`、`高中`、`初高` 匹配；没有匹配或匹配多个分类时报告错误，不猜测写入目标。也可将四个分类配置到同一张符合字段要求的弹幕表。

先运行只读检查（不需要上游凭证）：

```bash
qiniu-get check-base
```

## 运行

```bash
# 查询某开播日符合条件的直播，不下载也不写表
qiniu-get run --date 2026-09-09 --dry-run

# 单场：下载、切片并写入音频与弹幕
qiniu-get run --live-id 123456

# 多场
qiniu-get run --live-id 123456 --live-id 123457

# 只同步弹幕：不会调用回放接口或 FFmpeg
qiniu-get run --live-id 123456 --only danmaku

# 只下载、切片并写入音频
qiniu-get run --live-id 123456 --only audio

# 按开播自然日批处理
qiniu-get run --date 2026-09-09

# 指定另一个配置文件，全局参数放在子命令前面
qiniu-get --config /path/to/config.toml run --live-id 123456
```

`--date` 明确定义为 **Asia/Shanghai 开播自然日 00:00–24:00**，不是此前其他项目的业务时间窗口。列表请求的 endDate 取次日，以包含常见的跨午夜结束直播，然后按 start_time 筛选。超长直播、尚未结束的直播或供应商不同的 endDate 语义可能影响列表发现；可以在回放生成后用 `--live-id` 明确补跑。先用 dry-run 核对目标场次。

`media.segment_seconds` 默认 `3600`，最后一段保留不足一小时的音频。MP3 以音频帧为边界，因此切片时长会有毫秒级偏差。

## 重跑与故障处理

- 上游 API 读取遇到网络错误、429、5xx，最多尝试 4 次；HTTP 401 刷新令牌一次。业务错误、缺失分页结束标志或重复游标报错，不伪装为完整采集。
- 下载使用临时文件，ffprobe 验证后才改名。下载失败请重新运行，重新取得回放链接并从头下载，**不支持 HTTP 字节断点续传**。
- 媒体保留在本地。相同参数重跑会验证并复用视频、切片清单；损坏的切片重新生成；切片文件名带内容 SHA-256 的前 12 位。源视频导致处理失败时会移除程序自己的视频缓存，下次重下。默认不自动清理已成功处理的媒体。
- 弹幕仅在对应直播全部分页读取成功后开始写入。按现有字段元组及出现次数查重，保留内容相同但确实重复出现的消息。单批最多 200 条，串行写入。
- 音频按直播 ID 前缀和日期定位行，远端已有的同名（含内容指纹）切片跳过。若已经有其他命名规则/切片参数的音频，停止该场音频写入，避免重复追加；不会删除旧附件。
- 飞书写操作遇到失败或超时，不盲目重试。重新运行时先查询远端，补写缺失数据。上传成功但尚未可读等场景仍可能需要等待后重跑。
- 程序同一输出目录有进程锁。不要从多台机器或多个输出目录并发写同一 Base；飞书没有业务键唯一约束，本项目不保证分布式 exactly-once。
- **历史弹幕行若缺少直播ID，无法安全归属到某场直播，不参与该场查重。** 补采旧数据前应先核对这种历史行，避免重复数据。
- 默认保留本地视频和音频，约需视频大小再加 MP3 大小的磁盘空间；每个切片单独写入附件格。

运行结束输出 JSON 摘要。部分场次失败时继续下一场，最终退出码为 1；全部成功为 0。摘要不包含上游 token、app secret 或带签名回放链接。失败重跑时如果弹幕已成功、音频失败，弹幕会先查重。

```text
data/
└── LIVE_ID/
    ├── video.mp4
    ├── danmaku.json
    ├── danmaku.csv
    └── audio_3600/
        ├── manifest.json
        ├── LIVE_ID_s3600_part0000_HASH.mp3
        └── LIVE_ID_s3600_part0001_HASH.mp3
```

`.env`、`config.toml` 和 `data/` 都不会提交到仓库。弹幕 JSON/CSV 含用户数据，请只在受控环境保留。若用表格软件打开 CSV，建议将所有列作为文本导入。

## 接口来源与验证边界

上游协议参考用户指定的 [qiniu_api_get](https://github.com/IXYTYXI/qiniu_api_get) 及本地已存在的有因客户端：

- `POST /v1/account/auth-token/`
- `GET /v1/course/system/over_course/`
- `GET /v1/course/system/live_info/{id}/`
- `GET /v1/course/system/get_vod/{id}/`
- `GET /v1/course/push_message/{id}/`

这并不等于已验证供应商所有线上响应。交付测试使用模拟 HTTP 和真实本地 FFmpeg；飞书仅做目标表结构与用户访问的只读核验。未提供本项目上游凭证，因此尚未执行真实直播采集及附件写入联调。

## 开发与测试

```bash
python -m pytest -q
python -m qiniu_get --help
```

GitHub Actions 和 GitLab CI 配置均包含安装 FFmpeg 后运行测试。测试不会访问真实直播 API 或写入真实飞书表。
