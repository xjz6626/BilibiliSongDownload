import os
import re
from mutagen.mp4 import MP4, MP4Cover

# =================配置区域=================
# ⚠️ 注意：已开启实战模式！直接修改文件！
DRY_RUN = False  
MUSIC_DIR = "./Music_Done"
REPORT_FILE = "final_execution_log.txt"

# 🔥 新增：统一的专辑名称
TARGET_ALBUM = "b站收藏"
TARGET_ALBUM_ARTIST = "b站收藏"
# =========================================

# --- 1. 歌手数据库 ---
SAFE_ARTISTS = {
    "洛天依": "洛天依", "言和": "言和", "乐正绫": "乐正绫",
    "乐正龙牙": "乐正龙牙", "墨清弦": "墨清弦", "徵羽摩柯": "徵羽摩柯",
    "星尘": "星尘", "海伊": "海伊", "苍穹": "苍穹", "赤羽": "赤羽", "诗岸": "诗岸",
    "牧心": "牧心", "初音未来": "初音未来", "初音": "初音未来", "Miku": "初音未来",
    "镜音双子": "镜音双子", "镜音连": "镜音连", "镜音铃": "镜音铃",
    "巡音流歌": "巡音流歌", "巡音": "巡音流歌", "GUMI": "GUMI",
    "重音Teto": "重音Teto", "重音テト": "重音Teto", "Teto": "重音Teto",
    "心华": "心华", "起氏双子": "起氏双子",
    "南北组": "洛天依&乐正绫", "龙墨": "乐正龙牙&墨清弦", "言洛": "言和&洛天依",
    "Vsinger全员": "Vsinger All Stars"
}

STRICT_ALIASES = {
    "洛": "洛天依", "言": "言和", "绫": "乐正绫", "龙": "乐正龙牙", 
    "牙": "乐正龙牙", "墨": "墨清弦", "摩": "徵羽摩柯", "柯": "徵羽摩柯",
    "尘": "星尘", "星": "星尘", "海": "海伊", "苍": "苍穹", 
    "赤": "赤羽", "诗": "诗岸", "岸": "诗岸", "葱": "初音未来",
    "连": "镜音连", "铃": "镜音铃"
}

# --- 2. 垃圾关键词 ---
JUNK_WORDS = [
    "PV付", "pv付", "PV附", "pv附", "MV", "Official", "Video",
    "feat.", "ft.", "bilibili", "B站", "出品", "Hi-Res", "无损",
    "中文填词", "填词", "翻唱", "Cover", "cover", "Edition", "Ver.", "Ver"
]

def scan_artists(text):
    found = []
    # 1. 扫描全名
    for name, full_name in SAFE_ARTISTS.items():
        if name in text:
            if full_name not in found:
                found.append(full_name)
    
    # 2. 扫描括号内的简称
    brackets = re.findall(r'(\[.*?\]|【.*?】|\(.*?\)|（.*?）)', text)
    for b in brackets:
        parts = re.split(r'[^\w]', b)
        for p in parts:
            if p in STRICT_ALIASES:
                full = STRICT_ALIASES[p]
                if full not in found:
                    found.append(full)
    return list(set(found)) # 简单去重

def remove_artists_from_text(text, artist_list):
    cleaned = text
    targets = [k for k in SAFE_ARTISTS.keys()]
    targets.sort(key=len, reverse=True)
    
    for artist_name in targets:
        should_remove = False
        target_full_name = SAFE_ARTISTS[artist_name]
        if target_full_name in artist_list:
            should_remove = True
            
        if should_remove:
            # 移除 artist 名字及其前缀连接符
            pattern = re.compile(r'(?i)(feat\.?|ft\.?|&|with|vs)?\s*' + re.escape(artist_name), re.IGNORECASE)
            cleaned = pattern.sub('', cleaned)
    return cleaned

def clean_filename_structure(text, artist_list):
    final_title = ""
    # 优先提取书名号或引号内的内容
    match_book = re.search(r'《(.*?)》', text)
    match_corner = re.search(r'「(.*?)」', text)
    match_quote = re.search(r'[“"＂](.*?)[”"＂]', text)
    
    if match_book:
        final_title = match_book.group(1).strip()
    elif match_corner:
        final_title = match_corner.group(1).strip()
    elif match_quote:
        final_title = match_quote.group(1).strip()
    else:
        # 没有书名号，执行强力清理
        cleaned = re.sub(r'(\[.*?\]|【.*?】)', '', text)
        cleaned = re.sub(r'(?i)\((PV|MV|Official|Video|出品|Cover|翻唱).*?\)', '', cleaned)
        cleaned = re.sub(r'(?i)（(PV|MV|Official|Video|出品|Cover|翻唱).*?）', '', cleaned)
        for junk in JUNK_WORDS:
            cleaned = re.sub(r'(?i)' + re.escape(junk), '', cleaned)
        final_title = cleaned
        
    final_title = remove_artists_from_text(final_title, artist_list)
    # 替换特殊字符
    final_title = final_title.replace("⧸", " ").replace("|", " ")
    # 清理首尾符号
    final_title = re.sub(r'^[\s\-_+\.,#*!！?？&]+|[\s\-_+\.,#*!！?？&]+$', '', final_title)
    final_title = re.sub(r'\s+', ' ', final_title).strip()
    
    if not final_title:
        return text.strip()
    return final_title

