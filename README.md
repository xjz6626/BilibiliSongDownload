# Bilibili Music Sync Tool (B站音乐同步工具)

这是一个专为 Bilibili 音乐爱好者设计的自动化工具链。它可以自动从 B站收藏夹下载音乐，进行智能文件名清洗、元数据（标签）写入、封面嵌入，并最终通过 ADB 同步到 Android 手机。

## ✨ 主要功能

1.  **自动下载**: 使用 `yt-dlp` 从 B站收藏夹批量下载音频 (.m4a)。
2.  **智能清洗 (`prv.py`)**:
    *   **文件名净化**: 结合视频原标题和简介提取歌名，自动去除 "PV付"、"原创"、"1080P" 等无关修饰词；标题仍是主要依据，简介用于补全和消歧。
    *   **翻唱识别**: 从标题及简介识别 `翻唱`、`Cover`、`歌ってみた`、`カバー`、`试唱`、`翻调`、`首翻` 等强信号，并统一显示为 `[翻唱]`。
    *   **歌手提取**: 标题中的歌手结果优先且不被简介扩充；仅当标题没有结果时，才从简介里明确的演唱、Vocal、歌姬等字段兜底（支持 Vsinger、五维介质、日本 V家等），并写入音频标签。
    *   **格式统一**: 将最终文件标准化为 `歌手 - 歌名.m4a`，文件名和 Title 标签均不包含 BVID。
3.  **元数据完善**:
    *   自动写入 `Title` (标题)、`Artist` (艺术家)、`Album` (统一为 "b站收藏")。
    *   翻唱歌曲会在 Title 中追加 `[翻唱]`，同时写入 Grouping=`翻唱` 和隐藏类型标签。
    *   将完整的 B 站原始视频标题保存到 `Comment` (备注) 标签，便于追溯原视频。
    *   将用于解析的视频简介保存到隐藏的 `BilibiliDescription` 自定义标签，不占用 Comment，也不会挤进可见歌名。
    *   分别保存 BVID、CID、分 P 标题、UP 主/UID、投稿日期与带时区的精确投稿时间；分 P 的稳定身份使用 `BV + CID`，不依赖可能重排的 P 序号。
    *   从标题和简介里保守提取作词、作曲、编曲、调教、混音、母带、制作人、曲绘和 PV 等 Staff。标准作曲标签与分角色标签供播放器使用，完整数组、原始角色和证据保留在 `BilibiliCredits` JSON 中；UP 主不会被误当成 Artist。
    *   **封面嵌入**: 自动将下载的视频封面嵌入到音频文件中。
4.  **崩溃安全的增量同步**: 在临时副本中写入并校验标签，再原子替换源文件；处理中断后会依据音频内嵌状态继续改名，不会把半成品误判为完成。
5.  **逐文件 ADB 提交**: 每首歌推送成功后才清理对应本地文件；失败歌曲保留，下次覆盖重推。
6.  **并发保护**: 整条同步流程和 Python 处理器均有互斥锁，避免两个实例互相覆盖或误删文件。

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
pip3 install -r requirements.txt

# 安装 yt-dlp (推荐使用最新版)
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

### 2. 配置

1.  **修改 `sync-nusic.fish`**:
    *   `COOKIES_BROWSER`: 设置为你使用的浏览器 (如 `chrome`, `edge`, `firefox`)，用于提取 B站 Cookies。
    *   `REMOTE_PATH`: 修改为你手机上的音乐存储路径 (例如 `/sdcard/Music/Bilibili/`)。
    *   `ADB_SERIAL`: 只有同时连接多台 Android 设备时需要填写，可通过 `adb devices` 查看。
    *   `DOWNLOAD_URL`: 要下载的 B站收藏夹链接。
    *   `PLAYLIST_ITEMS`: 默认是 `1:100`（收藏夹前 100 个），也可改为 `1:50`、`51:100` 等范围。
    *   `DEEPSEEK_API_KEY`: 你的 DeepSeek API Key（`prv.py` 从环境变量读取，不硬编码）。
    *   `DEEPSEEK_BASE_URL`: OpenAI 兼容 API 地址，默认 `https://api.deepseek.com`。当前代码使用 OpenAI SDK，不能填写 Anthropic 协议地址。
    *   `DEEPSEEK_MODEL`: 默认 `deepseek-v4-flash`，也可切到 `deepseek-v4-pro`。

