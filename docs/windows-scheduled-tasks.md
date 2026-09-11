# Windows 定时采集录音：一次设置，每天自动运行

适用项目：get_qiniu_apiv2；原生 Windows + Python 3.12 + FFmpeg + lark-cli。无需 WSL。本文基于当前用户已经通过飞书字段检查和七牛直播列表查询的环境编写，不要求重新安装或重新授权。

默认方案：每天北京时间 07:00，由 Windows 任务计划程序启动，采集上海时区“昨天开播”的直播录音，下载视频、按小时切 MP3、写入配置的多维表格。07:00 是本文示例，可改成合适的时间；若回放此时尚未生成，应推迟执行时间。

所有下载和上传都在这台 Windows 上发生。请使用不按流量计费的网络，并保持电脑开机。任务不会借用 Mac 的网络或登录状态。

## 1. 更新并确认文件确实存在

在现有项目目录的 PowerShell 执行：

```powershell
git pull origin main
& "./.venv/Scripts/python.exe" -m pip install -e .
Test-Path "./run-daily-windows.ps1"
Test-Path "./config.toml"
Test-Path "./.env"
```

三个检查都应为 True。新增加的 run-daily-windows.ps1 会随 git pull 下载；config.toml 和 .env 是这台机器的本地配置，不包含在 Git 仓库中。已经配好的文件不要用模板覆盖。

若 git pull 提示不是仓库，先进入包含 pyproject.toml 和 .git 的项目目录，而不是它的上层 autoget 目录。无需重新克隆已有项目。

确认当前目录、Windows 账号和工具位置：

```powershell
(Get-Location).Path
whoami
Get-TimeZone
Get-Command ffmpeg, ffprobe, lark-cli.cmd -ErrorAction SilentlyContinue
```

后面任务必须使用同一个 Windows 账号。例如授权在 Administrator 下完成，就使用这个账号运行任务，不要改成 SYSTEM、另一个管理员或服务账号。

若任务环境找不到 lark-cli.cmd，可把 config.toml 中的 feishu.cli 改成上述 Get-Command 返回的完整 .cmd 路径；TOML 用单引号保存 Windows 路径，例如 cli = 'C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd'。以实际返回路径为准，不要凭例子猜目录。

## 2. 先验证定时入口，不下载视频

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "./run-daily-windows.ps1" -PrintPlan
```

这只打印运行计划，完全不联网、不创建日志、不采集。核对 target_date 是否是北京时间的昨天、working_directory 是否为当前项目。

然后做一次查询试运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "./run-daily-windows.ps1" -DryRun
```

这会调用七牛 API 查直播列表并生成日志，不下载视频、不写入多维表格。正常退出应显示 exit code: 0。stdout.log 中是直播列表；空列表代表没找到目标日场次，不代表完成了录音采集。

文件与日期的职责：run-windows.ps1 用于指定日期范围；run-daily-windows.ps1 用于每天动态计算昨天并输出日志。不要在每日任务中写死 2026-08-24，否则每天都会处理同一天。

## 3. 创建每日任务：推荐使用 Windows 界面

按 Win+R，输入 taskschd.msc，打开“任务计划程序”。右侧选择“创建任务”，不要选择“创建基本任务”。

### 常规

任务名称填 QiniuAudio-Daily。运行账号选第 1 节 whoami 显示的同一个账号。

首次建议选“只在用户登录时运行”，不勾选“使用最高权限运行”。这允许关闭 PowerShell 窗口、锁屏；但不能注销 Windows 账号。此选项更容易复用已完成的 lark-cli 用户登录环境。

如果必须在账号注销后仍执行，先完成本文验证，再参考第 8 节切换。不是必须用最高权限，提权也不能代替飞书权限。

### 触发器

新建触发器，选择“按预定计划”→“每天”，时间设为 07:00，勾选“已启用”。这里的 07:00 使用 Windows 系统时区；若要求北京时间 07:00，请先在 Windows 设置中把系统时区设为 UTC+08:00 北京等中国时区。脚本的数据日期始终按上海时区计算，不受系统时区影响。

### 操作

新建操作，选择“启动程序”。填写下面三项。项目路径以第 1 节实际输出为准；下面是本次部署示例。

| 项目 | 填写内容 |
| --- | --- |
| 程序或脚本 | C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe |
| 添加参数 | -NoProfile -ExecutionPolicy Bypass -File "D:\program\autoget\get_qiniu_apiv2\run-daily-windows.ps1" |
| 起始于 | D:\program\autoget\get_qiniu_apiv2 |

