import os
from mutagen.mp4 import MP4

MUSIC_DIR = "./Music_Done"

def check_covers():
    print(f"🕵️‍♀️ 开始检查 {MUSIC_DIR} 下的封面嵌入情况...")
    files = [f for f in os.listdir(MUSIC_DIR) if f.endswith('.m4a')]
    files.sort()
    
    success_count = 0
    fail_count = 0
    no_cover_files = []

    for filename in files:
        file_path = os.path.join(MUSIC_DIR, filename)
        try:
            audio = MP4(file_path)
            # MP4/M4A 格式中，封面存储在 'covr' 原子中
            if 'covr' in audio and len(audio['covr']) > 0:
                success_count += 1
                # print(f"✅ {filename}") # 想看刷屏可以取消注释
            else:
                fail_count += 1
                no_cover_files.append(filename)
                print(f"❌ [无封面] {filename}")
        except Exception as e:
            print(f"⚠️ [读取错误] {filename}: {e}")

    print("-" * 30)
    print("📊 检查报告：")
    print(f"✅ 成功嵌入封面: {success_count} 首")
    print(f"❌ 缺少封面: {fail_count} 首")
    
    if success_count + fail_count > 0:
        rate = (success_count / (success_count + fail_count)) * 100
        print(f"📉 覆盖率: {rate:.2f}%")
    
    if no_cover_files:
        print("-" * 30)
        print("👇 以下歌曲没有封面（可能是原视频本身就没有封面）：")
        for f in no_cover_files[:10]: # 只列出前10个
            print(f"  - {f}")
        if len(no_cover_files) > 10:
            print(f"  ... 以及其他 {len(no_cover_files)-10} 首")

if __name__ == "__main__":
    check_covers()