import os
import re
from typing import List
from bs4 import BeautifulSoup
from .models import db, Chunk, Article


def html_to_text(html: str) -> str:
    """
    Очищаем HTML в удобный для индексации плейнтекст.
    Медиа заменяем на маркеры, чтобы сохранялся контекст.
    """
    soup = BeautifulSoup(html or "", "html.parser")

    # маркеры медиа
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        fname = (img.get("src") or "").split("/")[-1]
        img.replace_with(f"[IMAGE: {alt or fname}]")

    for v in soup.find_all("video"):
        src = v.get("src") or (v.find("source").get("src") if v.find("source") else "")
        v.replace_with(f"[VIDEO: {src.split('/')[-1]}]")

    for a in soup.find_all("audio"):
        src = a.get("src") or (a.find("source").get("src") if a.find("source") else "")
        a.replace_with(f"[AUDIO: {src.split('/')[-1]}]")

    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> List[str]:
    """
    Простая разбивка по абзацам + символам, с перекрытием.
    """
    if not text:
        return []
    paras = re.split(r"\n\s*\n+", text.strip())
    chunks, buf, length = [], [], 0

    for p in paras:
        p = p.strip()
        if not p:
            continue
        add_len = (2 if buf else 0) + len(p)
        if length + add_len <= chunk_size:
            buf.append(p)
            length += add_len
        else:
            if buf:
                chunks.append("\n\n".join(buf))
            if overlap and chunks:
                tail = chunks[-1][-overlap:]
                buf = [tail, p]
                length = len(tail) + 2 + len(p)
            else:
                buf = [p]
                length = len(p)

    if buf:
        chunks.append("\n\n".join(buf))

    return [c[:chunk_size] for c in chunks]


def rebuild_article_chunks(article: Article, chunk_size: int | None = None, overlap: int | None = None):
    """
    Пересобираем чанки одной статьи (вызывается после создания/редактирования/отката).
    """
    cs = int(os.environ.get("CHUNK_SIZE_CHARS", str(chunk_size or 1200)))
    ov = int(os.environ.get("CHUNK_OVERLAP", str(overlap or 200)))

    # удалить старые чанки
    Chunk.query.filter_by(article_id=article.id).delete()
    db.session.flush()

    text = html_to_text(article.content or "")
    parts = chunk_text(text, cs, ov)
    for i, part in enumerate(parts):
        db.session.add(Chunk(article_id=article.id, idx=i, text=part, tokens=len(part.split())))
    db.session.commit()


def rebuild_all_chunks():
    for a in Article.query.filter_by(is_deleted=False).all():
        rebuild_article_chunks(a)