“添加参数”中的脚本路径保留双引号；“起始于”不加双引号。若 Windows 不在 C:\Windows，使用 $env:SystemRoot 对应的实际系统目录。不要只填写 python、不要依赖虚拟环境激活，也不要把 .env 凭证放到任务参数里。

如想避免手抄路径，在项目 PowerShell 中生成参数后复制第二行：

```powershell
$entry = (Resolve-Path "./run-daily-windows.ps1").Path
'-NoProfile -ExecutionPolicy Bypass -File "' + $entry + '"'
```

### 条件和设置

保持电脑接通电源、联网且不休眠；如果使用“唤醒计算机运行此任务”，仍受硬件和电源策略限制，不能唤醒关机的电脑。

在“设置”中勾选“允许按需运行任务”和“如果过了计划开始时间，立即启动任务”。“如果此任务已在运行”选“不启动新实例”。取消“如果任务运行时间超过……则停止任务”，避免长视频处理中被强行终止；磁盘和下载进度仍需定期检查。

不要同时创建多个相同的每日任务。也不要在历史补采尚未结束时启动每日任务；程序的文件锁会拒绝同一输出目录的第二个采集进程。

以上设置不自动更改任何网络计费选项。任务计划程序的“网络可用”不等于“不按流量计费”，应在运行机器上固定使用合适的网络。

## 4. 验证任务真的能在计划程序里运行

第一次验证只跑查询：临时在“添加参数”末尾加上 -DryRun，保存任务。右键 QiniuAudio-Daily →“运行”。这一步测试的是计划任务账号、PATH、工作目录和七牛凭证，不会下载视频。

待状态恢复为“准备就绪”后，刷新界面，看“上次运行结果”。0 或 0x0 表示进程成功退出，同时查看 logs 下本次输出。确认成功后，移除操作参数末尾的 -DryRun 并保存；以后到点会正式采集。

如果仍保留 -DryRun，每天只查直播列表，不会产生录音附件。正式采集的 stdout.log 中应有 selected、completed、failed 汇总，且 failed 为空；最后再打开多维表格核对音频附件。查询试运行不验证飞书上传权限，首次正式运行仍需要确认写入结果。

可以在 PowerShell 查看任务状态：

```powershell
Get-ScheduledTask -TaskName "QiniuAudio-Daily" | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName "QiniuAudio-Daily" | Format-List LastRunTime, NextRunTime, LastTaskResult
```

## 5. 日志在哪里，怎么看

每次运行都会在项目 logs 文件夹生成三个文件，不覆盖以前的日志：

| 后缀 | 内容 |
| --- | --- |
| .stdout.log | 七牛查询列表，或正式采集完成/失败的 JSON 汇总 |
| .stderr.log | 正在处理的直播 ID 与错误提示；不是只有失败时才有内容 |
| .result.json | 本次目标日期、UTC 开始/结束时间、退出码、日志路径 |

日志名同时包含目标日期和本次启动时间，因此失败重跑也能区分。result.json 在该次进程结束后生成；尚未出现时，先看任务是否仍在运行。

```powershell
Get-ChildItem "./logs" | Sort-Object LastWriteTime -Descending | Select-Object -First 9 Name, Length, LastWriteTime
$latest = Get-ChildItem "./logs/*.stderr.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) { Get-Content $latest.FullName -Tail 30 -Wait }
```

按 Ctrl+C 只退出这条日志查看命令，不会停止已由任务计划程序启动的任务。

## 6. 历史补采和失败日期怎么处理

每日任务只处理“执行时上海日期的昨天”。它不会自动追溯停机期间的所有日期，也没有待处理日期队列。“错过时间立即运行”只是补触发一次，不能代替多天的历史补采。

固定日期补采仍可用已有脚本。若想让 8 月 24–31 日的补采到指定时间自动启动，可以另建一个名为 QiniuAudio-Backfill-20260824-31 的任务，账号及设置沿用上面配置，触发器选择“一次”，操作参数改为：

```text
-NoProfile -ExecutionPolicy Bypass -File "D:\program\autoget\get_qiniu_apiv2\run-windows.ps1" -StartDate 2026-08-24 -EndDate 2026-08-31 -Only audio
```

历史脚本直接输出到控制台，没有每日入口的三个日志文件；若需要指定某一天的日志，改用每日入口的 -TargetDate 参数。例如创建一次性任务时，操作参数可填写：

```text
-NoProfile -ExecutionPolicy Bypass -File "D:\program\autoget\get_qiniu_apiv2\run-daily-windows.ps1" -TargetDate 2026-08-24
```

一次性历史任务完成后禁用，不要把固定历史日期设置成“每天”重复。长时间历史补采开始前，先禁用每日任务；补采完成后再启用。

