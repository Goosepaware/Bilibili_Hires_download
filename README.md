# bili-audio

B 站视频音频下载器，重点是 **Hi-Res 无损（FLAC 30251）** 与 **杜比全景声（E-AC3 30250）** 的获取，以及对下载结果做真实的音质核验，识别「假无损 / 升频 / 有损转码」。

## 特性

- 直连 B 站接口（WBI 签名），不依赖 yt-dlp / BBDown
- 扫码登录，Cookie 用 Windows DPAPI 加密保存，不落明文
- 音轨优先级：`Hi-Res 无损 FLAC` > `杜比全景声`（可选）> `最高 AAC`（回退）
- DASH fMP4 → FLAC **只转封装不重编码**（`-c:a copy`）
- 分片并发下载、断点续传、流地址过期自动重新取流
- 写入标签与封面（标题 / UP 主 / 曲号 / 来源 BV）
- **真伪核验**：ffmpeg 解码 + 纯 Python FFT，测量有效带宽与截断陡峭度，给出结论和频谱图
- **音源质检**：粘贴链接即可远程采样打分（1-100），逐段给出评语，完全不下载整首歌
- **自动分轨**：静音检测 + 波形图手动微调，或直接粘贴简介/评论里的曲目表按时间切分；自动编号、写标签
- **多格式输出**：FLAC / ALAC / WAV / MP3 / AAC / iPod 专用（ALAC 16bit/44.1k），有损格式带低中高预设
- 下载进度条、实时 MB/s 与剩余时间；自动记住上次的输出目录
- 完成后可直接打开下载文件夹；分轨支持自定义歌手、专辑与封面
- 命令行 + PySide6 图形界面（三个标签页：下载 / 音质检测 / 自动分轨）

## 安装

```powershell
pip install -r requirements.txt
# ffmpeg：放到项目目录，或加入 PATH，或设置 BILI_AUDIO_FFMPEG
winget install Gyan.FFmpeg
```

## 使用

```powershell
# 登录（二维码图片保存到 %APPDATA%\bili-audio\login_qr.png，手机 B 站扫码）
.\ba.cmd login

# 不方便扫码时：导入 cookies.txt，或直接粘贴 SESSDATA
.\ba.cmd login --cookie-file cookies.txt
.\ba.cmd login --sessdata "xxxx%2Cxxxx"

# 查看账号 / 大会员状态
.\ba.cmd whoami

# 只解析，看看这个视频有哪些音轨
.\ba.cmd info BV1xxxxxxxxx

# 下载（默认下载所有分 P）
.\ba.cmd dl BV1xxxxxxxxx

# 同时保留杜比全景声、指定输出目录、8 线程
.\ba.cmd dl "https://www.bilibili.com/video/BV1xxxxxxxxx" -o D:\Music\bili --atmos -t 8

# 只下第 2、5~7 个分 P
.\ba.cmd dl BV1xxxxxxxxx -p 2,5-7

# 核验本地文件
.\ba.cmd verify "D:\Music\bili\xxx.flac"

# 在线质检：远程采样打分（1-100），不下载整首歌
.\ba.cmd score BV1xxxxxxxxx
.\ba.cmd score BV1xxxxxxxxx -s 12 --json report.json

# 自动分轨：第一种，静音检测（会下载整轨到临时目录）
.\ba.cmd split BV1xxxxxxxxx --threshold -35 --min-silence 1.2

# 自动分轨：第二种，按曲目表文本（不下载整轨，直接远程切）
.\ba.cmd split BV1xxxxxxxxx --list tracklist.txt

# 图形界面
.\ba.cmd gui
```

也可以直接双击 `main.pyw`，或双击 `ba.cmd`（不带参数时都会打开图形界面）。

## 输出

- 单 P：`输出目录/视频标题.flac`（或 `.m4a` / `.eac3`）
- 多 P：`输出目录/视频标题/01 分P标题.flac`
- 每个音频旁边有同名 `.json`，记录实际音轨 ID、码率、采样率、位深、有效带宽与结论
- 核验时额外生成 `xxx.flac.spectrum.png` 频谱图

## 音源质检（打分 1-100）

`score` 不会下载整首歌，而是用 ffmpeg 通过 HTTP Range 在多个时间点直接读取几百毫秒音频做 FFT：

