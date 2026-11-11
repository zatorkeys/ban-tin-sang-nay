"""Hỏi đáp trên kho bài VnExpress đã crawl (RAG).

    python rag.py index                                   # tạo / cập nhật chỉ mục từ data/articles/
    python rag.py ask "Tuần trước độc giả nói gì về giá vàng?"

Luồng xử lý:
    1. Mỗi bài thành vài document: các đoạn nội dung (type=body) và một document gom
       bình luận nhiều thích nhất (type=comments). Embedding bằng Gemini, lưu vào ChromaDB.
    2. Khi hỏi: Gemini đọc câu hỏi, trả về câu truy vấn + bộ lọc ngày / chuyên mục
       (self-query), rồi tìm vector trong phạm vi đó, riêng cho nội dung bài và bình luận.
    3. Gemini viết câu trả lời chỉ từ các đoạn tìm được, đánh số nguồn [1], [2]...

Chỉ mục nằm ở rag_index/ (không commit, dựng lại được bất cứ lúc nào). Chạy `index` nhiều lần
chỉ embedding phần mới: bài mới, hoặc bài có thêm bình luận so với lần trước.
"""
import argparse
import glob
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import chromadb
from google.genai import errors, types
from pydantic import BaseModel

from ai_summary import RETRIES, VN_TZ, _generate, get_client, with_retry
from crawler import CATEGORIES

EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = 768             # đủ tốt cho tìm kiếm, nhẹ hơn 4 lần so với mặc định 3072
EMBED_BATCH = 100           # số đoạn mỗi lần gọi API (tự giảm khi bị giới hạn)
MIN_EMBED_BATCH = 5
DATA_GLOB = os.path.join("data", "articles", "*.jsonl.gz")
INDEX_DIR = "rag_index"
COLLECTION = "vnexpress"

CHUNK_CHARS = 1500          # một đoạn ~ 400 token, đủ chứa 2-4 đoạn văn
MAX_CHUNKS = 4              # phần cuối bài dài thường là chi tiết phụ
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 300
MAX_DOC_CHARS = 3000        # model embedding chỉ đọc ~2.048 token đầu

TOP_BODY = 10               # số đoạn nội dung lấy về mỗi câu hỏi
TOP_COMMENTS = 5            # số document bình luận (nhiều hơn khi hỏi về dư luận)
MAX_SOURCES = 6             # số bài tối đa đưa vào câu trả lời
EMPTY_INDEX = f"Chỉ mục trống ({INDEX_DIR}/ chưa có dữ liệu). Chạy `python rag.py index` trước."
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


# ----------------------------------------------------------------------------
# 1. Dữ liệu -> document
# ----------------------------------------------------------------------------
def load_articles(pattern=DATA_GLOB):
    """Đọc mọi file dữ liệu thô. Bài xuất hiện ở nhiều ngày (khung 24 giờ chồng nhau)
    thì giữ bản crawl sau cùng, vì có nhiều bình luận hơn."""
    articles = {}
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                articles[str(rec["ArticleID"])] = rec
    return list(articles.values())


def split_body(body):
    """Cắt nội dung theo đoạn văn, gom thành các khúc <= CHUNK_CHARS ký tự."""
    chunks, cur = [], ""
    for para in (p.strip() for p in (body or "").split("\n")):
        while len(para) > CHUNK_CHARS:                    # đoạn văn quá dài: cắt cứng
            chunks.append(para[:CHUNK_CHARS])
            para = para[CHUNK_CHARS:]
        if cur and len(cur) + len(para) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks[:MAX_CHUNKS]


