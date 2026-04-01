# Bilibili Music Sync Tool (B站音乐同步工具)

这是一个专为 Bilibili 音乐爱好者设计的自动化工具链。它可以自动从 B站的「稍后播放」列表下载音乐，进行智能文件名清洗、元数据（标签）写入、封面嵌入，并最终通过 ADB 同步到 Android 手机。

## ✨ 主要功能

1.  **自动下载**: 使用 `yt-dlp` 从 B站「稍后播放」列表批量下载音频 (.m4a)。
2.  **智能清洗 (`prv.py`)**:
    *   **文件名净化**: 自动去除 "PV付"、"原创"、"翻唱"、"1080P" 等无关关键词。
    *   **歌手提取**: 智能识别文件名中的歌手信息（支持 Vsinger、五维介质、日本 V家等），并写入音频标签。
    *   **格式统一**: 将文件名标准化为 `歌手 - 歌名.m4a` 格式。
3.  **元数据完善**:
    *   自动写入 `Title` (标题)、`Artist` (艺术家)、`Album` (统一为 "b站收藏")。
    *   **封面嵌入**: 自动将下载的视频封面嵌入到音频文件中。
4.  **增量/新歌同步**: 每次运行前清空临时目录，只处理和传输新下载的歌曲，避免重复传输。
5.  **ADB 自动传输**: 处理完成后自动推送到连接的 Android 手机指定目录。

## 🛠️ 依赖环境

*   **OS**: Linux / macOS (Windows 用户需使用 WSL 或自行修改 shell 脚本)
*   **Shell**: Fish Shell (`sync-nusic.fish`)
*   **Python**: Python 3.x
    *   依赖库: `mutagen`、`openai`、`httpx`
*   **工具**:
    *   `yt-dlp`: 强大的视频/音频下载工具。
    *   `adb`: Android Debug Bridge，用于传输文件到手机。
    *   `ffmpeg`: `yt-dlp` 处理音频转换时需要。

## 🚀 快速开始

### 1. 安装依赖

```bash
# Ubuntu/Debian
sudo apt install fish python3 python3-pip adb ffmpeg

# 安装 Python 依赖
pip3 install mutagen openai httpx

# 安装 yt-dlp (推荐使用最新版)
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

### 2. 配置

1.  **修改 `sync-nusic.fish`**:
    *   `COOKIES_BROWSER`: 设置为你使用的浏览器 (如 `chrome`, `edge`, `firefox`)，用于提取 B站 Cookies。
    *   `REMOTE_PATH`: 修改为你手机上的音乐存储路径 (例如 `/sdcard/Music/Bilibili/`)。
    *   `WATCH_LATER_URL`: 默认为 B站稍后播放列表，也可以改为其他收藏夹链接。
    *   `SILICONFLOW_API_KEY`: 填入你的硅基流动 API Key（`prv.py` 会从环境变量读取，不再在代码中硬编码）。

2.  **准备 B站列表**:
    *   在浏览器登录 B站。
    *   将想下载的歌曲视频加入「稍后播放」。

### 3. 运行

连接手机并开启 USB 调试，然后在终端运行：

```fish
./sync-nusic.fish
```

脚本将自动执行以下步骤：
1.  清空 `Music_Done` 文件夹。
2.  检查手机连接。
3.  下载新歌。
4.  运行 Python 脚本清洗数据、写入标签和封面。
5.  将处理好的音乐推送到手机。

## 🔧 辅助工具

除了主流程脚本外，本项目还包含两个实用的辅助脚本：

### 1. `_getlist.py` (收藏夹抓取)
*   **功能**: 获取指定 B站收藏夹内的所有视频链接，并保存到 `all_songs.txt` 文件中。
*   **用途**: 用于备份收藏夹列表，或者获取视频列表供其他下载工具使用。
*   **配置**: 需要在脚本内填入 `SESSDATA` (Cookie) 和 `MEDIA_ID` (收藏夹 ID)。

### 2. `chaeck.py` (元数据检查)
*   **功能**: 随机抽取 `Music_Done` 文件夹中的 5 首歌曲，检查其元数据（Tags）是否正确写入。
*   **用途**: 验证 `prv.py` 的清洗效果，确保歌手信息 (`Artist`) 被正确存储为列表格式，且专辑名 (`Album`) 正确。
*   **运行**: 直接执行 `python chaeck.py`。

## 📂 文件结构

*   `sync-nusic.fish`: 主控脚本，负责流程控制、下载和传输。
*   `prv.py`: Python 核心处理脚本，负责文件名解析、标签写入和封面嵌入。
*   `archive.txt`: `yt-dlp` 的下载记录文件，防止重复下载历史歌曲。
*   `Music_Done/`: 临时文件夹，用于存放处理好的音乐文件。

## 📝 注意事项

*   **Cookies**: `yt-dlp` 需要读取浏览器 Cookies 才能下载高音质音频或访问稍后播放列表。请确保在运行脚本的机器上已通过浏览器登录 B站。
*   **歌手映射**: `prv.py` 中内置了详细的 V家歌手映射表（如 "天依" -> "洛天依"），你可以根据需要自行修改 `ARTIST_MAP`。

## 🤝 贡献

欢迎提交 Issue 或 Pull Request 来改进文件名解析规则或增加新功能！