- 按视频长度自动分段（每段约 3 分钟，最多 12 段），每段取三个位置、挑最响的一段，避免前奏/淡出误判
- 每段测：有效带宽、相对 Nyquist 比例、截断陡峭度、峰值、削波比例
- 综合评分维度：编码是否无损、有效带宽、是否有硬截断、位深、削波、分段一致性
- 每段输出一句评语，例如：
  `01:27–01:56 有效带宽 18.1 kHz（Nyquist 的 76%），陡峭度 0.55。高频偏窄，疑似有损来源或升频。`

评分参考：

| 分数 | 含义 |
| --- | --- |
| 90-100 | 优秀，真无损，Hi-Res 带宽完整 |
| 80-89 | 良好，无损但带宽约 CD 级 |
| 65-79 | 一般，高频偏窄，疑似有损来源 |
| 45-64 | 存疑，高频明显缺失 |
| <45 | 差，基本是假无损或有损音轨 |

> 评分是工程启发式，用来快速筛掉明显造假的音源；最终仍建议结合 `.spectrum.png` 频谱图判断。

## 自动分轨

界面第三个标签页，或命令行 `ba split`。

**方式一：静音检测**

1. 载入音轨（支持 FLAC / 杜比 / AAC）
2. 点「检测静音并显示波形」——会把整轨下载到 `输出目录/.bili_audio_tmp`，导出后自动删除
3. 波形上显示每段的切点，拖动橙色标记可微调；也可「在最大空白处加分割点」、选中后按 Delete 删除
4. 导出：`输出目录/专辑名/01.flac`、`02.flac`……，并写入 `tracknumber` 标签

**方式二：曲目表文本**

很多整轨专辑的简介/置顶评论里都有这样的列表：

```
1. Sunset (2019 Y.Nakamura Remastering) 0:00
2. Syndrome 3:24
...
15. Flash 1:00:01
16. ひとひら 1:04:27
```

整段粘贴进去点「识别曲目」，软件会尽量兼容不同写法：

- 序号：`1.` `01` `1、` `1)` `Track 5` `【03】`
- 时间：`3:24`、`1:04:27`、全角冒号、时间在行首或行尾
- 歌名：去掉序号/时间后剩下的整段文本

识别结果会先显示成表格（序号/歌名/开始/结束/时长），确认无误再导出。这个模式**不需要预先下载整轨**，导出时按时间段直接远程读取，只传输需要的部分。勾选「用识别到的歌名命名文件」后得到 `01 Sunset (...).flac`。

> 分轨导出时：FLAC 源用无损重编码（`-c:a flac`，不改变音质），AAC/E-AC3 源直接 `-c:a copy`。

**分轨元数据**

导出前可以自定义：

- **歌手**：默认取 UP 主名
- **专辑**：默认取视频标题（同时作为导出文件夹名）
- **封面**：选择一个本地图片，会嵌入每一首分轨（FLAC / MP3 / M4A / WAV）

命令行对应参数：

```powershell
.\ba.cmd split BV1xxxx --list tracklist.txt `
  --album "专辑名" --artist "歌手名" --cover cover.jpg