2.  **准备 B站列表**:
    *   在浏览器登录 B站。
    *   将想下载的歌曲视频加入配置的收藏夹。

### 3. 运行

连接手机并开启 USB 调试，然后在终端运行：

```fish
# 默认检查收藏夹前 100 个
./sync-nusic.fish

# 检查前 100 个
./sync-nusic.fish 100

# 检查第 51 到 100 个
./sync-nusic.fish 51:100

# 只检查第 1、3、8 个
./sync-nusic.fish 1,3,8

# 查看参数帮助
./sync-nusic.fish --help
```

脚本内的 `PLAYLIST_ITEMS` 使用 yt-dlp 的 `--playlist-items` 语法：`[START]:[STOP][:STEP]`，支持逗号组合、负数索引以及兼容格式 `START-STOP`。命令行参数是临时覆盖方式；单独传入正整数 `N` 会自动转换为 `1:N`。

脚本将自动执行以下步骤：
1.  检查手机连接。
2.  下载阶段临时以 `标题 [BVID].m4a` 保存新歌，把封面统一转换为 JPEG，并用同名 `.info.json` 保存包括简介在内的视频元数据。
3.  Python 结合标题和简介提取歌名、歌手及翻唱信息，在临时副本中写入标签和封面，校验后原子提交；成功后清理信息边车，最终可见文件名会移除 BVID。
4.  将处理好的 `.m4a` 逐首推送到手机。
5.  单首推送成功才删除该文件；失败项保留供下次重试。

下载阶段仍会继续尝试收藏夹中的其他项目。若 B 站活动视频的普通 `/video/` 页面缺少 yt-dlp 所需状态，脚本会从 B 站公开元数据中取得并严格校验官方活动页地址，再自动重试；其他包含 `ERROR:` 的下载仍会让本次脚本最终返回非零。已经成功下载和同步的歌曲不受影响；未解决的视频不会写入下载归档，下次运行会再次尝试。最近一次 yt-dlp 输出保存在 `Music_Done/.yt-dlp-last.log`。

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
*   `credits.py`: 保守的 Staff 角色提取器，仅接受有明确角色标签和文本证据的人名。
*   `migrate_phone_library.py`: 旧手机曲库的只读目录、审阅计划、备份后补标签与独立伴奏删除工具。
*   `bilibili_festival_retry.py`: 只处理 yt-dlp 无法从普通视频页读取状态的活动视频，将其解析为 B 站官方活动页后重试。
*   `archive.txt`: `yt-dlp` 的下载记录文件，防止重复下载历史歌曲。
*   `Music_Done/`: 临时文件夹，用于存放处理好的音乐文件。
    *   `.song-name-registry.sqlite3`: 隐藏的持久命名注册表，用于跨批次为同名歌曲稳定分配 `(2)`、`(3)`；不会推送到手机。
*   `final_execution_log.txt`: 追加式处理日志，保留每次运行的成功、恢复、跳过和失败记录。

## 📝 注意事项

*   **Cookies**: `yt-dlp` 需要读取浏览器 Cookies 才能下载高音质音频或访问非公开收藏夹。请确保在运行脚本的机器上已通过浏览器登录 B站。
*   **歌手映射**: `prv.py` 中内置了 V 家歌手映射表，可以根据需要修改 `SAFE_ARTISTS` 和 `STRICT_ALIASES`。标题已有歌手时不会受简介里的曲目表、其他版本或活动阵容干扰；标题没有歌手时，才检查简介中明确的演唱/Vocal/歌姬字段。两处都无法识别时仍保留空 Artist，不会猜测。
*   **同名歌曲**: BVID 只保存在音频隐藏元数据和本地注册表中，不出现在最终歌名。不同歌姬的同名歌天然使用不同的 `歌姬 - 歌名`；原曲与翻唱也会因 `[翻唱]` 标记而区分。只有最终完整名称均相同时，才会稳定追加 `(2)`、`(3)`。该编号跨批次保留，不会在下一次同步时重置。
*   **翻唱边界**: `Staff`、`中文填词`、`中文翻填`、`Remix`、`本家投稿` 单独出现时不标记翻唱；`翻唱/翻调/Cover` 的授权、禁止和素材下载说明也不算当前作品是翻唱。`翻唱：歌姬`、`翻调版 Staff` 等明确版本信号才参与判断；`原创PV付` 只说明 PV 是原创，不能抵消歌曲本身的翻唱信号。
*   **旧版半成品**: 旧代码仅写入统一专辑标签、但没有新的内嵌完成状态。它们留在 `Music_Done` 时会被安全地重新处理一次，以修复过去“写标签后、改名前崩溃”的半完成状态。
*   **中断恢复**: 临时文件名以 `.prv-tmp-` 开头。异常退出后，原文件保持不变或已经完整写入；失败时 `.info.json` 会保留供重试，简介写入后会通过 SHA-256 摘要校验，成功提交后才清理边车。下次运行会清理无效临时副本，并完成尚未完成的改名。

