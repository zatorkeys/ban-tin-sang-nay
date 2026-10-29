"""Chạy toàn bộ bản tin trong ngày: crawl -> lưu dữ liệu thô -> AI tóm tắt -> digests/YYYY-MM-DD.json.

    python run_daily.py                      # chạy thật (cần GEMINI_API_KEY)
    python run_daily.py --from-data          # bỏ qua crawl, dùng lại data/articles/<ngày>.jsonl.gz
    python run_daily.py --now "2026-10-29 07:00" --from-data

Dữ liệu thô (bài + nội dung + bình luận) lưu ở data/articles/YYYY-MM-DD.jsonl.gz,
mỗi dòng một bài, bình luận nằm trong trường "comments". Đây là kho để làm RAG về sau.
"""
import argparse
import gzip
import json
import os
import sys
from datetime import datetime

import pandas as pd

import ai_summary
import crawler

DATA_DIR = os.path.join("data", "articles")


def save_raw(articles_df, comment_df, path):
    groups = {aid: g.drop(columns="ArticleID").to_dict("records")
              for aid, g in comment_df.groupby("ArticleID")}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for rec in articles_df.astype(object).where(articles_df.notna(), None).to_dict("records"):
            rec["comments"] = groups.get(rec["ArticleID"], [])
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Đã lưu dữ liệu thô: {path}")


def load_raw(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    comments = [{"ArticleID": r["ArticleID"], **c} for r in records for c in r.pop("comments")]
    return pd.DataFrame(records), pd.DataFrame(comments, columns=["ArticleID", "Content", "Likes"])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--now", help="mốc thời gian 'YYYY-MM-DD HH:MM' giờ VN (mặc định: bây giờ)")
    p.add_argument("--from-data", action="store_true", help="dùng dữ liệu đã crawl, không crawl lại")
    p.add_argument("--top", type=int, default=ai_summary.TOP_N)
    args = p.parse_args()

    now = (datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now
           else datetime.now(crawler.VN_TZ).replace(tzinfo=None, second=0, microsecond=0))
    raw_path = os.path.join(DATA_DIR, f"{now:%Y-%m-%d}.jsonl.gz")

    if args.from_data:
        articles_df, comment_df = load_raw(raw_path)
    else:
        articles_df, comment_df = crawler.crawl_last_24h(now=now)
        if articles_df.empty:
            sys.exit("Không crawl được bài nào (VnExpress chặn hoặc đổi giao diện?)")
        save_raw(articles_df, comment_df, raw_path)

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Thiếu biến môi trường GEMINI_API_KEY")
    digest = ai_summary.build_digest(articles_df, comment_df, now=now, top_n=args.top)
    if digest is None:
        sys.exit("Không có bài nào đủ điều kiện để làm bản tin")


if __name__ == "__main__":
    main()
