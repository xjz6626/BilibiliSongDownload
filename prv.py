import os
import re
import httpx  # 🔥 新增：用于接管底层网络请求
from mutagen.mp4 import MP4, MP4Cover
from openai import OpenAI

# =================配置区域=================
DRY_RUN = False  
MUSIC_DIR = "./Music_Done"
REPORT_FILE = "final_execution_log.txt"

TARGET_ALBUM = "b站收藏"
TARGET_ALBUM_ARTIST = "b站收藏"

# API 配置 (从环境变量读取，避免硬编码)
API_KEY = os.getenv("SILICONFLOW_API_KEY", "").strip()
BASE_URL = "https://api.siliconflow.cn/v1"
# =========================================

if not API_KEY:
    raise RuntimeError("未检测到 SILICONFLOW_API_KEY，请先在 shell 中导出后再运行脚本。")

# 🔥 核心修复：强制忽略系统 SOCKS/HTTP 代理，直连国内 API
http_client = httpx.Client(proxy=None, trust_env=False)

# 初始化客户端时，注入我们干净的 http_client
client = OpenAI(
    api_key=API_KEY, 
    base_url=BASE_URL,
    http_client=http_client
)

# ... [下方原封不动保留你 prv.py 里的 SAFE_ARTISTS 字典和其余函数] ...
# --- 1. 歌手数据库 (修正组合映射为列表) ---
SAFE_ARTISTS = {
    "洛天依": ["洛天依"], "言和": ["言和"], "乐正绫": ["乐正绫"],
    "乐正龙牙": ["乐正龙牙"], "墨清弦": ["墨清弦"], "徵羽摩柯": ["徵羽摩柯"],
    "星尘": ["星尘"], "海伊": ["海伊"], "苍穹": ["苍穹"], "赤羽": ["赤羽"], "诗岸": ["诗岸"],
    "牧心": ["牧心"], "初音未来": ["初音未来"], "初音": ["初音未来"], "Miku": ["初音未来"],
    "镜音双子": ["镜音连", "镜音铃"], "镜音连": ["镜音连"], "镜音铃": ["镜音铃"],
    "巡音流歌": ["巡音流歌"], "巡音": ["巡音流歌"], "GUMI": ["GUMI"],
    "重音Teto": ["重音Teto"], "重音テト": ["重音Teto"], "Teto": ["重音Teto"],
    "心华": ["心华"], "起氏双子": ["起氏双子"],
    "南北组": ["洛天依", "乐正绫"], "龙墨": ["乐正龙牙", "墨清弦"], "言洛": ["言和", "洛天依"],
    "Vsinger全员": ["洛天依", "言和", "乐正绫", "乐正龙牙", "墨清弦", "徵羽摩柯"]
}

STRICT_ALIASES = {
    "洛": ["洛天依"], "言": ["言和"], "绫": ["乐正绫"], "龙": ["乐正龙牙"], 
    "牙": ["乐正龙牙"], "墨": ["墨清弦"], "摩": ["徵羽摩柯"], "柯": ["徵羽摩柯"],
    "尘": ["星尘"], "星": ["星尘"], "海": ["海伊"], "苍": ["苍穹"], 
    "赤": ["赤羽"], "诗": ["诗岸"], "岸": ["诗岸"], "葱": ["初音未来"],
    "连": ["镜音连"], "铃": ["镜音铃"]
}

def scan_artists(text):
    """本地字典精准匹配歌手"""
    found = []
    # 1. 扫描全名
    for name, target_list in SAFE_ARTISTS.items():
        if name in text:
            for t in target_list:
                if t not in found:
                    found.append(t)
    
    # 2. 扫描括号内的简称
    brackets = re.findall(r'(\[.*?\]|【.*?】|\(.*?\)|（.*?）)', text)
    for b in brackets:
        parts = re.split(r'[^\w\u4e00-\u9fa5]', b) 
        for p in parts:
            if p in STRICT_ALIASES:
                for full in STRICT_ALIASES[p]:
                    if full not in found:
                        found.append(full)
    return found

def clean_title_with_ai(filename):
    """调用 Qwen3-8B 清洗歌名，并彻底粉碎残余的 think 标签"""
    prompt = f"""
你是一个无情的文本提取器。请从下面的原始音频文件名中，剥离出最纯净的核心【歌曲名】。

剥离规则：
1. 移除所有的前缀、后缀、修饰词（如：PV付、原创、翻唱、Cover、无损、初投稿、生贺、换源完毕等）。
2. 移除其中包含的歌手名字或组合名。
3. 如果歌名被《》、「」包围，请优先提取里面的内容，并去掉符号。
4. 绝对只输出最终的纯净歌名，不要返回任何解释，不要带引号，不要输出思考过程。

原始文件名："{filename}"
"""
    try:
        response = client.chat.completions.create(
            model="Qwen/Qwen3-8B", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            extra_body={"enable_thinking": False}
        )
        clean_title = response.choices[0].message.content.strip()
        
        # 🔥 新增兜底逻辑：暴力清除大模型可能漏出的 think 标签
        clean_title = re.sub(r'<think>.*?</think>', '', clean_title, flags=re.DOTALL) # 清除完整标签
        clean_title = re.sub(r'</?think>', '', clean_title) # 清除残缺标签
        clean_title = re.sub(r'\n+', '', clean_title) # 移除所有换行符（修复"吉\n\n哀歌"断层）
        
        # 兜底清理首尾残留的符号
        clean_title = re.sub(r'^["\'《「]+|["\'》」]+$', '', clean_title).strip()
        return clean_title
    except Exception as e:
        print(f"⚠️ API 请求失败，退回原名: {e}")
        return filename

