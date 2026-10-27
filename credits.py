"""Conservative credit extraction for Bilibili music metadata.

The parser intentionally only accepts explicit ``role: value`` declarations.  It
is designed for migration work where a missed credit can be reviewed later, but
an incorrectly attributed creator would be written to many files.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = 1

# The public result keys are deliberately independent from the labels used by a
# particular uploader.  Keep these stable: callers persist them in migration
# reports and MP4 freeform atoms.
MULTI_VALUE_FIELDS = (
    "lyricists",
    "composers",
    "arrangers",
    "tuners",
    "mixers",
    "mastering_engineers",
    "producers",
    "illustrators",
    "video_creators",
)

MP4_FREEFORM_TAGS = {
    "uploader": "----:com.apple.iTunes:BilibiliUploader",
    "uploader_id": "----:com.apple.iTunes:BilibiliUploaderID",
    "lyricists": "----:com.apple.iTunes:BilibiliLyricist",
    "composers": "----:com.apple.iTunes:BilibiliComposer",
    "arrangers": "----:com.apple.iTunes:BilibiliArranger",
    "tuners": "----:com.apple.iTunes:BilibiliTuner",
    "mixers": "----:com.apple.iTunes:BilibiliMixer",
    "mastering_engineers": "----:com.apple.iTunes:BilibiliMastering",
    "producers": "----:com.apple.iTunes:PRODUCER",
    "illustrators": "----:com.apple.iTunes:BilibiliIllustrator",
    "video_creators": "----:com.apple.iTunes:BilibiliVideoCreator",
}

_ROLE_ALIASES = {
    "uploader": (
        "UP主",
        "发布者",
        "发布人",
        "投稿者",
        "uploader",
        "channel",
    ),
    "lyricists": (
        "作词",
        "作詞",
        "词作",
        "詞作",
        "填词",
        "填詞",
        "lyricist",
        "lyrics",
        "lyric",
        "词",
        "詞",
    ),
    "composers": (
        "作曲",
        "曲作",
        "composer",
        "composition",
        "music",
        "曲",
    ),
    "arrangers": (
        "编曲",
        "編曲",
        "arranger",
        "arrangement",
        "编",
        "編",
    ),
    "tuners": (
        "调教",
        "調教",
        "调音",
        "調音",
        "调校",
        "調校",
        "调声",
        "調聲",
        "tuning",
        "tuner",
        "tune",
    ),
    "mixers": (
        "混音师",
        "混音師",
        "混音",
        "后期",
        "後期",
        "mixing",
        "mixer",
        "mix",
    ),
    "mastering_engineers": (
        "母带工程师",
        "母帶工程師",
        "母带处理",
        "母帶處理",
        "母带",
        "母帶",
        "mastering",
        "master",
    ),
    "producers": (
        "总制作人",
        "總製作人",
        "制作人",
        "製作人",
        "制片人",
        "製片人",
        "总策划",
        "總策劃",
        "策划",
        "策劃",
        "监制",
        "監製",
        "produced by",
        "producer",
        "production",
        "制作",
        "製作",
    ),
    "illustrators": (
        "曲绘",
        "曲繪",
        "画师",
        "畫師",
        "绘师",
        "繪師",
        "插画",
        "插畫",
        "illustration",
        "illustrator",
        "illust",
        "artwork",
        "art",
    ),
    "video_creators": (
        "视频制作",
        "視頻製作",
        "视频",
        "視頻",
        "影像",
        "映像",
        "动画",
        "動畫",
        "剪辑",
        "剪輯",
        "animation",
        "video",
        "movie",
        "PV",
        "MV",
    ),
}

_COMBINED_LABELS = {
    "词曲": ("lyricists", "composers"),
    "詞曲": ("lyricists", "composers"),
    "作词作曲": ("lyricists", "composers"),
    "作詞作曲": ("lyricists", "composers"),
    "曲编": ("composers", "arrangers"),
    "曲編": ("composers", "arrangers"),
    "作曲编曲": ("composers", "arrangers"),
    "作曲編曲": ("composers", "arrangers"),
}

_ALIAS_TO_ROLE = {
    alias.casefold(): role
    for role, aliases in _ROLE_ALIASES.items()
    for alias in aliases
}
_LABELS = set(_ALIAS_TO_ROLE) | set(_COMBINED_LABELS)
# Combined labels such as ``词/曲`` are handled by allowing separators between
# two or more known labels.  A colon/equal sign is mandatory, avoiding song-title
# prose such as "作词作曲挑战".
_LABEL_ATOM = "|".join(
    re.escape(label) for label in sorted(_LABELS, key=len, reverse=True)
)
_ROLE_DECLARATION_RE = re.compile(
    rf"(?P<label>(?:{_LABEL_ATOM})(?:\s*[/&+、·・\-]\s*(?:{_LABEL_ATOM}))*)"
    rf"\s*(?:[:=]|\s+[/／]\s+)",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"【([^【】]{1,500})】|\[([^\[\]]{1,500})\]|\(([^()]{1,500})\)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_TRAILING_CONTACT_RE = re.compile(
    r"\s*[（(](?:微博|B站|bilibili|Twitter|X|UID|uid|@)[^()（）]{0,80}[)）]\s*$",
    re.IGNORECASE,
)
_LEADING_DECORATION_RE = re.compile(r"^[\s@＠*#>·•|｜:：=\-—–]+")
_TRAILING_DECORATION_RE = re.compile(r"[\s*#>·•|｜:：=\-—–]+$")

_REFERENCE_LINE_RE = re.compile(
    r"^\s*(?:[-*#>·•]+\s*)?(?:【|\[|\()?\s*"
    r"(?:本家(?:様)?|原曲|原唱|原作|原版|原视频|原視頻|"
    r"original(?:\s+(?:song|version|video|staff|credits?))?|source)\b",
    re.IGNORECASE,
)
_REFERENCE_ANYWHERE_RE = re.compile(
    r"(?:本家|原曲|原唱|原作|原版|旧版|舊版|初版|上一版|前版|其他版本|"
    r"另一版|original\s+(?:version|staff|credits?))",
    re.IGNORECASE,
)
_REFERENCE_SECTION_RE = re.compile(
    r"(?:本家|原曲|原唱|原作|原版|旧版|舊版|初版|上一版|前版|其他版本|另一版)"
    r".{0,12}(?:staff|credits?|制作|製作|人员|人員|信息|資料)",
    re.IGNORECASE,
)
_REFERENCE_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:【|\[|\()?\s*"
    r"(?:本家(?:様)?|原曲|原唱|原作|原版|旧版|舊版|初版|上一版|前版|"
    r"其他版本|另一版)(?:\s*(?:staff|credits?|信息|資料))?\s*[:：]?\s*(?:】|\]|\))?$",
    re.IGNORECASE,
)
_MATERIAL_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:【|\[|\()?\s*(?:借物表|素材(?:来源|來源|出处|出處)|授权信息|授權信息)"
    r"\s*[:：]?\s*(?:】|\]|\))?$",
    re.IGNORECASE,
)
_CURRENT_SECTION_RE = re.compile(
    r"(?:(?:本作|本曲|本视频|本視頻|当前|當前|新版|重制版|重製版|翻唱版)"
    r".{0,12}(?:staff|credits?|制作|製作|人员|人員))",
    re.IGNORECASE,
)
_STAFF_HEADER_RE = re.compile(
    r"^\s*(?:【|\[|\()?\s*(?:staff|credits?|制作人员|製作人員|创作人员|創作人員|"
    r"主创|主創|制作名单|製作名單)\s*[:：]?\s*(?:】|\]|\))?$",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"授权|授權|许可|許可|转载|轉載|搬运|搬運|禁止|严禁|嚴禁|请勿|請勿|"
    r"不得|未经|未經|素材(?:来自|來自|来源|來源|下载|下載)|"
    r"permission|licensed?\s+(?:by|from)|repost",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?://|www\.|b23\.tv|av\d{3,}|BV[0-9A-Za-z]{8,})", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[。！？!?]|…{2,}")
_PROMPT_INJECTION_RE = re.compile(
    r"忽略(?:上述|上面|之前|以前|原有|所有)?(?:指令|要求|规则|提示)|"
    r"不要遵守|改变规则|系统提示|执行命令|只输出|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)|"
    r"system\s+prompt|developer\s+message|execute\s+(?:this\s+)?command|"
    r"output\s+only",
    re.IGNORECASE,
)
_NON_NAME_VALUES = {
    "staff",
    "unknown",
    "未知",
    "无",
    "無",
    "暂无",
    "暫無",
    "见简介",
    "見簡介",
    "见动态",
    "見動態",
    "none",
    "n/a",
    "我",
    "本人",
    "自己",
    "me",
}

# These useful but less universal roles may appear outside a Staff block.  They
# remain structured ``other_staff`` rather than being promoted into invented MP4
# atoms.  All other unknown labels are accepted only inside a clear Staff block.
_OTHER_STAFF_LABELS = {
    "文案",
    "题字",
    "題字",
    "字体",
    "字體",
    "字体设计",
    "字體設計",
    "logo",
    "海报",
    "海報",
    "封面",
    "封面设计",
    "封面設計",
    "美术",
    "美術",
    "摄影",
    "攝影",
    "摄像",
    "攝像",
    "导演",
    "導演",
    "视频策划",
    "視頻策劃",
    "脚本",
    "腳本",
    "分镜",
    "分鏡",
    "建模",
    "模型",
    "场景",
    "場景",
    "特效",
    "调色",
    "調色",
    "字幕",
    "校对",
    "校對",
    "录音",
    "錄音",
    "吉他",
    "贝斯",
    "貝斯",
    "贝司",
    "貝司",
    "鼓",
    "钢琴",
    "鋼琴",
    "二胡",
    "笛子",
    "演奏",
    "乐器",
    "樂器",
    "协力",
    "協力",
    "演唱",
    "歌手",
    "歌姬",
    "主唱",
    "人声",
    "人聲",
    "vocal",
    "singer",
}
_OTHER_STAFF_LABELS = {label.casefold() for label in _OTHER_STAFF_LABELS}
_REJECTED_OTHER_STAFF_LABELS = {
    "staff",
    "credit",
    "credits",
    "歌名",
    "歌曲",
    "作品",
    "标题",
    "標題",
    "title",
    "简介",
    "簡介",
    "链接",
    "連結",
    "link",
    "地址",
    "网址",
    "網址",
    "日期",
    "时间",
    "時間",
    "投稿时间",
    "投稿時間",
    "发布时间",
    "發布時間",
    "发行日期",
    "發行日期",
    "bvid",
    "avid",
    "uid",
    "微博",
    "qq",
    "微信",
    "下载",
    "下載",
    "鸣谢",
    "鳴謝",
    "特别感谢",
    "特別感謝",
    "参与",
    "參與",
    "参加",
    "參加",
    "参演",
    "參演",
    "活动阵容",
    "活動陣容",
}
_OTHER_STAFF_DECLARATION_RE = re.compile(
    r"^\s*(?:[-*#>·•❀]+\s*)?"
    r"(?P<label>[0-9A-Za-z\u3400-\u9fff][0-9A-Za-z\u3400-\u9fff _./&+、·・\-]{0,23}?)"
    r"\s*(?:[:=]|\s+[/／]\s+)\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_NON_PERSON_VALUE_RE = re.compile(
    r"^(?:\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|\d{1,2}:\d{2}(?::\d{2})?|\d+(?:\.\d+)?)$"
)


def _normalize_source_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    # Preserve newlines because section boundaries are meaningful.
    value = "\n".join(_CONTROL_RE.sub(" ", line).rstrip() for line in value.splitlines())
    return value.strip()


def normalize_credit_name(value: Any) -> str:
    """Normalize one creator name without inventing or transliterating it."""

    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL_RE.sub(" ", value)
    value = _SPACE_RE.sub(" ", value).strip()
    value = re.sub(r"^(?:by|from)\s+", "", value, flags=re.IGNORECASE)
    value = _LEADING_DECORATION_RE.sub("", value)
    value = _TRAILING_DECORATION_RE.sub("", value)
    value = _TRAILING_CONTACT_RE.sub("", value).strip()
    return value


def _normalize_metadata_uploader(value: Any) -> str:
    """Keep the authoritative account spelling, including edge punctuation."""

    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def _valid_credit_name(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    if value.casefold() in _NON_NAME_VALUES:
        return False
    if _URL_RE.search(value) or _SENTENCE_RE.search(value) or _PROMPT_INJECTION_RE.search(value):
        return False
    if _ROLE_DECLARATION_RE.search(value):
        return False
    # At least one letter, ideograph, kana, or digit must remain after trimming.
    return bool(re.search(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff]", value))


def _valid_other_staff_name(value: str) -> bool:
    # Unknown roles get a stricter value policy than known role declarations.
    # In particular, dates, counters, and another embedded declaration are not
    # accepted as people or credited groups.
    return bool(
        _valid_credit_name(value)
        and not _NON_PERSON_VALUE_RE.fullmatch(value)
        and ":" not in value
        and "=" not in value
        and value.casefold() not in {"大家", "所有人", "各位", "粉丝", "粉絲", "观众", "觀眾"}
    )


_OPEN_TO_CLOSE = {
    "(": ")",
    "（": "）",
    "[": "]",
    "【": "】",
    "<": ">",
    "《": "》",
    "「": "」",
    "『": "』",
}
_CLOSING_BRACKETS = set(_OPEN_TO_CLOSE.values())
_NAME_SEPARATORS = set("、,，/&＆+＋|;；")


def _strip_balanced_outer_wrapper(value: str) -> str:
    """Remove only a wrapper that encloses the complete value.

    ``str.strip('()')`` corrupts legitimate affiliations such as
    ``李懋扬 (T2o)``.  This helper retains such suffixes while still accepting
    values written as ``(甲 / 乙)``.
    """

    value = value.strip()
    while value and value[0] in _OPEN_TO_CLOSE:
        expected = _OPEN_TO_CLOSE[value[0]]
        stack: list[str] = []
        outer_end = None
        for index, character in enumerate(value):
            if character in _OPEN_TO_CLOSE:
                stack.append(_OPEN_TO_CLOSE[character])
            elif stack and character == stack[-1]:
                stack.pop()
                if not stack:
                    outer_end = index
                    break
        if outer_end != len(value) - 1 or value[-1] != expected:
            break
        value = value[1:-1].strip()
    return value


def _split_value_parts(raw_value: str) -> list[str]:
    """Split creator lists without treating delimiters in scopes as people."""

    parts: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    index = 0

    def flush() -> None:
        value = "".join(current).strip()
        current.clear()
        if value:
            parts.append(value)

    while index < len(raw_value):
        character = raw_value[index]
        if character in _OPEN_TO_CLOSE:
            stack.append(_OPEN_TO_CLOSE[character])
            current.append(character)
            index += 1
            continue
        if stack and character == stack[-1]:
            stack.pop()
            current.append(character)
            index += 1
            continue
        if not stack and character in _NAME_SEPARATORS:
            flush()
            index += 1
            while index < len(raw_value) and raw_value[index].isspace():
                index += 1
            continue
        if not stack and character.isspace():
            run_start = index
            while index < len(raw_value) and raw_value[index].isspace():
                index += 1
            run = raw_value[run_start:index]
            previous = next((item for item in reversed(current) if not item.isspace()), "")
            following = raw_value[index] if index < len(raw_value) else ""
            if following in _OPEN_TO_CLOSE:
                # Uploader lists often put vocal scope after two spaces:
                # ``@鬼面P  (天依/摩柯)@哈士奇P``.  The scope belongs to the
                # preceding creator and is not another credited person.
                if current:
                    current.append(" ")
            elif len(run) >= 2 or following in "@＠" or previous in _CLOSING_BRACKETS:
                flush()
            elif current and following:
                current.append(" ")
            continue
        if not stack and character in "@＠" and current:
            previous = next((item for item in reversed(current) if not item.isspace()), "")
            if previous in _CLOSING_BRACKETS:
                flush()
            current.append(character)
            index += 1
            continue
        current.append(character)
        index += 1
    flush()

    # A few Bilibili descriptions use one ordinary space between handles, for
    # example ``boorain Star白鲤``.  Restrict this heuristic to an ASCII handle
    # followed by a mixed Latin+CJK handle so normal names such as ``Mixer E``
    # and affiliation suffixes such as ``李懋扬 (T2o)`` stay intact.
    expanded: list[str] = []
    mixed_handle = re.compile(
        r"^([0-9A-Za-z_.\-]{2,40})\s+"
        r"((?=[0-9A-Za-z_.\-\u3400-\u9fff]{2,50}$)"
        r"(?=[^\s]*[A-Za-z])(?=[^\s]*[\u3400-\u9fff])[^\s]+)$"
    )
    for part in parts:
        match = mixed_handle.fullmatch(part)
        if match:
            expanded.extend(match.groups())
            continue
        cjk_tokens = part.split()
        if (
            len(cjk_tokens) >= 3
            and all(re.search(r"[\u3400-\u9fff]", token) for token in cjk_tokens)
            and not any(character in part for character in _OPEN_TO_CLOSE)
        ):
            expanded.extend(cjk_tokens)
            continue
        expanded.append(part)
    return expanded


def _split_names(raw_value: str) -> list[str]:
    raw_value = _strip_balanced_outer_wrapper(raw_value)
    # Check before splitting: URL slashes must never turn a link into plausible
    # looking creator fragments such as ``https``, ``example.com`` and ``name``.
    if _URL_RE.search(raw_value):
        return []
    names = []
    for part in _split_value_parts(raw_value):
        name = normalize_credit_name(part)
        if _valid_credit_name(name):
            names.append(name)
    return names


def _roles_for_label(raw_label: str) -> list[str]:
    normalized = re.sub(r"\s+", "", raw_label).casefold()
    if normalized in _COMBINED_LABELS:
        return list(_COMBINED_LABELS[normalized])

    roles = []
    for part in re.split(r"[/&+、·・\-]", normalized):
        if part in _COMBINED_LABELS:
            candidates = _COMBINED_LABELS[part]
        else:
            role = _ALIAS_TO_ROLE.get(part)
            candidates = (role,) if role else ()
        for role in candidates:
            if role and role not in roles:
                roles.append(role)
    return roles


def _unknown_label_parts(raw_label: str) -> list[str]:
    """Return role components that are not represented by canonical fields."""

    unknown = []
    for part in re.split(r"[/&+、·・\-]", raw_label):
        part = _SPACE_RE.sub(" ", part).strip()
        if not part:
            continue
        normalized = part.casefold()
        if normalized in _COMBINED_LABELS or normalized in _ALIAS_TO_ROLE:
            continue
        unknown.append(part)
    return unknown


def _dedupe_key(value: str) -> str:
    return re.sub(r"[\s@＠._·・\-—–]+", "", value).casefold()


def _contains_unsafe_reference(line: str) -> bool:
    if _REFERENCE_LINE_RE.search(line):
        return True
    # Do not reject an unrelated title merely for saying "原曲".  The broader
    # check is only relevant when that same unit contains an explicit declaration.
    return bool(_ROLE_DECLARATION_RE.search(line) and _REFERENCE_ANYWHERE_RE.search(line))


def _declaration_units(line: str) -> list[str]:
    """Keep role declarations inside title brackets from consuming song text."""

    bracket_units = []
    spans = []
    for match in _BRACKET_RE.finditer(line):
        content = next(group for group in match.groups() if group is not None)
        if _ROLE_DECLARATION_RE.search(content):
            bracket_units.append(content)
            spans.append(match.span())
    if not spans:
        return [line]

    remainder_parts = []
    cursor = 0
    for start, end in spans:
        remainder_parts.append(line[cursor:start])
        cursor = end
    remainder_parts.append(line[cursor:])
    remainder = " ".join(remainder_parts)
    if _ROLE_DECLARATION_RE.search(remainder):
        bracket_units.append(remainder)
    return bracket_units


def _other_staff_declaration_units(line: str) -> list[str]:
    """Return complete lines plus bracketed extension-role declarations."""

    units = [line]
    for match in _BRACKET_RE.finditer(line):
        content = next(group for group in match.groups() if group is not None)
        declaration = _OTHER_STAFF_DECLARATION_RE.fullmatch(content)
        if not declaration:
            continue
        label = _SPACE_RE.sub(" ", declaration.group("label")).strip().casefold()
        if label in _OTHER_STAFF_LABELS:
            units.insert(0, content)
    return units


def _add_credit(
    result: dict[str, Any],
    seen: dict[str, dict[str, int]],
    field: str,
    name: str,
    *,
    source: str,
    label: str,
    confidence: float,
) -> None:
    key = _dedupe_key(name)
    if not key:
        return

    details = result["credit_details"][field]
    existing_index = seen[field].get(key)
    evidence = {"source": source, "label": label}
    if existing_index is not None:
        detail = details[existing_index]
        if evidence not in detail["evidence"]:
            detail["evidence"].append(evidence)
        detail["confidence"] = max(detail["confidence"], confidence)
        return

    if field == "uploader":
        if result["uploader"]:
            # The metadata uploader is authoritative; retain no alternate value
            # that might actually be a producer mentioned in prose.
            return
        result["uploader"] = name
    else:
        result[field].append(name)
    seen[field][key] = len(details)
    details.append(
        {
            "name": name,
            "confidence": confidence,
            "evidence": [evidence],
        }
    )


def _add_other_staff(
    result: dict[str, Any],
    seen: dict[str, int],
    role: str,
    names: list[str],
    *,
    source: str,
    confidence: float,
) -> None:
    role = _SPACE_RE.sub(" ", normalize_credit_name(role)).strip()
    role_key = _dedupe_key(role)
    if not role_key or not names:
        return

    evidence = {"source": source, "label": role, "names": list(names)}
    existing_index = seen.get(role_key)
    if existing_index is None:
        seen[role_key] = len(result["other_staff"])
        result["other_staff"].append(
            {
                "role": role,
                "names": list(names),
                "confidence": confidence,
                "evidence": [evidence],
            }
        )
        return

    entry = result["other_staff"][existing_index]
    existing_names = {_dedupe_key(name) for name in entry["names"]}
    for name in names:
        if _dedupe_key(name) not in existing_names:
            entry["names"].append(name)
            existing_names.add(_dedupe_key(name))
    if evidence not in entry["evidence"]:
        entry["evidence"].append(evidence)
    entry["confidence"] = max(entry["confidence"], confidence)


def _record_ignored(result: dict[str, Any], source: str, reason: str, text: str) -> None:
    text = _SPACE_RE.sub(" ", text).strip()
    if text:
        result["ignored_contexts"].append(
            {"source": source, "reason": reason, "text": text[:240]}
        )


def _extract_from_text(
    result: dict[str, Any],
    seen: dict[str, dict[str, int]],
    text: str,
    *,
    source_name: str,
    base_confidence: float,
) -> None:
    unsafe_section = False
    in_staff_section = False
    other_staff_seen = {
        _dedupe_key(entry["role"]): index
        for index, entry in enumerate(result["other_staff"])
    }
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            unsafe_section = False
            in_staff_section = False
            continue

        source = f"{source_name}:line:{line_number}"
        if _REFERENCE_SECTION_RE.search(stripped) or _REFERENCE_SECTION_HEADER_RE.search(stripped):
            unsafe_section = True
            in_staff_section = False
            _record_ignored(result, source, "referenced-or-other-version staff section", stripped)
            continue
        if _MATERIAL_SECTION_HEADER_RE.search(stripped):
            unsafe_section = True
            in_staff_section = False
            _record_ignored(result, source, "authorization/material section", stripped)
            continue
        if _CURRENT_SECTION_RE.search(stripped):
            unsafe_section = False
            in_staff_section = True
            # A header can also contain declarations after "Staff".
        if unsafe_section:
            if _ROLE_DECLARATION_RE.search(stripped):
                _record_ignored(result, source, "inside referenced-or-other-version section", stripped)
            continue
        if _STAFF_HEADER_RE.fullmatch(stripped):
            in_staff_section = True
            continue
        if _AUTHORIZATION_RE.search(stripped):
            if _ROLE_DECLARATION_RE.search(stripped):
                _record_ignored(result, source, "authorization/material attribution", stripped)
            continue
        if _contains_unsafe_reference(stripped):
            _record_ignored(result, source, "referenced original or other version", stripped)
            continue

        for unit in _declaration_units(stripped):
            matches = list(_ROLE_DECLARATION_RE.finditer(unit))
            for index, match in enumerate(matches):
                value_end = matches[index + 1].start() if index + 1 < len(matches) else len(unit)
                raw_value = unit[match.end() : value_end]
                names = _split_names(raw_value)
                if not names:
                    continue
                label = match.group("label")
                roles = _roles_for_label(label)
                # Short generic labels (词/曲/编/PV/MV/Art/Music) remain explicit
                # but receive a slightly lower confidence score for audit reports.
                short_label = re.sub(r"\s+", "", label).casefold()
                confidence = base_confidence
                if short_label in {"词", "詞", "曲", "编", "編", "pv", "mv", "art", "music"}:
                    confidence = min(confidence, 0.88)
                for role in roles:
                    for name in names:
                        _add_credit(
                            result,
                            seen,
                            role,
                            name,
                            source=source,
                            label=label,
                            confidence=confidence,
                        )

        # Unknown roles are deliberately parsed only as a whole declaration,
        # avoiding accidental matches in normal prose and song-title prefixes.
        for unit in _other_staff_declaration_units(stripped):
            declaration = _OTHER_STAFF_DECLARATION_RE.fullmatch(unit)
            if not declaration:
                continue
            raw_label = declaration.group("label")
            normalized_label = _SPACE_RE.sub(" ", raw_label).strip().casefold()
            roles = _roles_for_label(raw_label)
            unknown_labels = _unknown_label_parts(raw_label)
            if normalized_label in _REJECTED_OTHER_STAFF_LABELS:
                continue
            if roles and not unknown_labels:
                # Canonical declarations were handled above.
                continue
            whitelisted = normalized_label in _OTHER_STAFF_LABELS
            accepted_unknown_labels = [
                label
                for label in unknown_labels
                if label.casefold() not in _REJECTED_OTHER_STAFF_LABELS
                and (in_staff_section or label.casefold() in _OTHER_STAFF_LABELS)
            ]
            if not roles and not accepted_unknown_labels and not whitelisted:
                continue
            raw_value = declaration.group("value")
            if _ROLE_DECLARATION_RE.search(raw_value):
                continue
            names = [name for name in _split_names(raw_value) if _valid_other_staff_name(name)]
            if not names:
                continue
            # A declaration such as ``混音、封面: 甲`` carries both a canonical
            # credit and an uploader-specific role.  Persist both instead of
            # losing whichever half the generic role regex cannot represent.
            for role in roles:
                for name in names:
                    _add_credit(
                        result,
                        seen,
                        role,
                        name,
                        source=source,
                        label=raw_label,
                        confidence=base_confidence,
                    )
            confidence = min(base_confidence, 0.90 if in_staff_section else 0.86)
            labels_to_preserve = accepted_unknown_labels or (
                [raw_label] if not roles and whitelisted else []
            )
            for label in labels_to_preserve:
                _add_other_staff(
                    result,
                    other_staff_seen,
                    label,
                    names,
                    source=source,
                    confidence=confidence,
                )


def extract_credits(
    title: str,
    description: str,
    *,
    uploader: str = "",
    uploader_id: str = "",
) -> dict[str, Any]:
    """Extract explicitly labelled creator credits from Bilibili metadata.

    The stable, easy-to-persist keys are ``uploader``, ``uploader_id`` and the
    list fields in :data:`MULTI_VALUE_FIELDS`.  ``credit_details`` adds audit
    evidence without changing those simple values.  ``other_staff`` retains
    explicit but non-canonical roles and is intended for the authoritative JSON
    record rather than arbitrary MP4 atoms.  ``ignored_contexts`` reports
    potentially misleading source/reference blocks that were deliberately not
    interpreted.

    ``uploader`` and ``uploader_id`` should normally come from yt-dlp/Bilibili
    metadata.  A labelled ``UP主:`` declaration is used only if ``uploader`` is
    absent.
    """

    title = _normalize_source_text(title)
    description = _normalize_source_text(description)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "uploader": "",
        "uploader_id": "",
        **{field: [] for field in MULTI_VALUE_FIELDS},
        "other_staff": [],
        "credit_details": {
            "uploader": [],
            **{field: [] for field in MULTI_VALUE_FIELDS},
        },
        "ignored_contexts": [],
    }
    seen = {"uploader": {}, **{field: {} for field in MULTI_VALUE_FIELDS}}

    metadata_uploader = _normalize_metadata_uploader(uploader)
    if _valid_credit_name(metadata_uploader):
        _add_credit(
            result,
            seen,
            "uploader",
            metadata_uploader,
            source="metadata:uploader",
            label="uploader",
            confidence=1.0,
        )
    if isinstance(uploader_id, (str, int)) and not isinstance(uploader_id, bool):
        normalized_id = _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(uploader_id))).strip()
        if len(normalized_id) <= 200 and not _CONTROL_RE.search(normalized_id):
            result["uploader_id"] = normalized_id

    _extract_from_text(
        result,
        seen,
        title,
        source_name="title",
        base_confidence=0.92,
    )
    _extract_from_text(
        result,
        seen,
        description,
        source_name="description",
        base_confidence=0.95,
    )
    return result


def build_mp4_credit_tags(
    credits: Mapping[str, Any], *, encode: bool = True
) -> dict[str, list[bytes] | list[str]]:
    """Convert :func:`extract_credits` output to Mutagen MP4 freeform values.

    Mutagen expects a list of UTF-8 ``bytes`` for ``----:`` atoms, hence
    ``encode=True`` by default.  Passing ``encode=False`` is convenient for JSON
    previews and tests.  Empty fields are omitted.
    """

    tags: dict[str, list[bytes] | list[str]] = {}
    for field, tag_name in MP4_FREEFORM_TAGS.items():
        raw_value = credits.get(field, "" if field in {"uploader", "uploader_id"} else [])
        values = [raw_value] if isinstance(raw_value, str) else raw_value
        if not isinstance(values, (list, tuple)):
            continue
        normalized_values = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                continue
            value = value.strip()
            key = _dedupe_key(value)
            if not value or not key or key in seen:
                continue
            seen.add(key)
            normalized_values.append(value.encode("utf-8") if encode else value)
        if normalized_values:
            tags[tag_name] = normalized_values
    return tags


__all__ = [
    "MP4_FREEFORM_TAGS",
    "MULTI_VALUE_FIELDS",
    "SCHEMA_VERSION",
    "build_mp4_credit_tags",
    "extract_credits",
    "normalize_credit_name",
]
