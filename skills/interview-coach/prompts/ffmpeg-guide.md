## 在 Windows 上安装 FFmpeg

FFmpeg 官网只发布源代码，官网的 Windows 页面会把可执行文件导向两个公开构建站。
推荐使用官方页面列出的 `gyan.dev` release essentials 构建：

- FFmpeg 官方下载页：https://ffmpeg.org/download.html
- Windows 构建页：https://www.gyan.dev/ffmpeg/builds/#release-builds
- 当前 release essentials ZIP 直链：https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

页面当前对应的稳定版是 FFmpeg 9.0.2。下载 64 位 release essentials 的 ZIP 文件，
解压到例如 `C:\ffmpeg`，解压后的命令程序在 `C:\ffmpeg\bin\ffmpeg.exe`。
如果你下载到本机的文件名为 `ffmpeg-9.0.2-essentials_build.zip`，内容就是这一版；
解压和后面的使用方式完全相同。

## 检查是否可以用

在 Windows PowerShell 中进入 `bin` 目录：

```powershell
cd C:\ffmpeg\bin
.\ffmpeg.exe -version
```

能打印版本信息就说明安装完成。

## 从视频提取音频

下面命令保留适合后续语音识别的单声道 16 kHz WAV：

```powershell
.\ffmpeg.exe -i "D:\面试\面试录像.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "D:\面试\面试音频.wav"
```

如果更在意文件大小，可以导出单声道 64 kbps 的 M4A：

```powershell
.\ffmpeg.exe -i "D:\面试\面试录像.mp4" -vn -ac 1 -ar 16000 -c:a aac -b:a 64k "D:\面试\面试音频.m4a"
```

参数含义：

- `-i "输入视频"`：输入文件，路径含空格时必须加双引号。
- `-vn`：不处理视频画面，只保留音频。
- `-ac 1`：转成单声道。
- `-ar 16000`：采样率设为 16 kHz。
- `-c:a pcm_s16le`：WAV 使用 16 位 PCM 编码。
- 最后的路径：输出音频文件。

注意：FFmpeg 到这里只完成了“视频 → 音频”，不能把音频转成文字；音频转文字需要后续接入语音识别模型。