```

下载或分轨完成后，完成弹窗里有「打开文件夹」按钮，可以直接在资源管理器里定位到输出目录。

## 输出格式

`dl` 和 `split` 都支持以下格式（`-f/--format`），有损格式用 `--preset low|medium|high` 选择参数，默认 **medium**。

| 格式 | 扩展名 | 预设 | 参数 |
| --- | --- | --- | --- |
| `flac`（默认） | `.flac` | 原样 | 保持源采样率/位深，转封装不重编码 |
| `ipod` | `.m4a` | 兼容预设 | **ALAC · 44.1kHz · 16bit · 立体声 · triangular_hp 抖动** |
| `alac` | `.m4a` | 原样 / CD 兼容 | Apple Lossless |
| `wav` | `.wav` | 16bit/44.1k、24bit/原采样率 | PCM 未压缩，体积很大 |
| `mp3` | `.mp3` | 低 V5 / 中 V2 / 高 V0 | LAME VBR，约 130 / 190 / 245 kbps |
| `aac` | `.m4a` | 低 128k / 中 192k / 高 256k | ffmpeg AAC |
| `source` | 源格式 | — | 只用于 `dl`：保留原始流（FLAC/AAC/E-AC3） |

```powershell
.\ba.cmd dl BV1xxxxxxxxx -f mp3 --preset high
.\ba.cmd dl BV1xxxxxxxxx -f ipod
.\ba.cmd dl BV1xxxxxxxxx -f wav --preset cd
.\ba.cmd split BV1xxxxxxxxx --list tracklist.txt -f ipod
.\ba.cmd split BV1xxxxxxxxx --list tracklist.txt -f mp3 --preset low
```

**iPod 专用预设**按文档实现：ALAC / `.m4a` / 44.1kHz / 16bit / 立体声；降位深时用高质量抖动；不做响度归一化、EQ、动态压缩或淡入淡出；保留标题、艺人、专辑、曲目编号与封面。若源本身是 44.1kHz/16bit，则不会做无意义的重采样。

> 注意：MP3/AAC 是有损格式，转过去会再次损失音质；AAC 源转无损格式也不会恢复已丢失的信息，工具会明确提示。分轨默认「跟随源格式」，需要统一格式时再显式选择。

## 打包 exe

已打包三个版本：

```
D:\program\dist\bili-audio\bili-audio.exe                      # 图形界面（文件夹版），双击即开，无黑框
D:\program\dist\bili-audio-cli\bili-audio-cli.exe              # 命令行版本
D:\program\dist\bili-audio-portable\bili-audio-portable.exe    # 单文件便携版，内含 ffmpeg
```

- **GUI 版是 Windows GUI 子系统程序**，不会创建控制台窗口，因此没有黑框闪烁
- **便携版只有一个 exe（约 126 MB）**，ffmpeg 已打包在内，可以单独拷到任何地方运行；首次启动会解压到临时目录，稍慢几秒
- 命令行使用 `bili-audio-cli.exe`，例如：
  ```powershell
  .\bili-audio-cli\bili-audio-cli.exe whoami
  .\bili-audio-cli\bili-audio-cli.exe dl BV1xxxx -f ipod
  ```
- 文件夹版同目录的 `_internal` 文件夹和 `ffmpeg.exe` 必须一起保留，不能只拷 exe
- 下载页和分轨页会记住上次使用的输出目录（`%APPDATA%\bili-audio\config.json`），下次打开自动填入
- 重新打包：

```powershell
# GUI（无控制台）
pyinstaller --noconfirm --onedir --windowed --name bili-audio `
  --distpath D:\program\dist --workpath build_gui --specpath build_gui main.py

# CLI（控制台）
pyinstaller --noconfirm --onedir --console --name bili-audio-cli `
  --exclude-module PySide6 --exclude-module shiboken6 `
  --distpath D:\program\dist --workpath build_cli --specpath build_cli main.py

# 单文件便携版（内含 ffmpeg）
pyinstaller --noconfirm --onefile --windowed --name bili-audio-portable `
  --add-binary "D:\program\bili_audio\ffmpeg.exe;." `
  --distpath D:\program\dist\bili-audio-portable `
  --workpath build_portable --specpath build_portable main.py
```

> 未签名的 exe 首次运行可能被 SmartScreen 提示，选择「更多信息 → 仍要运行」即可。

## 核验结论怎么读

| 结论 | 含义 |
| --- | --- |
| 无损（带宽充足） | 容器是 FLAC，且频谱能量接近 Nyquist（或明显超过 22kHz），没有硬截断 |
| 无损封装，但高频带宽受限 | 是无损封装，有效带宽约 19~22kHz（常见于 44.1kHz 源或做过降频） |
| 疑似升频 | 有硬截断且带宽低于当前采样率，例如 44.1kHz 源被转成 48kHz/96kHz |
| 疑似假无损 | 有效带宽只有 16~19kHz，或截断明显，源头很可能是有损音源 |
| 疑似假无损（硬截断） | 高频存在陡峭截断，基本可以确定来自有损编码（MP3/AAC） |
| 非无损编码 | 本身是 AAC / E-AC3，不可能是无损 |

> 说明：核验只能判断「这条音轨是否被有损压缩或升频过」，无法证明它的母带来源。真正的 Hi-Res 也取决于 UP 主是否上传了无损源；绝大多数普通视频最高只有 192K AAC。

## 说明与边界

- 需要大会员账号才能拿到 `30251` FLAC；非大会员会明确提示并回退 AAC（可用 `--no-aac` 关闭回退）
- 第一版只支持普通投稿视频（BV/av/短链）与多 P；番剧 PGC、付费课程、充电专属未适配
- 下载的视频音频仅供个人学习与备份，请遵守 B 站用户协议与著作权法
- 音质打分仅供参考，打分根据高频混叠截断来判断，歌曲EQ，LIVE版，录音室质量都肯能影响分数，不代表真实音源质量