## 📱 补全手机旧曲库

`migrate_phone_library.py` 的默认动作只有读取手机和生成计划。它只以手机当前仍存在的 M4A 为迁移范围；`archive.txt` 只用于查元数据，不会把你已从手机删掉的伴奏或歌曲补回去。

```bash
# 1. 仅下载 archive 对应的 info.json 和封面，不下载音频
python3 migrate_phone_library.py catalog --cookies-from-browser firefox

# 2. 只读扫描手机，结合简介生成歌名和 Staff 审阅计划
# --use-llm 会调用 prv.py 已配置的标题清洗模型，结果会即时缓存
python3 migrate_phone_library.py plan --use-llm --pilot 10 \
  --remote-dir '/sdcard/_Library/Audio/Music/v+/'

# 3. 审阅 .migration/phone-library-plan.json 和同名 CSV，记下输出的 plan_hash

# 4. 首次只补 10 首标签，不改文件名
python3 migrate_phone_library.py apply --pilot 10 --confirm-hash '<plan_hash>'
```

`apply` 会先把每首原件按 SHA-256 备份到 `.migration/backups/<plan_hash>/`，确认 M4A 的音频 `mdat`、封面、歌词和未受管自定义标签没有变化，再推送到同目录隐藏临时名。临时文件和提交后的最终文件都会校验整文件 SHA-256，最后还会重新拉回检查标签和音频流。

少数旧的 fragmented M4A 会被探测成 `0` 秒。对此工具只在“封面唯一、不是多 P、规范化后的现有标题至少 4 个字符，并与 B 站原标题互相完整包含”时自动匹配；真实的非零时长冲突、短标题和任何多候选情况仍留给人工复核。每首成功回读验证后，本地修改副本和手机端事务临时文件都会立即释放，批量处理不会持续占满临时目录。

首次 pilot 不要在 `plan` 加 `--rename-files`，也不要在 `apply` 加 `--allow-rename`。原件可以从备份按字节恢复，但 Android MediaStore 或 Musicolet 是否把“改路径”当成新歌，只能通过真机确认。请在 Musicolet 里检查这 10 首的播放、拖动、封面、收藏和播放列表关联，再决定是否继续或单独测试改名。

伴奏关键词只会产生 `delete_candidate`，不会被 `apply` 删除。删除是另一个命令，且必须逐首填入完整文件名：

```bash
python3 migrate_phone_library.py delete \
  --confirm-hash '<plan_hash>' \
  --allow-delete \
  --confirm-file '已人工确认的伴奏版.m4a'
```

删除前也会保留完整的 PC 原件备份。“干音、Demo、Remix、音乐纯享”等不会因为分 P 序号被自动删除；同封面多 P、CID 冲突、时长不符和目录缺页都会转人工审阅。

## 🧪 运行测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile prv.py credits.py migrate_phone_library.py chaeck.py check_covers.py
fish -n sync-nusic.fish.example
```

测试还包括 Staff 角色/证据与提示注入反例、v2/v3 状态兼容、BV+CID 分 P 身份、同封面多 P 歧义、时长不符、plan 篡改、备份/回读校验、音频流不变、封面/歌词/未受管标签保留，以及伴奏必须逐文件确认。

## 🤝 贡献

欢迎提交 Issue 或 Pull Request 来改进文件名解析规则或增加新功能！
