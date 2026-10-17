

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List


def clean_comments(
    input_path: str,
    output_path: str,
    min_likes: int = 10,
) -> Dict[str, Any]:
    """
    健壮的B站评论数据清洗工具。
    - 正确读取 JSON 格式的 B 站评论数据文件
    - 删除点赞数 (like 字段) 小于 min_likes 的评论
    - 保留原始数据结构 (url / title / bv / total_collected / top_comments)
    - 清洗后更新 total_collected 为实际保留数量
    - 输出清洗统计信息 (原始数 / 保留数 / 删除数)
    - 处理边界情况：空评论列表、缺失 like 字段、文件不存在、非法类型等
    - 结果写入 file_database/current/
    """
    input_file = Path(input_path)
    if not input_file.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    if not input_file.is_file():
        raise ValueError(f"路径不是有效文件: {input_path}")

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON解析失败: {e}") from e
    except Exception as e:
        raise RuntimeError(f"读取文件失败: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("根节点必须是JSON对象")

    data.setdefault("url", "")
    data.setdefault("title", "")
    data.setdefault("bv", "")
    data.setdefault("total_collected", 0)
    data.setdefault("top_comments", [])

    original = data.get("top_comments")
    if not isinstance(original, list):
        raise ValueError("top_comments 必须是列表")

    cleaned: List[Dict[str, Any]] = []
    removed: List[Any] = []

    for comment in original:
        if not isinstance(comment, dict):
            removed.append({"invalid": True, "raw": comment})
            continue

        like_val = comment.get("like", 0)
        try:
            like_count = int(like_val)
        except (TypeError, ValueError):
            like_count = 0

        if like_count >= min_likes:
            cleaned.append(comment)
        else:
            removed.append(comment)

    data["top_comments"] = cleaned
    data["total_collected"] = len(cleaned)

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    db_dir = Path("file_database/current")
    db_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "original_count": len(original),
        "cleaned_count": len(cleaned),
        "removed_count": len(removed),
        "removed_likes_distribution": [
            (c.get("like", 0) if isinstance(c, dict) else None) for c in removed
        ],
    }

    (db_dir / "clean_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (db_dir / "removed_comments.json").write_text(
        json.dumps(removed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    shutil.copy(str(output_file), db_dir / "cleaned_comments.json")

    print(f"原始评论数: {stats['original_count']}")
    print(f"保留评论数: {stats['cleaned_count']}")
    print(f"删除评论数: {stats['removed_count']}")
    print(f"保留 {len(cleaned)} 条，删除 {len(removed)} 条")
    return stats


if __name__ == "__main__":
    try:
        clean_comments(
            "comments_BV1GXJs6kEt3.json",
            "cleaned_comments.json",
            min_likes=10,
        )
    except Exception as e:
        print(f"清洗失败: {e}")
        raise
# END_AND_END