目前程序不会覆盖不同命名规则的旧音频附件；遇到旧附件冲突会报告失败，需核对已有录音后决定处理。完整媒体默认保留在 data 目录，占用磁盘；不能把“任务结果为 0”当成所有历史日期都已补齐，也不能把退出码 1 全部归因于网络。

## 7. 暂停、恢复和停止

暂停今后的自动启动（不停止正在运行的一次）：

```powershell
Disable-ScheduledTask -TaskName "QiniuAudio-Daily"
```

恢复今后的自动启动：

```powershell
Enable-ScheduledTask -TaskName "QiniuAudio-Daily"
```

若要立即停止正在下载的一次，先禁用任务，再在任务计划程序右键该任务→“结束”，或执行：

```powershell
Stop-ScheduledTask -TaskName "QiniuAudio-Daily"
```

随后检查任务状态、任务管理器和网络占用，确认本任务的 Python/FFmpeg 子进程也已退出，不要误杀其他项目进程。暂停或停止不需要删除 .env、config.toml 或 data。

手动触发已配置任务会按它当前的参数正式执行；只有确认网络和参数合适时再运行：

```powershell
Start-ScheduledTask -TaskName "QiniuAudio-Daily"
```

## 8. 无人登录时运行及常见问题

如果要在 Windows 注销后继续运行，在任务“常规”中改为“不管用户是否登录都要运行”，仍指定同一个已授权账号。保存时由 Windows 系统窗口接收该账号的登录密码，不要把密码写进脚本；Windows Hello PIN 通常不等于账号密码。不要勾选“不存储密码，仅能访问本地资源”，本任务需要联网。切换后必须从任务计划程序重新试运行，确认实际环境仍能读取 CLI 登录凭据。

| 现象 | 处理方式 |
| --- | --- |
| 手动可运行，计划任务失败 | 核对任务账号、起始目录、PATH，以及 config.toml 的 lark-cli.cmd 路径 |
| config.toml 找不到 | 确认它在项目根目录；不要把上层 autoget 目录作为项目目录 |
| lark-cli token_missing / token_invalid | 在任务使用的同一个 Windows 账号下执行 auth status --json --verify，先看现有状态，再按确实缺少的授权恢复 |
| 字段检查通过但上传失败 | 字段只读检查不证明记录写入、附件上传和目标表编辑权限，查看原始 CLI 错误 |
| 上次结果 0x41301 | 通常表示任务仍在运行；查看进程与日志，不把它当采集完成 |
| 提示 already running | 同一输出目录已有任务；先查清进程，不删除锁文件来强行绕过 |
| 昨天没有录音 | 查 stdout.log 的 selected 和失败项；可能没有场次，也可能回放尚未生成 |
| 程序运行越来越慢或磁盘不足 | 查看 data 文件大小；确认飞书附件完整后，再自行清理不再需要的本地媒体 |

当前使用 lark-cli 用户身份，定时调度不会把它变成应用身份。CLI 会按其机制使用刷新令牌，但撤销授权、刷新令牌失效或账户策略变化仍可能需要重新授权；不能承诺永久零维护。没有实际错误时，不要反复追加权限。

## 9. 参考和验证范围

Microsoft 官方资料：

- [任务执行程序、参数及工作目录](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskaction?view=windowsserver2025-ps)
- [任务运行账号与登录类型](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps)
- [错过触发、运行时限和重复实例设置](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset?view=windowsserver2025-ps)

配套脚本自动化测试覆盖：从其他工作目录启动时仍定位项目、上海时区昨天的日期、DryRun 参数传递、失败时日志和退出码。测试不使用真实凭证，不创建用户的 Windows 计划任务；用户机器的实际定时触发仍须按第 4 节验证。

## 10. 并发设置（新版）

默认同时处理最多 3 场，下载和转码均可并行；附件上传最多并行 3 场，应用身份素材请求共享 250 ms 间隔。范围补采仍逐日执行，不会同时启动多个日期的并发池。每日任务不加参数即使用 3 场；若需降低占用，在任务的“添加参数”末尾增加 `-Workers 1` 或 `-Workers 2`。范围脚本也支持 `-Workers 3`。

这里是一个采集进程内部并行，不能通过创建多个计划任务来替代。任务日志包含直播 ID 与下载、转码、上传阶段；一场失败后仍继续其他场次。应用身份不依赖 lark-cli 用户登录；前文关于 CLI 登录账号的要求仅适用于 `feishu.auth = "cli"`。

应用接口明确返回 HTTP 429 时，最多退避重试 3 次；上传文件重试会从原始位置重读。网络中断、服务端 5xx 等无法确认是否写入成功的写请求仍报告失败，避免盲目重复写入。