def parse_file_info_hybrid(filename):
    """双引擎解析文件名"""
    name_no_ext, ext = os.path.splitext(filename)
    
    # 1. 本地代码精准提取歌手
    artist_list = scan_artists(name_no_ext)
    
    # 2. 云端 API 清洗出纯净歌名
    final_title = clean_title_with_ai(name_no_ext)
    
    artist_filename_str = "&".join(artist_list)
    
    # 3. 拼接新文件名
    if artist_filename_str:
        new_filename = f"{artist_filename_str} - {final_title}{ext}"
    else:
        new_filename = f"{final_title}{ext}"
        
    new_filename = new_filename.replace("/", "&").replace(":", "：").replace("?", "？")
    return artist_list, artist_filename_str, final_title, new_filename

def find_cover(base_path, base_name):
    """支持多种常见图片格式的封面查找"""
    for ext in ['.jpg', '.jpeg', '.png']:
        cover_path = os.path.join(base_path, base_name + ext)
        if os.path.exists(cover_path):
            return cover_path
    return None

def main():
    if not os.path.exists(MUSIC_DIR):
        print(f"❌ 找不到目录: {MUSIC_DIR}")
        return

    print(f"🚀 启动混合引擎清理... 目标文件夹: {MUSIC_DIR}")
    files = [f for f in os.listdir(MUSIC_DIR) if f.endswith('.m4a')]
    files.sort()
    total = len(files)
    processed = 0
    
    with open(REPORT_FILE, "w", encoding="utf-8") as log:
        for filename in files:
            artist_list, artist_str, title, new_filename = parse_file_info_hybrid(filename)
            
            file_path = os.path.join(MUSIC_DIR, filename)
            new_path = os.path.join(MUSIC_DIR, new_filename)
            
            try:
                # === 1. 写标签 (Metadata) ===
                audio = MP4(file_path)
                
                # 写入标题 (规范：传入列表)
                audio['\xa9nam'] = [title]
                
                # 写入多位歌手 (规范：直接传入列表)
                if artist_list: 
                    audio['\xa9ART'] = artist_list
                
                # 写入统一的专辑信息
                audio['\xa9alb'] = [TARGET_ALBUM]      
                audio['aART'] = [TARGET_ALBUM_ARTIST]  
                
                # === 2. 嵌入封面 (如果存在) ===
                base_name = os.path.splitext(filename)[0]
                cover_path = find_cover(MUSIC_DIR, base_name)
                
                if cover_path:
                    try:
                        with open(cover_path, "rb") as f:
                            fmt = MP4Cover.FORMAT_PNG if cover_path.lower().endswith('.png') else MP4Cover.FORMAT_JPEG
                            audio["covr"] = [MP4Cover(f.read(), imageformat=fmt)]
                    except Exception as img_err:
                        print(f"⚠️ 封面读取失败: {filename} -> {img_err}")
                
                # 统一保存一次，减少 I/O 占用
                audio.save()
                
                # === 3. 文件重命名 (Rename) ===
                if filename != new_filename:
                    # 只有文件名真的变了才改名，防止覆盖已有文件
                    if not os.path.exists(new_path): 
                        os.rename(file_path, new_path)
                        log.write(f"✅ 改名: {filename} -> {new_filename}\n")
                    else:
                        print(f"⚠️ 目标文件名已存在，跳过重命名: {new_filename}")
                else:
                    log.write(f"✅ 标签更新(名未变): {filename}\n")
                
                processed += 1
                # 优化进度显示，实时看到当前处理的歌名
                print(f"⏳ 进度: {processed}/{total} | 处理完成: {new_filename}")
                    
            except Exception as e:
                err_msg = f"❌ 失败: {filename} -> {e}"
                print(err_msg)
                log.write(err_msg + "\n")

    print(f"\n🎉 大功告成！共处理 {processed} 个文件。")
    print(f"📁 详细日志已保存至 {REPORT_FILE}")
    print("📱 标签规范完毕，请使用 ADB 将 Music_Done 文件夹覆盖至手机测试效果！")

if __name__ == "__main__":
    main()