"""AI tóm tắt bản tin trong ngày.

Luồng xử lý:
    1. pick_top()          chọn TOP_N bài trong 24 giờ qua có nhiều bình luận/giờ nhất
    2. summarize_articles() gọi AI tóm tắt nội dung tin + dư luận (gom BATCH_SIZE bài/lần gọi)
    3. summarize_day()      viết đoạn tổng quan "hôm nay có gì" từ các tóm tắt
    4. build_digest()       ghép tất cả thành file digests/YYYY-MM-DD.json cho trang web

Dữ liệu đầu vào:
    articles_df: ArticleID, Title, URL, PublishDate ('YYYY-MM-DD HH:MM', giờ VN), CommentCount
                 + (nên có) Body = nội dung bài, Description, Category
    comment_df : ArticleID, Content, Likes

API key: đặt biến môi trường GEMINI_API_KEY trước khi chạy.
    Colab : os.environ["GEMINI_API_KEY"] = userdata.get("GEMINI_API_KEY")
    GitHub Actions: lưu trong Secrets rồi truyền vào env.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from google import genai
from google.genai import errors
from pydantic import BaseModel

VN_TZ = timezone(timedelta(hours=7))

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
TOP_N = 15                 # số bài trong bản tin
BATCH_SIZE = 5             # số bài mỗi lần gọi AI (bài có nội dung dài nên để 5, không phải 10)
MAX_BODY_CHARS = 3000      # cắt nội dung bài: đoạn đầu bài báo thường chứa đủ ý chính
MAX_COMMENTS = 20          # số bình luận nhiều thích nhất đưa vào prompt
MAX_COMMENT_CHARS = 300
MIN_HOURS = 2              # bài mới đăng < 2 giờ vẫn chia cho 2, tránh điểm ảo (3 bình luận / 0.1 giờ)
RETRIES = 5
TAM_SU = "tam-su"          # thư độc giả: khung riêng cuối trang, không tính vào TOP_N
TAM_SU_N = 2


# ----------------------------------------------------------------------------
# 1. Chọn bài
# ----------------------------------------------------------------------------
def pick_top(articles_df, now=None, top_n=TOP_N, only=None, exclude=None):
    """Chọn top_n bài trong 24 giờ trước `now` theo số bình luận mỗi giờ.

    only / exclude: chỉ lấy / bỏ qua một chuyên mục (slug), dùng để tách Tâm sự.
    """
    now = now or datetime.now(VN_TZ).replace(tzinfo=None)
    df = articles_df.copy()
    df["ArticleID"] = df["ArticleID"].astype(str)
    df["PublishDate"] = pd.to_datetime(df["PublishDate"], errors="coerce")
    df["CommentCount"] = pd.to_numeric(df["CommentCount"], errors="coerce").fillna(0)

    df = df[(df["PublishDate"] >= now - timedelta(hours=24)) & (df["PublishDate"] <= now)]
    df = df[df["CommentCount"] > 0].drop_duplicates("ArticleID")
    if "Category" in df:
        if only:
            df = df[df["Category"] == only]
        if exclude:
            df = df[df["Category"] != exclude]

    hours = (now - df["PublishDate"]).dt.total_seconds() / 3600
    df["CommentsPerHour"] = df["CommentCount"] / hours.clip(lower=MIN_HOURS)
    return df.sort_values("CommentsPerHour", ascending=False).head(top_n)


# ----------------------------------------------------------------------------
# 2. Gọi AI
# ----------------------------------------------------------------------------
_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    return _client


def _generate(prompt, system, max_tokens, schema=None):
    """Gọi Gemini, tự thử lại khi server quá tải (5xx) hoặc vượt giới hạn (429).

    Trả về chuỗi text của model, hoặc None nếu thử RETRIES lần vẫn lỗi.
    """
    config = {"system_instruction": system, "max_output_tokens": max_tokens}
    if schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_schema"] = schema

    for attempt in range(1, RETRIES + 1):
        try:
            resp = get_client().models.generate_content(model=MODEL, contents=prompt, config=config)
            return resp.text
        except errors.ServerError as e:      # 503: server Google quá tải
            wait = 10 * attempt
            print(f"    [{e.code}] server bận, thử lại lần {attempt}/{RETRIES} sau {wait}s")
        except errors.ClientError as e:
            if e.code != 429:                # sai key, sai tên model... thử lại cũng vô ích
                raise
            wait = 30 * attempt
            print(f"    [429] vượt giới hạn, thử lại lần {attempt}/{RETRIES} sau {wait}s")
        time.sleep(wait)
    return None


# ----------------------------------------------------------------------------
# 3. Tóm tắt từng bài (tin + dư luận)
# ----------------------------------------------------------------------------
class ArticleDigest(BaseModel):      # khuôn JSON bắt model trả về
    id: str
    tom_tat: str
    du_luan: str


ARTICLE_SYSTEM_PROMPT = """Bạn là biên tập viên viết bản tin buổi sáng cho người bận rộn.
Đầu vào gồm nhiều bài báo, mỗi bài bắt đầu bằng '### ID'. Với TỪNG bài, viết:

- tom_tat: 3-4 câu, đọc xong không cần mở bài gốc vẫn nắm đủ ý chính: chuyện gì xảy ra,
  ở đâu, ai liên quan, con số quan trọng, nguyên nhân hoặc diễn biến tiếp theo nếu bài có nêu.
  CHỈ dùng thông tin trong bài được cung cấp, không thêm kiến thức bên ngoài, không suy đoán.
  Nếu phần nội dung trống, tóm tắt từ tiêu đề và mô tả.
- du_luan: 3-5 câu cho biết độc giả đang nói gì, ưu tiên ý được nhiều lượt thích: các luồng
  ý kiến chính, ý nào được đồng tình nhiều nhất, có ý kiến trái chiều không, thái độ chung
  (đồng tình, phản đối, lo lắng, bức xúc...). Không trích nguyên văn, không nêu tên người
  bình luận. Ít bình luận thì viết ngắn hơn; không có bình luận thì ghi "Chưa có bình luận đáng chú ý."

Riêng bài chuyên mục tam-su (thư độc giả kể chuyện riêng): tom_tat kể lại hoàn cảnh và điều người
viết đang băn khoăn; du_luan nêu các lời khuyên chính độc giả đưa ra và ý nào được đồng tình nhiều nhất.

Mỗi bài xử lý độc lập, không trộn thông tin giữa các bài. Văn phong trung lập, rõ ràng như báo chí.
Trả về đúng một mục cho mỗi ID."""


def _clean(text, limit):
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:limit]


def _article_block(row, comments):
    lines = [
        f"### ID: {row.ArticleID}",
        f"Chuyên mục: {_clean(getattr(row, 'Category', ''), 50)}",
        f"Tiêu đề: {row.Title}",
        f"Mô tả: {_clean(getattr(row, 'Description', ''), 500)}",
        f"Nội dung: {_clean(getattr(row, 'Body', ''), MAX_BODY_CHARS)}",
    ]
    top = comments.sort_values("Likes", ascending=False).head(MAX_COMMENTS)
    lines.append(f"Bình luận ({len(top)} bình luận nhiều thích nhất / tổng {int(row.CommentCount)}):")
    for c in top.itertuples():
        lines.append(f"- [{c.Likes} thích] {_clean(c.Content, MAX_COMMENT_CHARS)}")
    return "\n".join(lines)


def _summarize_batch(rows, groups, empty):
    """Tóm tắt một nhóm bài. Trả về {ArticleID: {"tom_tat", "du_luan"}}; bài bị bỏ sót thì không có."""
    prompt = "\n\n".join(_article_block(r, groups.get(r.ArticleID, empty)) for r in rows)
    text = _generate(prompt, ARTICLE_SYSTEM_PROMPT, max_tokens=8000, schema=list[ArticleDigest])
    if not text:
        return {}
    try:
        items = json.loads(text)
    except json.JSONDecodeError:            # JSON bị cắt ngang
        print("    JSON trả về bị lỗi, bỏ qua nhóm này")
        return {}

    valid_ids = {r.ArticleID for r in rows}
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        aid = str(it.get("id"))
        if aid in valid_ids and it.get("tom_tat"):     # chỉ nhận ID có thật, phòng model bịa ID
            out[aid] = {"tom_tat": it["tom_tat"].strip(), "du_luan": (it.get("du_luan") or "").strip()}
    return out


def summarize_articles(top_df, comment_df):
    """Tóm tắt các bài trong top_df. Bài bị sót ở lần gom nhóm sẽ được gọi lại từng bài một."""
    comment_df = comment_df.copy()
    comment_df["ArticleID"] = comment_df["ArticleID"].astype(str)
    groups = {aid: g for aid, g in comment_df.groupby("ArticleID")}
    empty = comment_df.iloc[0:0]

    rows = list(top_df.itertuples())
    results = {}
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        results.update(_summarize_batch(batch, groups, empty))
        print(f"  Tóm tắt: {min(start + BATCH_SIZE, len(rows))}/{len(rows)} bài")

    missing = [r for r in rows if r.ArticleID not in results]
    for r in missing:                       # thử lại riêng từng bài bị sót
        print(f"  Thử lại riêng bài {r.ArticleID}")
        results.update(_summarize_batch([r], groups, empty))
    return results


# ----------------------------------------------------------------------------
# 4. Tổng quan trong ngày
# ----------------------------------------------------------------------------
DAY_SYSTEM_PROMPT = """Bạn viết đoạn mở đầu cho bản tin buổi sáng.
Dựa CHỈ trên các tóm tắt được cung cấp, viết 3-4 câu tiếng Việt nêu những điểm đáng chú ý nhất
hôm nay và dư luận đang quan tâm điều gì. Không liệt kê từng tin, không thêm thông tin bên ngoài.
Chỉ trả về đoạn văn."""


def summarize_day(items):
    lines = [f"- [{it['category']}] {it['title']}: {it['tom_tat']} Dư luận: {it['du_luan']}"
             for it in items if it.get("tom_tat")]
    if not lines:
        return None
    text = _generate("\n".join(lines), DAY_SYSTEM_PROMPT, max_tokens=1000)
    return text.strip() if text else None


# ----------------------------------------------------------------------------
# 5. Ghép bản tin và lưu JSON
# ----------------------------------------------------------------------------
def _to_items(df, summaries):
    items = []
    for rank, r in enumerate(df.itertuples(), 1):
        s = summaries.get(r.ArticleID, {})
        items.append({
            "rank": rank,
            "id": r.ArticleID,
            "title": r.Title,
            "url": r.URL,
            "category": _clean(getattr(r, "Category", ""), 50) or None,
            "description": _clean(getattr(r, "Description", ""), 1000) or None,  # web dùng tạm nếu AI không tóm tắt được
            "publish_date": r.PublishDate.strftime("%Y-%m-%d %H:%M"),
            "comment_count": int(r.CommentCount),
            "comments_per_hour": round(float(r.CommentsPerHour), 1),
            "tom_tat": s.get("tom_tat"),
            "du_luan": s.get("du_luan"),
        })
    return items


def build_digest(articles_df, comment_df, now=None, top_n=TOP_N, out_dir="digests"):
    now = now or datetime.now(VN_TZ).replace(tzinfo=None)
    top = pick_top(articles_df, now=now, top_n=top_n, exclude=TAM_SU)
    tam_su = pick_top(articles_df, now=now, top_n=TAM_SU_N, only=TAM_SU)
    print(f"Chọn được {len(top)} bài + {len(tam_su)} bài Tâm sự trong 24 giờ trước {now:%Y-%m-%d %H:%M}")
    if top.empty:
        return None

    summaries = summarize_articles(pd.concat([top, tam_su]), comment_df)
    items = _to_items(top, summaries)

    digest = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "model": MODEL,
        "overview": summarize_day(items),      # tổng quan chỉ dựa trên tin chính
        "items": items,
        "tam_su": _to_items(tam_su, summaries),
    }

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{digest['date']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    done = sum(1 for it in items if it["tom_tat"])
    print(f"Đã lưu {path}: {done}/{len(items)} bài có tóm tắt")
    return digest


if __name__ == "__main__":
    # Ví dụ: python ai_summary.py articles.csv comments.csv --now "2026-10-29 18:00"
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Tạo bản tin trong ngày từ dữ liệu đã crawl")
    p.add_argument("articles_csv")
    p.add_argument("comments_csv")
    p.add_argument("--now", help="mốc thời gian 'YYYY-MM-DD HH:MM' (mặc định: bây giờ)")
    p.add_argument("--top", type=int, default=TOP_N)
    args = p.parse_args()

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else None
    build_digest(pd.read_csv(args.articles_csv), pd.read_csv(args.comments_csv), now=now, top_n=args.top)