def article_docs(rec):
    """Một bài -> danh sách (id, text, metadata)."""
    aid = str(rec["ArticleID"])
    title = rec.get("Title") or ""
    publish = rec.get("PublishDate") or ""
    meta = {
        "article_id": aid,
        "title": title,
        "url": rec.get("URL") or "",
        "category": rec.get("Category") or "",
        "publish": publish,
        "day": int(publish[:10].replace("-", "") or 0),       # 20261110, để lọc theo khoảng ngày
        "comment_count": int(float(rec.get("CommentCount") or 0)),
    }
    docs = []
    # đặt tiêu đề trước mỗi đoạn để đoạn giữa bài vẫn biết mình nói về chuyện gì
    body_chunks = split_body(rec.get("Body"))
    for i, chunk in enumerate(body_chunks or [""]):
        head = f"{title}\n{rec.get('Description') or ''}" if i == 0 else title
        docs.append((f"{aid}-b{i}", f"{head}\n{chunk}".strip(), {**meta, "type": "body"}))

    comments = sorted(rec.get("comments") or [], key=lambda c: -(c.get("Likes") or 0))[:MAX_COMMENTS]
    if comments:
        lines = [f"- ({c.get('Likes') or 0} thích) {(c.get('Content') or '').strip()[:MAX_COMMENT_CHARS]}"
                 for c in comments]
        text = (f"Bình luận của độc giả về bài: {title}\n" + "\n".join(lines))[:MAX_DOC_CHARS]
        docs.append((f"{aid}-c", text, {**meta, "type": "comments"}))
    return docs


# ----------------------------------------------------------------------------
# 2. Embedding + chỉ mục
# ----------------------------------------------------------------------------
def embed(texts, task, retry=True):
    """task: RETRIEVAL_DOCUMENT khi lưu, RETRIEVAL_QUERY khi hỏi. Trả về None nếu API lỗi mãi.

    retry=False: gọi một lần, lỗi thì ném ra để build_index tự xử lý (giảm cỡ lô).
    """
    config = types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM)
    call = lambda: get_client().models.embed_content(model=EMBED_MODEL, contents=texts, config=config)
    resp = with_retry(call) if retry else call()
    return [e.values for e in resp.embeddings] if resp is not None else None


def _quota_name(err):
    """Tên hạn mức bị vượt trong lỗi 429, vd. 'EmbedContentInputTokensPerModelPerMinute-FreeTier'."""
    m = re.search(r"'quotaId': '([^']+)'|\"quotaId\": \"([^\"]+)\"", str(err))
    return (m.group(1) or m.group(2)) if m else ""


def get_collection():
    client = chromadb.PersistentClient(path=INDEX_DIR)
    return client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})


