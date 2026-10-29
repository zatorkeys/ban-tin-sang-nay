"""Thu thập bài VnExpress đăng trong 24 giờ qua, kèm nội dung và bình luận.

Luồng xử lý (mỗi chuyên mục):
    1. Đọc RSS (60 bài mới nhất, có sẵn giờ đăng) -> giữ bài trong cửa sổ 24 giờ.
       Nếu RSS chưa phủ hết 24 giờ (chuyên mục đăng nhiều tin), lấy thêm URL
       từ trang danh sách -p1..-pN, giờ đăng sẽ đọc từ trang bài.
    2. Vào từng bài: tác giả, giờ đăng, nội dung, categoryid.
    3. Gọi API bình luận (1 request/bài): tổng số bình luận + bình luận nhiều thích nhất.

Kết quả: articles_df (một dòng một bài) và comment_df (một dòng một bình luận gốc),
đúng định dạng ai_summary.build_digest() cần.

Chạy thử:  python crawler.py --categories suc-khoe --out-dir tmp
"""
import argparse
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

VN_TZ = timezone(timedelta(hours=7))

# slug trên VnExpress -> tên hiển thị
CATEGORIES = {
    "thoi-su": "Thời sự",
    "the-gioi": "Thế giới",
    "kinh-doanh": "Kinh doanh",
    "phap-luat": "Pháp luật",
    "giao-duc": "Giáo dục",
    "suc-khoe": "Sức khỏe",
    "doi-song": "Đời sống",
    "bat-dong-san": "Bất động sản",
    "khoa-hoc-cong-nghe": "Khoa học công nghệ",
    "the-thao": "Thể thao",
    "giai-tri": "Giải trí",
    "du-lich": "Du lịch",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://google.com",
}
COMMENT_API = "https://usi-saas.vnexpress.net/index/get"
SITE_ID = 1000000
COMMENT_LIMIT = 30        # 1 lần gọi/bài: đủ cho tổng số + 30 bình luận nhiều thích nhất
LISTING_PAGES = 3         # số trang danh sách xem thêm khi RSS không phủ đủ 24 giờ
DELAY = (0.5, 1.0)        # nghỉ ngẫu nhiên sau mỗi request

session = requests.Session()   # dùng lại kết nối
session.headers.update(HEADERS)


def polite_sleep():
    time.sleep(random.uniform(*DELAY))