def parse_file_info(filename):
    name_no_ext, ext = os.path.splitext(filename)
    
    # 1. 扫描歌手（返回列表）
    artist_list = scan_artists(name_no_ext)
    
    # 2. 生成用于文件名的歌手字符串（用 & 连接，因为文件名不能存列表）
    artist_filename_str = "&".join(artist_list)
    
    # 3. 清洗标题
    final_title = clean_filename_structure(name_no_ext, artist_list)

    # 4. 生成新文件名
    if artist_filename_str:
        new_filename = f"{artist_filename_str} - {final_title}{ext}"
    else:
        new_filename = f"{final_title}{ext}"
        
    new_filename = new_filename.replace("/", "&").replace(":", "：").replace("?", "？")
    
    # 返回：歌手列表(写标签用), 歌手字符串(日志用), 标题, 新文件名
    return artist_list, artist_filename_str, final_title, new_filename

def main():
    if not os.path.exists(MUSIC_DIR):
        print(f"❌ 找不到目录: {MUSIC_DIR}")
        return

    print(f"🚀 启动最终清理... 目标文件夹: {MUSIC_DIR}")
    print(f"🎵 目标标签格式 -> 歌手: 列表(List), 专辑: {TARGET_ALBUM}")
    
    files = [f for f in os.listdir(MUSIC_DIR) if f.endswith('.m4a')]
    files.sort()
    total = len(files)
    processed = 0
    
    with open(REPORT_FILE, "w", encoding="utf-8") as log:
        for idx, filename in enumerate(files):
            # 获取解析结果
            artist_list, artist_str, title, new_filename = parse_file_info(filename)
            
            file_path = os.path.join(MUSIC_DIR, filename)
            new_path = os.path.join(MUSIC_DIR, new_filename)
            
            try:
                # === 1. 写标签 (Metadata) ===
                audio = MP4(file_path)
                
                # 写入标题
                audio['\xa9nam'] = title
                
                # 🔥 写入歌手（直接传入 List！）
                # 这样播放器就能识别为多个歌手了
                if artist_list: 
                    audio['\xa9ART'] = artist_list
                
                # 🔥 写入统一的专辑信息
                audio['\xa9alb'] = TARGET_ALBUM      # 专辑名
                audio['aART'] = TARGET_ALBUM_ARTIST  # 专辑艺术家
                
                audio.save()
                
                # === 2. 嵌入封面 (如果存在) ===
                # 注意：这里要用原始 filename 找 jpg，因为还没重命名
                jpg_name = os.path.splitext(filename)[0] + ".jpg"
                jpg_path = os.path.join(MUSIC_DIR, jpg_name)
                
                if os.path.exists(jpg_path):
                    try:
                        with open(jpg_path, "rb") as f:
                            audio["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
                            audio.save()
                        # 可选：嵌入后删除 jpg
                        # os.remove(jpg_path) 
                    except Exception as img_err:
                        print(f"⚠️ 封面嵌入警告: {filename} -> {img_err}")
                
                # === 3. 文件重命名 (Rename) ===
                if filename != new_filename:
                    # 只有文件名真的变了才改名
                    if not os.path.exists(new_path): # 防止覆盖已有文件
                        os.rename(file_path, new_path)
                        log.write(f"✅ 改名: {filename} -> {new_filename}\n")
                    else:
                        print(f"⚠️ 目标文件名已存在，跳过重命名: {new_filename}")
                else:
                    # 名字没变，但也记录一下标签已更新
                    log.write(f"✅ 标签更新(名未变): {filename}\n")
                
                processed += 1
                if processed % 50 == 0:
                    print(f"⏳ 进度: {processed}/{total} ...")
                    
            except Exception as e:
                err_msg = f"❌ 失败: {filename} -> {e}"
                print(err_msg)
                log.write(err_msg + "\n")

    print(f"\n🎉 大功告成！共处理 {processed} 个文件。")
    print(f"📁 详细日志已保存至 {REPORT_FILE}")
    print("📱 请使用 ADB 将 Music_Done 文件夹覆盖至手机！")

if __name__ == "__main__":
    main()