def build_index():
    col = get_collection()
    docs = [d for rec in load_articles() for d in article_docs(rec)]
    existing = col.get(include=["metadatas"])
    seen = dict(zip(existing["ids"], existing["metadatas"]))
    # document bình luận phải embedding lại khi bài có thêm bình luận
    todo = [d for d in docs if d[0] not in seen
            or (d[2]["type"] == "comments" and seen[d[0]]["comment_count"] != d[2]["comment_count"])]
    print(f"{len(docs)} document từ {len({d[2]['article_id'] for d in docs})} bài; "
          f"đã có {len(seen)}, cần embedding {len(todo)}")

    # gói miễn phí giới hạn token mỗi phút: gặp 429 thì chia đôi lô và chờ hết phút, đến khi lọt
    size, start, fails = EMBED_BATCH, 0, 0
    while start < len(todo):
        batch = todo[start:start + size]
        try:
            vectors = embed([d[1] for d in batch], "RETRIEVAL_DOCUMENT", retry=False)
        except errors.APIError as e:
            if e.code != 429 and not 500 <= e.code < 600:
                raise
            quota = _quota_name(e)
            if "PerDay" in quota:
                sys.exit(f"Hết hạn mức trong ngày ({quota}). Phần đã xong vẫn được giữ, mai chạy lại để làm tiếp.")
            fails += 1
            if fails > RETRIES:
                sys.exit(f"Gemini lỗi liên tục ({e.code} {quota}). Phần đã xong vẫn được giữ, chạy lại để làm tiếp.")
            if e.code == 429:
                size = max(MIN_EMBED_BATCH, size // 2)
            print(f"    [{e.code}] {quota or e.status}: lô còn {size} đoạn, chờ 60s")
            time.sleep(60)
            continue
        fails = 0
        # ghi từng lô: ngắt giữa chừng thì lần sau làm tiếp, không mất công
        col.upsert(ids=[d[0] for d in batch], documents=[d[1] for d in batch],
                   metadatas=[d[2] for d in batch], embeddings=vectors)
        start += len(batch)
        print(f"  {start}/{len(todo)}")
    print(f"Chỉ mục có {col.count()} document ({INDEX_DIR}/)")


# ----------------------------------------------------------------------------
# 3. Hiểu câu hỏi (self-query)
# ----------------------------------------------------------------------------
class QueryPlan(BaseModel):
    search_query: str
    date_from: str          # YYYY-MM-DD, "" nếu không giới hạn
    date_to: str
    categories: list[str]
    wants_opinion: bool


PLAN_SYSTEM_PROMPT = """Bạn chuyển câu hỏi của người dùng về tin tức VnExpress thành truy vấn tìm kiếm.
Trả về JSON:
- search_query: câu truy vấn ngắn gọn bằng tiếng Việt, chỉ giữ nội dung cần tìm,
  bỏ các từ chỉ thời gian và các từ như "độc giả nói gì".
- date_from, date_to: khoảng ngày đăng bài (YYYY-MM-DD) nếu câu hỏi nhắc tới thời gian, ngược lại để "".
  "hôm qua" = một ngày; "tuần trước" = Thứ Hai đến Chủ Nhật của tuần trước;
  "tuần này" = Thứ Hai tuần này đến hôm nay; "gần đây", "mấy hôm nay" = 7 ngày gần nhất.
- categories: slug chuyên mục, CHỈ khi câu hỏi nói rõ lĩnh vực; không chắc thì để [] (lọc sai sẽ mất kết quả).
- wants_opinion: true nếu người dùng hỏi về ý kiến, phản ứng, tranh cãi, lời khuyên của độc giả."""


def plan_query(question, today):
    cats = ", ".join(f"{slug} ({name})" for slug, name in CATEGORIES.items())
    prompt = (f"Hôm nay: {WEEKDAYS[today.weekday()]}, {today:%Y-%m-%d}.\n"
              f"Các chuyên mục: {cats}.\n\nCâu hỏi: {question}")
    text = _generate(prompt, PLAN_SYSTEM_PROMPT, max_tokens=500, schema=QueryPlan)
    try:
        plan = QueryPlan.model_validate_json(text)
    except Exception:                       # model lỗi: tìm thẳng bằng câu hỏi, không lọc
        return QueryPlan(search_query=question, date_from="", date_to="", categories=[], wants_opinion=False)
    plan.categories = [c for c in plan.categories if c in CATEGORIES]
    for field in ("date_from", "date_to"):
        try:
            datetime.strptime(getattr(plan, field), "%Y-%m-%d")
        except ValueError:
            setattr(plan, field, "")
    plan.search_query = plan.search_query.strip() or question
    return plan


def where_filter(plan, doc_type, use_plan=True):
    conds = [{"type": doc_type}]
    if use_plan:
        if plan.date_from:
            conds.append({"day": {"$gte": int(plan.date_from.replace("-", ""))}})
        if plan.date_to:
            conds.append({"day": {"$lte": int(plan.date_to.replace("-", ""))}})
        if plan.categories:
            conds.append({"category": {"$in": plan.categories}})
    return conds[0] if len(conds) == 1 else {"$and": conds}


# ----------------------------------------------------------------------------
# 4. Tìm + trả lời
# ----------------------------------------------------------------------------
def retrieve(col, plan, vector, use_plan=True):
    """Tìm riêng nội dung bài và bình luận, gom theo bài, giữ thứ tự bài khớp nhất trước."""
    hits = []
    for doc_type, k in (("body", TOP_BODY), ("comments", TOP_COMMENTS * (2 if plan.wants_opinion else 1))):
        res = col.query(query_embeddings=vector, n_results=k, where=where_filter(plan, doc_type, use_plan),
                        include=["documents", "metadatas", "distances"])
        hits += zip(res["distances"][0], res["documents"][0], res["metadatas"][0])

    articles = {}
    for dist, doc, meta in sorted(hits, key=lambda h: h[0]):
        a = articles.setdefault(meta["article_id"], {"meta": meta, "body": [], "comments": None})
        if meta["type"] == "comments":
            a["comments"] = doc
        else:
            a["body"].append(doc)
    return list(articles.values())[:MAX_SOURCES]


ANSWER_SYSTEM_PROMPT = """Bạn trả lời câu hỏi về tin tức dựa trên các bài VnExpress được cung cấp.
- CHỈ dùng thông tin trong các nguồn bên dưới. Nguồn không đủ để trả lời thì nói rõ là không tìm thấy.
- Ghi số nguồn ngay sau ý lấy từ nguồn đó, dạng [1] hoặc [1][3].
- Tách bạch sự việc (nội dung bài báo) với ý kiến độc giả (phần bình luận). Bình luận chỉ là ý kiến
  của người đọc, không phải sự thật đã kiểm chứng; không trích nguyên văn, không nêu tên người bình luận.
- Khi các nguồn ở những ngày khác nhau, nêu diễn biến theo thời gian.
- Trả lời bằng tiếng Việt, ngắn gọn, đi thẳng vào câu hỏi."""


def ask(question, today=None):
    """Trả về {"answer", "sources", "plan", "filtered"}; RuntimeError nếu chỉ mục trống hoặc Gemini lỗi."""
    col = get_collection()
    if col.count() == 0:                # báo ngay, khỏi tốn lượt gọi Gemini
        raise RuntimeError(EMPTY_INDEX)
    today = today or datetime.now(VN_TZ).replace(tzinfo=None)
    plan = plan_query(question, today)
    vector = embed([plan.search_query], "RETRIEVAL_QUERY")
    if vector is None:
        raise RuntimeError("Không embedding được câu hỏi: API key đã hết hạn mức embedding trong ngày "
                           "(làm mới lúc 0 giờ giờ Mỹ, khoảng 14-15 giờ Việt Nam) hoặc Gemini đang lỗi.")

    found = retrieve(col, plan, vector)
    filtered = True
    if not found:                       # bộ lọc quá hẹp (vd. hỏi ngày chưa có dữ liệu): bỏ lọc
        found, filtered = retrieve(col, plan, vector, use_plan=False), False

    blocks, sources = [], []
    for n, a in enumerate(found, 1):
        m = a["meta"]
        parts = [f"[{n}] {m['title']} (đăng {m['publish']}, {CATEGORIES.get(m['category'], m['category'])})"]
        parts += [f"Nội dung bài:\n{b}" for b in a["body"]]
        if a["comments"]:
            parts.append(a["comments"])
        blocks.append("\n".join(parts))
        sources.append({"n": n, "title": m["title"], "url": m["url"], "publish": m["publish"]})

    prompt = f"Câu hỏi: {question}\n\nNguồn:\n\n" + "\n\n".join(blocks)
    answer = _generate(prompt, ANSWER_SYSTEM_PROMPT, max_tokens=1500) or "Gemini đang lỗi, thử lại sau."
    return {"answer": answer, "sources": sources, "plan": plan, "filtered": filtered}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="tạo / cập nhật chỉ mục từ data/articles/")
    q = sub.add_parser("ask", help="đặt câu hỏi")
    q.add_argument("question")
    q.add_argument("--today", help="giả lập ngày hỏi 'YYYY-MM-DD' (mặc định: hôm nay)")
    args = p.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Thiếu biến môi trường GEMINI_API_KEY")
    if args.cmd == "index":
        build_index()
        return

    today = datetime.strptime(args.today, "%Y-%m-%d") if args.today else None
    try:
        r = ask(args.question, today)
    except RuntimeError as e:
        sys.exit(str(e))
    plan = r["plan"]
    scope = [f"{plan.date_from or '…'} → {plan.date_to or '…'}"] if plan.date_from or plan.date_to else []
    scope += plan.categories
    print(f"Tìm: \"{plan.search_query}\"" + (f" | lọc: {', '.join(scope)}" if scope else "")
          + ("" if r["filtered"] else " | (không có kết quả trong phạm vi lọc, đã tìm trên toàn bộ)"))
    print()
    print(r["answer"])
    print()
    for s in r["sources"]:
        print(f"[{s['n']}] {s['title']} ({s['publish']})\n    {s['url']}")


if __name__ == "__main__":
    main()