def get_with_retry(url, params=None, headers=None, retries=3):
    """Gửi GET, tự thử lại khi lỗi mạng hoặc server trả 429/5xx. Thất bại thì trả về None."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 404:      # bài đã bị gỡ: không cần thử lại
                return None
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            wait = 2 * attempt + random.random()
            print(f"    [Lỗi] {e} -> thử lại lần {attempt}/{retries} sau {wait:.1f}s")
            time.sleep(wait)
    return None


def get_article_id(url):
    """Mã bài = dãy số cuối URL, ví dụ ...-5130372.html -> '5130372'."""
    m = re.search(r"-(\d+)\.html", url or "")
    return m.group(1) if m else None


def is_article_url(url):
    return bool(url) and url.startswith("https://vnexpress.net/") and "/topic/" not in url \
        and get_article_id(url) is not None


# ----------------------------------------------------------------------------
# 1. Danh sách bài: RSS + trang chuyên mục
# ----------------------------------------------------------------------------
def fetch_rss(category):
    """Trả về list dict {URL, Title, Description, Image, PublishDate (datetime giờ VN, không tz)}."""
    resp = get_with_retry(f"https://vnexpress.net/rss/{category}.rss")
    polite_sleep()
    if resp is None:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        print(f"    RSS {category} lỗi định dạng")
        return []

    items = []
    for it in root.findall("./channel/item"):
        url = (it.findtext("link") or "").strip()
        if not is_article_url(url):
            continue
        desc_soup = BeautifulSoup(it.findtext("description") or "", "html.parser")
        img = desc_soup.find("img")
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(VN_TZ).replace(tzinfo=None)
        except (TypeError, ValueError):
            pub = None
        items.append({
            "URL": url,
            "Title": (it.findtext("title") or "").strip(),
            "Description": desc_soup.get_text(" ", strip=True),
            "Image": img.get("src") if img else None,
            "PublishDate": pub,
        })
    return items


def fetch_listing(category, page):
    """URL + tiêu đề + mô tả từ trang chuyên mục (không có giờ đăng)."""
    resp = get_with_retry(f"https://vnexpress.net/{category}-p{page}")
    polite_sleep()
    if resp is None:
        return []
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    for article in soup.find_all("article", class_=re.compile("^item-new")):
        a = article.find("a")
        url = a.get("href") if a else None
        if not is_article_url(url):
            continue
        desc_tag = article.find("p", class_="description")
        description = ""
        if desc_tag:
            area = desc_tag.find("span", class_="location-stamp")
            if area:
                area.extract()               # bỏ chữ địa danh đứng đầu mô tả
            description = desc_tag.get_text(" ", strip=True)
        items.append({"URL": url, "Title": a.get("title") or a.get_text(strip=True),
                      "Description": description, "Image": None, "PublishDate": None})
    return items


def candidates_for_category(category, start, end):
    """Các bài có thể nằm trong [start, end]. Bài có PublishDate=None thì cần đọc trang bài mới biết."""
    rss = fetch_rss(category)
    dated = [it for it in rss if it["PublishDate"] is not None]
    fresh = [it for it in dated if start <= it["PublishDate"] <= end]

    # RSS chỉ có 60 bài: nếu bài cũ nhất vẫn còn trong cửa sổ thì có thể đã sót bài
    if not dated or min(it["PublishDate"] for it in dated) > start:
        print(f"    RSS chưa phủ đủ 24 giờ, xem thêm {LISTING_PAGES} trang danh sách")
        known = {it["URL"] for it in rss}
        for page in range(1, LISTING_PAGES + 1):
            for it in fetch_listing(category, page):
                if it["URL"] not in known:
                    known.add(it["URL"])
                    fresh.append(it)
    return fresh


# ----------------------------------------------------------------------------
# 2. Chi tiết bài
# ----------------------------------------------------------------------------
def get_meta(soup, *names):
    """Lấy content của thẻ <meta> đầu tiên khớp name / property / itemprop."""
    for name in names:
        for attr in ("name", "property", "itemprop"):
            tag = soup.find("meta", attrs={attr: name})
            if tag and tag.get("content"):
                return tag["content"].strip()
    return None


def _is_right_aligned(p):
    return "text-align:right" in (p.get("style") or "").replace(" ", "").lower()


def parse_author(soup):
    for selector in ["p.author_mail strong", "p.author strong", "p.author_mail", "p.author"]:
        tag = soup.select_one(selector)
        if tag and tag.get_text(strip=True):
            return tag.get_text(" ", strip=True)

    # VnExpress thường để tên tác giả ở đoạn cuối thân bài, căn phải
    body = soup.select_one("article.fck_detail")
    if body:
        for p in reversed(body.select("p")):
            text = p.get_text(" ", strip=True)
            if _is_right_aligned(p) and text:
                return text
    return get_meta(soup, "author", "article:author")


def parse_date(soup):
    """Giờ đăng (datetime giờ VN, không tz) hoặc None."""
    iso = get_meta(soup, "datePublished", "pubdate", "article:published_time")
    if iso:
        try:
            return datetime.fromisoformat(iso).astimezone(VN_TZ).replace(tzinfo=None)
        except ValueError:
            pass
    # Chữ hiển thị trên trang, ví dụ: 'Thứ năm, 29/10/2026, 10:05 (GMT+7)'
    tag = soup.select_one("span.date")
    if tag:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}),?\s*(\d{1,2}:\d{2})", tag.get_text())
        if m:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M")
    return None


def parse_body(soup):
    """Nội dung bài: các đoạn p.Normal trong article.fck_detail, bỏ dòng tên tác giả."""
    body = soup.select_one("article.fck_detail")
    if body is None:
        return ""
    paras = body.select("p.Normal") or body.find_all("p")
    return "\n".join(t for p in paras if not _is_right_aligned(p)
                     if (t := p.get_text(" ", strip=True)))


def to_vn_time(ts):
    """Đổi Unix timestamp sang 'YYYY-MM-DD HH:MM' giờ Việt Nam."""
    try:
        return datetime.fromtimestamp(int(ts), VN_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def fetch_comments(article_id, category_id):
    """1 lần gọi API bình luận, sắp theo lượt thích.

    Trả về (số bình luận gốc, tổng kể cả trả lời, list bình luận gốc + bình luận ghim).
    """
    params = {"objectid": article_id, "objecttype": 1, "siteid": SITE_ID,
              "offset": 0, "limit": COMMENT_LIMIT, "sort_by": "like"}
    if category_id:
        params["categoryid"] = category_id

    resp = get_with_retry(COMMENT_API, params=params, headers={"Referer": "https://vnexpress.net/"})
    polite_sleep()
    if resp is None:
        return None, None, []
    try:
        payload = resp.json()
    except ValueError:
        return None, None, []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):           # bài không có hoặc đã tắt bình luận
        return 0, 0, []

    comments, seen = [], set()
    for item in (data.get("items_pin") or []) + (data.get("items") or []):
        cid = item.get("comment_id")
        if cid in seen:
            continue
        seen.add(cid)
        replys = item.get("replys") or {}
        comments.append({
            "ArticleID": article_id,
            "CommentID": cid,
            "Content": item.get("content") or "",
            "Likes": int(item.get("userlike") or 0),
            "ReplyCount": int(replys.get("total") or 0),
            "Time": to_vn_time(item.get("creation_time")),
        })
    return data.get("total"), data.get("totalitem"), comments


def crawl_article(item, category, start, end):
    """Đọc trang bài + bình luận. Trả về (dict bài, list bình luận); bài ngoài cửa sổ/lỗi -> (None, [])."""
    url = item["URL"]
    resp = get_with_retry(url)
    polite_sleep()
    if resp is None:
        return None, []
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    publish = parse_date(soup) or item.get("PublishDate")
    if publish is None or not (start <= publish <= end):
        return None, []                      # bài cũ lấy từ trang danh sách: bỏ, không gọi API bình luận

    article_id = get_meta(soup, "tt_article_id") or get_article_id(url)
    top_total, all_total, comments = fetch_comments(article_id, get_meta(soup, "tt_category_id"))

    h1 = soup.select_one("h1.title-detail")
    desc = soup.select_one("p.description")
    article = {
        "ArticleID": str(article_id),
        "URL": url,
        "Title": item.get("Title") or (h1.get_text(" ", strip=True) if h1 else ""),
        "Description": item.get("Description") or (desc.get_text(" ", strip=True) if desc else ""),
        "Category": category,
        "Author": parse_author(soup),
        "PublishDate": publish.strftime("%Y-%m-%d %H:%M"),
        "CommentCount": all_total,           # tổng kể cả trả lời
        "TopCommentCount": top_total,        # chỉ bình luận gốc
        "Image": item.get("Image") or get_meta(soup, "og:image"),
        "Body": parse_body(soup),
    }
    return article, comments


# ----------------------------------------------------------------------------
# 3. Gộp lại
# ----------------------------------------------------------------------------
def crawl_last_24h(now=None, categories=None, hours=24):
    """Crawl mọi chuyên mục. Trả về (articles_df, comment_df)."""
    end = now or datetime.now(VN_TZ).replace(tzinfo=None)
    start = end - timedelta(hours=hours)
    categories = categories or list(CATEGORIES)
    print(f"Crawl bài đăng từ {start:%Y-%m-%d %H:%M} đến {end:%Y-%m-%d %H:%M}")

    articles, comments, seen = [], [], set()
    for category in categories:
        todo = [it for it in candidates_for_category(category, start, end) if it["URL"] not in seen]
        seen.update(it["URL"] for it in todo)  # một bài có thể nằm ở 2 chuyên mục: chỉ lấy 1 lần
        print(f"[{category}] {len(todo)} bài cần đọc")

        kept = 0
        for i, item in enumerate(todo, 1):
            article, cmts = crawl_article(item, category, start, end)
            if article is None:
                continue
            articles.append(article)
            comments.extend(cmts)
            kept += 1
            if i % 20 == 0:
                print(f"    {i}/{len(todo)}")
        print(f"    giữ {kept} bài")

    articles_df = pd.DataFrame(articles)
    comment_df = pd.DataFrame(comments, columns=["ArticleID", "CommentID", "Content",
                                                 "Likes", "ReplyCount", "Time"])
    if not articles_df.empty:
        articles_df = articles_df.drop_duplicates("ArticleID").reset_index(drop=True)
        for col in ["CommentCount", "TopCommentCount"]:
            articles_df[col] = pd.to_numeric(articles_df[col], errors="coerce").astype("Int64")
    print(f"Xong: {len(articles_df)} bài, {len(comment_df)} bình luận")
    return articles_df, comment_df


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # console Windows không in được tiếng Việt
    p = argparse.ArgumentParser(description="Crawl bài VnExpress trong 24 giờ qua ra CSV")
    p.add_argument("--categories", nargs="*", help="mặc định: tất cả")
    p.add_argument("--out-dir", default=".")
    args = p.parse_args()

    a_df, c_df = crawl_last_24h(categories=args.categories)
    a_df.to_csv(f"{args.out_dir}/articles.csv", index=False, encoding="utf-8-sig")
    c_df.to_csv(f"{args.out_dir}/comments.csv", index=False, encoding="utf-8-sig")
