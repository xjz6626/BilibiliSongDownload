import os
import random
from mutagen.mp4 import MP4

TARGET_DIR = "./Music_Done"

def check_status():
    if not os.path.exists(TARGET_DIR):
        print("❌ 找不到文件夹")
        return

    files = [f for f in os.listdir(TARGET_DIR) if f.endswith(".m4a")]
    if not files:
        print("❌ 文件夹是空的")
        return

    # 随机抽取 5 首歌进行体检
    sample_files = random.sample(files, min(5, len(files)))

    print(f"🕵️‍♀️ 随机抽取 {len(sample_files)} 首歌进行底层数据检查...\n")
    print(f"{'文件名 (截取)':<30} | {'数据类型':<10} | {'实际内容 (Python List)'}")
    print("-" * 100)

    for filename in sample_files:
        path = os.path.join(TARGET_DIR, filename)
        try:
            audio = MP4(path)
            
            # 读取歌手标签 (\xa9ART)
            artists = audio.tags.get("\xa9ART")
            album = audio.tags.get("\xa9alb")
            
            # 检查数据类型
            is_list = isinstance(artists, list)
            type_str = "✅ 列表" if is_list else "❌ 字符串"
            
            # 截取文件名方便显示
            display_name = (filename[:28] + '..') if len(filename) > 28 else filename

            print(f"{display_name:<30} | {type_str:<10} | {artists}")
            
            if album != ["b站收藏"] and album != "b站收藏":
                 print(f"   ⚠️ 警告: 专辑名不对 -> {album}")

        except Exception as e:
            print(f"❌ 读取失败: {filename}")

    print("-" * 100)
    print("💡 判定标准：")
    print("1. [数据类型] 必须显示 '✅ 列表'。")
    print("2. [实际内容] 必须是方括号包裹的，如 ['歌手A', '歌手B']。")

if __name__ == "__main__":
    check_status()