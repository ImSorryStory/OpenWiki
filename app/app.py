import os
import re
import json
import uuid
import fcntl
import mimetypes
import base64
import requests
from urllib.parse import urlparse, unquote
from datetime import datetime
from flask import (
    Flask, request, render_template, redirect, url_for,
    flash, session, send_from_directory, jsonify, Response, stream_with_context
)
from werkzeug.utils import secure_filename
import urllib.request
import urllib.error
import bleach
from bleach.css_sanitizer import CSSSanitizer
from .models import db, Section, Subsection, Article, Attachment, ArticleRevision, Chunk, Favorite
from .utils import parse_users_file, current_user, login_required, admin_required
from .rag import rebuild_article_chunks, rebuild_all_chunks



def create_app():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
    )

    # --- Config ---
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change_me_in_env")
    app.config["USERS_FILE"] = os.environ.get("USERS_FILE", "/data/user.txt")

    # БД: используем DATABASE_URL если задан, иначе sqlite в /data
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:////data/lertowiki.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Файлы
    default_upload = os.path.join(BASE_DIR, "uploads")
    app.config["UPLOAD_FOLDER"] = os.environ.get("UPLOAD_FOLDER", default_upload)
    app.config.setdefault("UPLOAD_URL", "/uploads")
    app.config.setdefault("MAX_CONTENT_LENGTH", 50 * 1024 * 1024 * 1024)

    os.makedirs(os.path.dirname(app.config["USERS_FILE"]), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs("/data", exist_ok=True)

    db.init_app(app)

    # --- Безопасная очистка HTML (разрешаем медиа) ---
    ALLOWED_TAGS = [
        "p", "br", "hr", "span", "div", "pre", "code",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "b", "em", "i", "u", "s", "blockquote",
        "ul", "ol", "li",
        "table", "thead", "tbody", "tr", "th", "td", "caption", "colgroup", "col",
        "a", "img", "figure", "figcaption",
        "video", "audio", "source"
    ]
    ALLOWED_ATTRS = {
        "*": ["class", "id", "style"],
        "a": ["href", "title", "target", "rel"],
        "img": ["src", "alt", "title", "width", "height", "loading"],
        "video": ["src", "poster", "controls", "preload", "width", "height"],
        "audio": ["src", "controls", "preload"],
        "source": ["src", "type"],
        # разрешим старые html-атрибуты, если TinyMCE их вдруг проставит
        "table": ["border", "cellpadding", "cellspacing", "width", "height", "style", "class"],
        "td": ["colspan", "rowspan", "width", "height", "style", "class"],
        "th": ["colspan", "rowspan", "width", "height", "style", "class"],
    }
    ALLOWED_PROTOCOLS = ["http", "https", "data", "blob"]
    
    CSS_ALLOWED_PROPERTIES = [
        # границы
        "border", "border-top", "border-right", "border-bottom", "border-left",
        "border-color", "border-width", "border-style", "border-collapse",
        # размеры
        "width", "height", "min-width", "max-width", "min-height", "max-height",
        # отступы/выравнивание
        "padding", "margin", "text-align", "vertical-align", "white-space",
        # цвета/фон/шрифт
        "color", "background", "background-color",
        "font-weight", "font-style", "font-size", "line-height"
    ]
    try:
        # для bleach 6.x
        css_sanitizer = CSSSanitizer(
            allowed_css_properties=CSS_ALLOWED_PROPERTIES,
            allowed_css_functions=["rgb", "rgba", "hsl", "hsla", "calc", "var", "url"],
            allowed_protocols=["http", "https", "data", "blob"],
        )
    except TypeError:
        # на случай более старой реализации без этих параметров
        css_sanitizer = CSSSanitizer(
            allowed_css_properties=CSS_ALLOWED_PROPERTIES
        )


    def sanitize_html(html: str) -> str:
        return bleach.clean(
            html or "",
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRS,
            protocols=ALLOWED_PROTOCOLS,
            css_sanitizer=css_sanitizer,
            strip=False,
        )   
    # --- Локализация внешних картинок/медиа в /uploads ---
    _REMOTE_MAX_BYTES = int(os.environ.get("MAX_REMOTE_MEDIA_BYTES", 25 * 1024 * 1024))  # 25 МБ на единицу

    _EXT_BY_MIME = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/heic": ".heic",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
    }

    def _is_allowed_media_mime(ctype: str) -> bool:
        ctype = (ctype or "").lower()
        return ctype.startswith(("image/", "video/", "audio/")) or ctype == "image/svg+xml"

    def _choose_ext(ctype: str, fallback_name: str = "") -> str:
        ctype = (ctype or "").lower()
        if ctype in _EXT_BY_MIME:
            return _EXT_BY_MIME[ctype]
        # попытаемся по имени
        ext = os.path.splitext(fallback_name)[1].lower()
        if ext:
            return ext
        # последний шанс — по mimetypes
        return mimetypes.guess_extension(ctype) or ""

    def _save_bytes_to_uploads(data: bytes, ctype: str, fallback_name: str = "") -> tuple[str, str]:
        """Сохраняет байты в /uploads, возвращает (filename, url)."""
        ext = _choose_ext(ctype, fallback_name)
        fname = secure_filename(f"{uuid.uuid4().hex}{ext}")
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        with open(path, "wb") as f:
            f.write(data)
        return fname, url_for("uploaded_file", filename=fname)

    def _download_to_uploads(url: str) -> tuple[str | None, str | None]:
        """
        Качает внешний ресурс в /uploads. Возвращает (filename, url) или (None, None) при неудаче.
        Ограничение по размеру — _REMOTE_MAX_BYTES.
        """
        try:
            # Базовые заголовки — некоторые CDN капризничают без User-Agent
            headers = {
                "User-Agent": "LertoWiki/1.0 (+https://lerto.local)",
                "Accept": "*/*",
            }
            with requests.get(url, headers=headers, stream=True, timeout=(7, 30)) as r:
                r.raise_for_status()

                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                # если сервер не прислал тип — попробуем угадать по расширению
                if not _is_allowed_media_mime(ctype):
                    guessed = (mimetypes.guess_type(url)[0] or "").lower()
                    if guessed:
                        ctype = guessed
                if not _is_allowed_media_mime(ctype):
                    return None, None

                # Имя файла — из Content-Disposition либо из URL-пути
                disp = r.headers.get("Content-Disposition") or ""
                fallback_name = ""
                if "filename=" in disp:
                    fallback_name = disp.split("filename=", 1)[1].strip('"; ')
                if not fallback_name:
                    try:
                        fallback_name = os.path.basename(urlparse(url).path)
                    except Exception:
                        fallback_name = ""

                # Стримим в память с ограничением
                chunks = []
                total = 0
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _REMOTE_MAX_BYTES:
                        return None, None
                    chunks.append(chunk)
                data = b"".join(chunks)

                fname, local_url = _save_bytes_to_uploads(data, ctype, fallback_name=fallback_name)
                return fname, local_url
        except Exception:
            return None, None

    def _data_uri_to_upload(uri: str) -> tuple[str | None, str | None]:
        """
        Преобразует data:image/...;base64,... в файл в /uploads.
        Возвращает (filename, url) или (None, None) если формат не поддержан.
        """
        try:
            if not uri.startswith("data:"):
                return None, None
            head, data_part = uri.split(",", 1)
            # пример head: data:image/png;base64
            mime = "text/plain"
            is_base64 = False
            parts = head[5:].split(";")  # отрезаем "data:"
            if parts:
                mime = parts[0] or mime
                is_base64 = any(p.lower() == "base64" for p in parts[1:])

            if not _is_allowed_media_mime(mime):
                return None, None

            if is_base64:
                raw = base64.b64decode(data_part, validate=False)
            else:
                # не base64 — обычно urlencoded
                raw = unquote(data_part).encode("utf-8")

            if len(raw) > _REMOTE_MAX_BYTES:
                return None, None

            fname, local_url = _save_bytes_to_uploads(raw, mime)
            return fname, local_url
        except Exception:
            return None, None

    def localize_external_media(html: str) -> str:
        """
        Находит в HTML теги <img>/<video>/<audio>/<source> с src:
          - data:...         -> сохраняет в /uploads, подменяет src
          - http/https:...   -> скачивает на сервер, подменяет src
        Уже локальные (/uploads/...) — не трогаем.
        Если BeautifulSoup недоступен — возвращаем исходный HTML.
        """
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return html or ""

        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")
        upload_url = app.config["UPLOAD_URL"].rstrip("/")

        def _process_tag(tag, attr="src"):
            val = tag.get(attr)
            if not val:
                return
            v = val.strip()

            # Уже локальный?
            if v.startswith(upload_url + "/") or v.startswith(url_for("uploaded_file", filename="").rstrip("/")):
                return

            # data:...
            if v.startswith("data:"):
                _fname, local_url = _data_uri_to_upload(v)
                if local_url:
                    tag[attr] = local_url
                return

            # http/https:...
            if v.startswith("http://") or v.startswith("https://"):
                _fname, local_url = _download_to_uploads(v)
                if local_url:
                    tag[attr] = local_url
                return

            # Остальные протоколы/форматы не трогаем (mailto:, blob:, file:, и т.п.)

        # img/src
        for img in soup.find_all("img"):
            _process_tag(img, "src")
            # при наличии srcset возьмём первый URL и тоже локализуем (упрощённо)
            srcset = img.get("srcset")
            if srcset and isinstance(srcset, str):
                first = srcset.split(",")[0].strip()
                url_only = first.split(" ")[0]
                if url_only.startswith(("http://", "https://", "data:")):
                    _fname, local_url = (None, None)
                    if url_only.startswith("data:"):
                        _fname, local_url = _data_uri_to_upload(url_only)
                    else:
                        _fname, local_url = _download_to_uploads(url_only)
                    if local_url:
                        img["src"] = local_url
                        img.attrs.pop("srcset", None)  # убираем srcset, чтобы не было конфликтов

        # video/audio/src
        for media in soup.find_all(["video", "audio"]):
            _process_tag(media, "src")
            for source in media.find_all("source"):
                _process_tag(source, "src")

        return str(soup)

    # --- Создание схемы и "тихие миграции" под файлоком ---
    with app.app_context():
        lock_path = "/data/.db_init.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            db.create_all()
            from sqlalchemy import text
            for ddl in [
                'CREATE TABLE IF NOT EXISTS favorites ('
                ' id INTEGER PRIMARY KEY,'
                ' user_login VARCHAR(64) NOT NULL,'
                ' article_id INTEGER NOT NULL,'
                ' created_at DATETIME,'
                ' UNIQUE(user_login, article_id)'
                ')',
                # --- статьи ---
                'ALTER TABLE articles ADD COLUMN updated_at DATETIME',
                'ALTER TABLE articles ADD COLUMN updated_by_login VARCHAR(64)',
                'ALTER TABLE articles ADD COLUMN is_deleted BOOLEAN DEFAULT 0',
                'ALTER TABLE articles ADD COLUMN deleted_at DATETIME',
                'ALTER TABLE articles ADD COLUMN deleted_by_login VARCHAR(64)',
            
                # --- ревизии статей ---
                'ALTER TABLE article_revisions ADD COLUMN attachments_json TEXT',
                'ALTER TABLE article_revisions ADD COLUMN created_at DATETIME',
            
                # --- разделы ---
                'ALTER TABLE sections ADD COLUMN updated_at DATETIME',
                'ALTER TABLE sections ADD COLUMN updated_by_login VARCHAR(64)',
                'ALTER TABLE sections ADD COLUMN is_deleted BOOLEAN DEFAULT 0',
                'ALTER TABLE sections ADD COLUMN deleted_at DATETIME',
                'ALTER TABLE sections ADD COLUMN deleted_by_login VARCHAR(64)',
            
                # --- подразделы ---
                'ALTER TABLE subsections ADD COLUMN updated_at DATETIME',
                'ALTER TABLE subsections ADD COLUMN updated_by_login VARCHAR(64)',
                'ALTER TABLE subsections ADD COLUMN is_deleted BOOLEAN DEFAULT 0',
                'ALTER TABLE subsections ADD COLUMN deleted_at DATETIME',
                'ALTER TABLE subsections ADD COLUMN deleted_by_login VARCHAR(64)',
            ]:
                try:
                    db.session.execute(text(ddl))
                except Exception:
                    pass
            db.session.commit()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # --- Кеш пользователей для логина ---
    app._users_cache = {}
    app._users_mtime = None

    def load_users():
        users_file = app.config["USERS_FILE"]
        try:
            mtime = os.path.getmtime(users_file)
        except OSError:
            mtime = None
        if mtime != app._users_mtime:
            app._users_cache = parse_users_file(users_file)
            app._users_mtime = mtime
        return app._users_cache

    # --- Jinja context ---
    @app.context_processor
    def inject_globals():
        try:
            sections_nav = Section.query.filter(Section.is_deleted.is_(False)).order_by(Section.title).all()
        except Exception:
            sections_nav = []
        return {
            "now": datetime.utcnow(),
            "current_user": current_user(),
            "nav_sections": sections_nav,
        }

    # --- Helpers ---
    def _snapshot_attachments(article: Article):
        data = []
        for att in article.attachments:
            data.append({
                "filename": att.filename,
                "mime_type": att.mime_type,
                "uploaded_by_login": att.uploaded_by_login,
                "uploaded_at": att.uploaded_at.isoformat() if att.uploaded_at else None,
            })
        return data

    def sync_attachments_from_content(article: Article, html: str):
        """Регистрируем в БД вложения, которые встречаются в тексте (src/href=<UPLOAD_URL>/...)."""
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return  # если bs4 не установлен
        soup = BeautifulSoup(html or "", "html.parser")
        files = set()
        upload_url = app.config["UPLOAD_URL"].rstrip("/")
        pattern = re.compile(rf"^{re.escape(upload_url)}/(.+)$")

        for tag in soup.find_all(["img", "video", "audio", "source", "a"]):
            src = tag.get("src") or tag.get("href")
            if not src:
                continue
            m = pattern.match(src.strip())
            if m:
                files.add(m.group(1))

        if not files:
            return

        existing = {att.filename for att in article.attachments}
        for fn in files - existing:
            db.session.add(Attachment(
                article_id=article.id,
                filename=fn,
                mime_type=mimetypes.guess_type(fn)[0] or "application/octet-stream",
                uploaded_by_login=session["user"]["login"],
            ))
        db.session.commit()

    # --- Static uploads serving ---
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    # --- Auth ---
    @app.route("/login", methods=["GET", "POST"])
    def login():
        load_users()
        if request.method == "POST":
            login_ = request.form.get("login", "").strip()
            password = request.form.get("password", "").strip()
            users = app._users_cache
            user = users.get(login_)
            if user and user["password"] == password:
                session["user"] = {
                    "login": login_,
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "is_admin": user.get("is_admin", False),
                }
                flash("Добро пожаловать!", "success")
                return redirect(request.args.get("next") or url_for("index"))
            flash("Неверный логин или пароль.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("user", None)
        flash("Вы вышли из системы.", "info")
        return redirect(url_for("login"))

    # --- Pages ---
    @app.route("/", methods=["GET"])
    @login_required
    def index():
        q = (request.args.get("q") or "").strip()
        user_login = session["user"]["login"]
        fav_ids = [f.article_id for f in Favorite.query.filter_by(user_login=user_login).all()]
        favorites = []
        if fav_ids:
            favorites = (Article.query
                 .filter(Article.id.in_(fav_ids), Article.is_deleted.is_(False))
                 .order_by(Article.updated_at.desc())
                 .all())
        
        if q:
            articles = (Article.query
                        .filter(
                            (Article.title.ilike(f"%{q}%")) | (Article.content.ilike(f"%{q}%")),
                            Article.is_deleted.is_(False))
                        .order_by(Article.updated_at.desc())
                        .all())
        else:
            articles = (Article.query
                        .filter(Article.is_deleted.is_(False))
                        .order_by(Article.updated_at.desc())
                        .limit(10)
                        .all())
        sections = Section.query.order_by(Section.title).all()
        return render_template("index.html", articles=articles, favorites=favorites, q=q)

    @app.route("/about")
    @login_required
    def about():
        return render_template("about.html")

    # --- Sections/Subsections ---
    @app.route("/sections/new", methods=["GET", "POST"])
    @login_required
    def new_section():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            desc = request.form.get("description", "")
            if not title:
                flash("Название раздела обязательно.", "error")
                return render_template("section_form.html")
            s = Section(title=title, description=desc, created_by_login=session["user"]["login"])
            db.session.add(s)
            db.session.commit()
            flash("Раздел создан.", "success")
            return redirect(url_for("section_detail", section_id=s.id))
        return render_template("section_form.html")

    @app.route("/subsections/<int:subsection_id>")
    @login_required
    def subsection_detail(subsection_id):
        ss = Subsection.query.filter(Subsection.id == subsection_id, Subsection.is_deleted.is_(False)).first_or_404()
        active_articles = Article.query.filter_by(subsection_id=ss.id, is_deleted=False)\
                                       .order_by(Article.updated_at.desc()).all()
        return render_template("subsection_detail.html", subsection=ss, articles_active=active_articles)

    @app.route("/sections/<int:section_id>/subsections/new", methods=["GET", "POST"])
    @login_required
    def new_subsection(section_id):
        s = Section.query.get_or_404(section_id)
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            desc = request.form.get("description", "")
            if not title:
                flash("Название подраздела обязательно.", "error")
                return render_template("subsection_form.html", section=s)
            ss = Subsection(section_id=s.id, title=title, description=desc, created_by_login=session["user"]["login"])
            db.session.add(ss)
            db.session.commit()
            flash("Подраздел создан.", "success")
            return redirect(url_for("subsection_detail", subsection_id=ss.id))
        return render_template("subsection_form.html", section=s)

    @app.route("/sections/<int:section_id>")
    @login_required
    def section_detail(section_id):
        s = Section.query.filter(Section.id == section_id, Section.is_deleted.is_(False)).first_or_404()
        # Если шаблон обходит section.subsections, он увидит и удалённые.
        # Поэтому лучше передать отдельный список активных:
        active_subsections = Subsection.query.filter_by(section_id=s.id, is_deleted=False)\
                                            .order_by(Subsection.title).all()
        return render_template("section_detail.html", section=s, subsections_active=active_subsections)

    # --- Edit Section ---
    @app.route("/sections/<int:section_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_section(section_id):
        s = Section.query.get_or_404(section_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            desc = request.form.get("description") or ""
            if not title:
                flash("Название раздела обязательно.", "error")
                return render_template("section_form.html", section=s)
            s.title = title
            s.description = desc
            s.updated_at = datetime.utcnow()
            s.updated_by_login = session["user"]["login"]
            db.session.commit()
            flash("Раздел обновлён.", "success")
            return redirect(url_for("section_detail", section_id=s.id))
        return render_template("section_form.html", section=s)


    # --- Edit Subsection ---
    @app.route("/subsections/<int:subsection_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_subsection(subsection_id):
        ss = Subsection.query.get_or_404(subsection_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            desc = request.form.get("description") or ""
            if not title:
                flash("Название подраздела обязательно.", "error")
                return render_template("subsection_form.html", section=ss.section, subsection=ss)
            ss.title = title
            ss.description = desc
            ss.updated_at = datetime.utcnow()
            ss.updated_by_login = session["user"]["login"]
            db.session.commit()
            flash("Подраздел обновлён.", "success")
            return redirect(url_for("subsection_detail", subsection_id=ss.id))
        return render_template("subsection_form.html", section=ss.section, subsection=ss)



    # --- Article CRUD ---
    @app.route("/subsections/<int:subsection_id>/articles/new", methods=["GET", "POST"])
    @login_required
    def new_article(subsection_id):
        ss = Subsection.query.get_or_404(subsection_id)
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = sanitize_html(request.form.get("content", ""))
            content = localize_external_media(content)
            if not title:
                flash("Название статьи обязательно.", "error")
                return render_template("article_form.html", subsection=ss, article=None)

            a_obj = Article(
                subsection_id=ss.id,
                title=title,
                content=content,
                created_by_login=session["user"]["login"],
                updated_by_login=session["user"]["login"],
            )
            db.session.add(a_obj)
            db.session.flush()

            # загрузка файлов (если были приложены через input[type=file])
            files = request.files.getlist("files")
            for file in files:
                if not file or not file.filename:
                    continue
                ext = os.path.splitext(file.filename)[1]
                unique = f"{uuid.uuid4().hex}{ext}"
                fname = secure_filename(unique)
                path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                file.save(path)
                db.session.add(Attachment(
                    article_id=a_obj.id, filename=fname, mime_type=file.mimetype,
                    uploaded_by_login=session["user"]["login"]
                ))
            db.session.commit()

            # синхронизация вложений, встречающихся внутри HTML
            sync_attachments_from_content(a_obj, a_obj.content)

            # первая ревизия (с снапшотом вложений)
            rev = ArticleRevision(
                article_id=a_obj.id,
                content=a_obj.content,
                editor_login=session["user"]["login"],
                attachments_json=json.dumps(_snapshot_attachments(a_obj), ensure_ascii=False),
            )
            db.session.add(rev)
            db.session.commit()

            # RAG
            rebuild_article_chunks(a_obj)

            flash("Статья создана.", "success")
            return redirect(url_for("article_detail", article_id=a_obj.id))
        return render_template("article_form.html", subsection=ss, article=None)

    @app.route("/articles/<int:article_id>")
    @login_required
    def article_detail(article_id):
        a_obj = Article.query.get_or_404(article_id)
        is_fav = Favorite.query.filter_by(
            article_id=a_obj.id,
            user_login=session["user"]["login"]
        ).first() is not None
        return render_template("article_detail.html", article=a_obj, is_fav=is_fav)

    @app.route("/articles/<int:article_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_article(article_id):
        a_obj = Article.query.get_or_404(article_id)
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = sanitize_html(request.form.get("content", ""))
            content = localize_external_media(content)
            if not title:
                flash("Название статьи обязательно.", "error")
                return render_template("article_form.html", subsection=a_obj.subsection, article=a_obj)

            # ревизия текущего состояния (контент + вложения)
            prev = ArticleRevision(
                article_id=a_obj.id,
                content=a_obj.content,
                editor_login=session["user"]["login"],
                attachments_json=json.dumps(_snapshot_attachments(a_obj), ensure_ascii=False),
            )
            db.session.add(prev)

            a_obj.title = title
            a_obj.content = content
            a_obj.updated_by_login = session["user"]["login"]
            a_obj.updated_at = datetime.utcnow()
            db.session.commit()

            # загрузка дополнительных файлов
            files = request.files.getlist("files")
            for file in files:
                if not file or not file.filename:
                    continue
                ext = os.path.splitext(file.filename)[1]
                unique = f"{uuid.uuid4().hex}{ext}"
                fname = secure_filename(unique)
                path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                file.save(path)
                db.session.add(Attachment(
                    article_id=a_obj.id, filename=fname, mime_type=file.mimetype,
                    uploaded_by_login=session["user"]["login"]
                ))
            db.session.commit()

            # синхронизация вложений, встречающихся в HTML
            sync_attachments_from_content(a_obj, a_obj.content)

            # RAG
            rebuild_article_chunks(a_obj)

            flash("Статья обновлена.", "success")
            return redirect(url_for("article_detail", article_id=a_obj.id))
        return render_template("article_form.html", subsection=a_obj.subsection, article=a_obj)

    @app.route("/articles/<int:article_id>/history")
    @login_required
    def article_history(article_id):
        a = Article.query.get_or_404(article_id)
        return render_template("history.html", article=a, revisions=a.revisions)

    @app.route("/articles/<int:article_id>/rollback/<int:rev_id>", methods=["POST"])
    @login_required
    def rollback_article(article_id, rev_id):
        a_obj = Article.query.get_or_404(article_id)
        rev = ArticleRevision.query.get_or_404(rev_id)

        # ревизия текущего состояния (контент + вложения)
        prev = ArticleRevision(
            article_id=a_obj.id,
            content=a_obj.content,
            editor_login=session["user"]["login"],
            attachments_json=json.dumps(_snapshot_attachments(a_obj), ensure_ascii=False),
        )
        db.session.add(prev)
        db.session.flush()

        # откат контента
        a_obj.content = rev.content
        a_obj.updated_by_login = session["user"]["login"]
        a_obj.updated_at = datetime.utcnow()

        # откат вложений по снапшоту
        target = []
        if rev.attachments_json:
            try:
                target = json.loads(rev.attachments_json)
            except Exception:
                target = []

        # удаляем текущие привязки и восстанавливаем из снапшота
        Attachment.query.filter_by(article_id=a_obj.id).delete()
        db.session.flush()

        missing = 0
        for item in target:
            fname = item.get("filename")
            mtype = item.get("mime_type")
            upl_by = item.get("uploaded_by_login") or session["user"]["login"]
            upl_at = datetime.utcnow()
            if item.get("uploaded_at"):
                try:
                    upl_at = datetime.fromisoformat(item["uploaded_at"])
                except Exception:
                    pass
            fpath = os.path.join(app.config["UPLOAD_FOLDER"], fname) if fname else None
            if not fname or not os.path.exists(fpath):
                missing += 1
                continue
            db.session.add(Attachment(
                article_id=a_obj.id,
                filename=fname,
                mime_type=mtype,
                uploaded_by_login=upl_by,
                uploaded_at=upl_at,
            ))

        db.session.commit()

        # RAG
        rebuild_article_chunks(a_obj)

        if missing:
            flash(f"Откат выполнен, но {missing} вложений отсутствуют на диске и не были восстановлены.", "warning")
        else:
            flash("Откат выполнен (контент и вложения).", "success")
        return redirect(url_for("article_detail", article_id=a_obj.id))


    # ---------- Article soft delete ----------
    @app.route("/articles/<int:article_id>/delete", methods=["POST"])
    @login_required
    def delete_article(article_id):
        a_obj = Article.query.get_or_404(article_id)
        if a_obj.is_deleted:
            flash("Статья уже в корзине.", "info")
            return redirect(url_for("article_detail", article_id=article_id))
        a_obj.is_deleted = True
        a_obj.deleted_at = datetime.utcnow()
        a_obj.deleted_by_login = session["user"]["login"]
        db.session.commit()
        flash("Статья перемещена в корзину.", "success")
        return redirect(url_for("index"))


    @app.route("/articles/<int:article_id>/restore", methods=["POST"])
    @admin_required
    def restore_article(article_id):
        a_obj = Article.query.get_or_404(article_id)
        if not a_obj.is_deleted:
            flash("Статья не в корзине.", "info")
            return redirect(url_for("article_detail", article_id=article_id))
        a_obj.is_deleted = False
        a_obj.deleted_at = None
        a_obj.deleted_by_login = None
        db.session.commit()
        flash("Статья восстановлена.", "success")
        return redirect(url_for("article_detail", article_id=article_id))


    @app.route("/articles/<int:article_id>/purge", methods=["POST"])
    @admin_required
    def purge_article(article_id):
        a_obj = Article.query.get_or_404(article_id)
        if not a_obj.is_deleted:
            flash("Для окончательного удаления сначала поместите статью в корзину.", "warning")
            return redirect(url_for("article_detail", article_id=article_id))
    
        # удалить файлы с диска (best-effort)
        removed = 0
        for att in list(a_obj.attachments):
            try:
                fpath = os.path.join(app.config["UPLOAD_FOLDER"], att.filename)
                if os.path.exists(fpath):
                    os.remove(fpath)
                    removed += 1
            except Exception:
                pass

        db.session.delete(a_obj)  # каскадно удалит attachments/revisions/chunks
        db.session.commit()
        flash(f"Статья и вложения удалены окончательно (файлов удалено: {removed}).", "success")
        return redirect(url_for("admin_trash"))


    @app.route("/articles/<int:article_id>/favorite", methods=["POST"])
    @login_required
    def toggle_favorite(article_id):
        a_obj = Article.query.get_or_404(article_id)
        user_login = session["user"]["login"]
    
        fav = Favorite.query.filter_by(article_id=a_obj.id, user_login=user_login).first()
        if fav:
            db.session.delete(fav)
            db.session.commit()
            flash("Статья убрана из избранного.", "info")
        else:
            db.session.add(Favorite(article_id=a_obj.id, user_login=user_login))
            db.session.commit()
            flash("Статья добавлена в избранное.", "success")
    
        # Возвращаемся туда, откуда пришли
        return redirect(request.referrer or url_for("article_detail", article_id=a_obj.id))

    # ---------- Soft delete helpers ----------

    def _soft_delete_article(a_obj, login):
        if a_obj.is_deleted:
            return
        a_obj.is_deleted = True
        a_obj.deleted_at = datetime.utcnow()
        a_obj.deleted_by_login = login

    def _restore_article(a_obj):
        if not a_obj.is_deleted:
            return
        a_obj.is_deleted = False
        a_obj.deleted_at = None
        a_obj.deleted_by_login = None

    def _purge_article(a_obj):
        # снять файлы с диска и удалить объект
        removed = 0
        for att in list(a_obj.attachments):
            try:
                fpath = os.path.join(app.config["UPLOAD_FOLDER"], att.filename)
                if os.path.exists(fpath):
                    os.remove(fpath)
                    removed += 1
            except Exception:
                pass
        db.session.delete(a_obj)
        return removed

    def _soft_delete_subsection(ss, login):
        if ss.is_deleted:
            return
        ss.is_deleted = True
        ss.deleted_at = datetime.utcnow()
        ss.deleted_by_login = login
        # все статьи в подразделе
        for a in Article.query.filter_by(subsection_id=ss.id).all():
            _soft_delete_article(a, login)

    def _restore_subsection(ss):
        if not ss.is_deleted:
            pass
        ss.is_deleted = False
        ss.deleted_at = None
        ss.deleted_by_login = None
        for a in Article.query.filter_by(subsection_id=ss.id).all():
            _restore_article(a)

    def _purge_subsection(ss):
        total_removed = 0
        for a in Article.query.filter_by(subsection_id=ss.id).all():
            total_removed += _purge_article(a)
        db.session.delete(ss)
        return total_removed

    def _soft_delete_section(s, login):
        if s.is_deleted:
            return
        s.is_deleted = True
        s.deleted_at = datetime.utcnow()
        s.deleted_by_login = login
        # все подразделы + их статьи
        for ss in Subsection.query.filter_by(section_id=s.id).all():
            _soft_delete_subsection(ss, login)

    def _restore_section(s):
        if not s.is_deleted:
            pass
        s.is_deleted = False
        s.deleted_at = None
        s.deleted_by_login = None
        for ss in Subsection.query.filter_by(section_id=s.id).all():
            _restore_subsection(ss)

    def _purge_section(s):
        total_removed = 0
        for ss in Subsection.query.filter_by(section_id=s.id).all():
            total_removed += _purge_subsection(ss)
        db.session.delete(s)
        return total_removed

    # ---------- Subsection soft delete ----------
    @app.route("/subsections/<int:subsection_id>/delete", methods=["POST"])
    @login_required
    def delete_subsection(subsection_id):
        ss = Subsection.query.get_or_404(subsection_id)
        if ss.is_deleted:
            flash("Подраздел уже в корзине.", "info")
            return redirect(url_for("subsection_detail", subsection_id=subsection_id))
        _soft_delete_subsection(ss, session["user"]["login"])
        db.session.commit()
        flash("Подраздел и его статьи перемещены в корзину.", "success")
        return redirect(url_for("section_detail", section_id=ss.section_id))

    @app.route("/subsections/<int:subsection_id>/restore", methods=["POST"])
    @admin_required
    def restore_subsection(subsection_id):
        ss = Subsection.query.get_or_404(subsection_id)
        if not ss.is_deleted:
            flash("Подраздел не в корзине.", "info")
            return redirect(url_for("subsection_detail", subsection_id=subsection_id))
        _restore_subsection(ss)
        db.session.commit()
        flash("Подраздел и его статьи восстановлены.", "success")
        return redirect(url_for("subsection_detail", subsection_id=subsection_id))

    @app.route("/subsections/<int:subsection_id>/purge", methods=["POST"])
    @admin_required
    def purge_subsection(subsection_id):
        ss = Subsection.query.get_or_404(subsection_id)
        if not ss.is_deleted:
            flash("Для окончательного удаления сначала поместите подраздел в корзину.", "warning")
            return redirect(url_for("subsection_detail", subsection_id=subsection_id))
        removed = _purge_subsection(ss)
        db.session.commit()
        flash(f"Подраздел удалён окончательно. Файлов удалено: {removed}.", "success")
        return redirect(url_for("admin_trash"))

    @app.route("/admin/trash")
    @admin_required
    def admin_trash():
        deleted_articles = Article.query.filter_by(is_deleted=True)\
                                        .order_by(Article.deleted_at.desc()).all()
        deleted_subsections = Subsection.query.filter_by(is_deleted=True)\
                                              .order_by(Subsection.deleted_at.desc()).all()
        deleted_sections = Section.query.filter_by(is_deleted=True)\
                                        .order_by(Section.deleted_at.desc()).all()
        return render_template(
            "trash.html",
            articles=deleted_articles,
            subsections=deleted_subsections,
            sections=deleted_sections,
        )


    # ---------- Section soft delete ----------
    @app.route("/sections/<int:section_id>/delete", methods=["POST"])
    @login_required
    def delete_section(section_id):
        s = Section.query.get_or_404(section_id)
        if s.is_deleted:
            flash("Раздел уже в корзине.", "info")
            return redirect(url_for("section_detail", section_id=section_id))
        _soft_delete_section(s, session["user"]["login"])
        db.session.commit()
        flash("Раздел со всеми подразделами и статьями перемещён в корзину.", "success")
        return redirect(url_for("index"))

    @app.route("/sections/<int:section_id>/restore", methods=["POST"])
    @admin_required
    def restore_section(section_id):
        s = Section.query.get_or_404(section_id)
        if not s.is_deleted:
            flash("Раздел не в корзине.", "info")
            return redirect(url_for("section_detail", section_id=section_id))
        _restore_section(s)
        db.session.commit()
        flash("Раздел со всеми потомками восстановлен.", "success")
        return redirect(url_for("section_detail", section_id=section_id))

    @app.route("/sections/<int:section_id>/purge", methods=["POST"])
    @admin_required
    def purge_section(section_id):
        s = Section.query.get_or_404(section_id)
        if not s.is_deleted:
            flash("Для окончательного удаления сначала поместите раздел в корзину.", "warning")
            return redirect(url_for("section_detail", section_id=section_id))
        removed = _purge_section(s)
        db.session.commit()
        flash(f"Раздел и все потомки удалены окончательно. Файлов удалено: {removed}.", "success")
        return redirect(url_for("admin_trash"))


    # --- Upload endpoint for editor (вставка в HTML через <img>/<video>/<audio>) ---
    @app.route("/upload", methods=["POST"])
    @login_required
    def upload_media():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "no file"}), 400
    
        ctype = (file.mimetype or "").lower()
        is_media = ctype.startswith(("image/", "video/", "audio/"))
        if not is_media:
            guessed = (mimetypes.guess_type(file.filename)[0] or "").lower()
            if guessed:
                ctype = ctype or guessed
                is_media = guessed.startswith(("image/", "video/", "audio/"))
        if not is_media:
            return jsonify({"error": "unsupported type"}), 415
    
        # подставим расширение, если его нет
        ext = os.path.splitext(file.filename)[1].lower()
        if not ext:
            mapping = {
                "image/jpeg": ".jpeg",
                "image/jpg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/svg+xml": ".svg",
                "image/bmp": ".bmp",
                "image/heic": ".heic",
                "video/mp4": ".mp4",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
            }
            ext = mapping.get(ctype, mimetypes.guess_extension(ctype) or "")
    
        fname = secure_filename(f"{uuid.uuid4().hex}{ext}")
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        file.save(path)
    
        a_id = request.form.get("article_id")
        if a_id:
            try:
                db.session.add(Attachment(
                    article_id=int(a_id),
                    filename=fname,
                    mime_type=ctype or file.mimetype,
                    uploaded_by_login=session["user"]["login"],
                ))  
                db.session.commit()
            except Exception:
                db.session.rollback()
    
        url = url_for("uploaded_file", filename=fname)
        return jsonify({"location": url, "filename": fname, "mime": ctype or file.mimetype})

    # --- RAG-friendly export APIs ---
    def _chunk_to_dict(ch: Chunk):
        a = ch.article
        ss = a.subsection
        s = ss.section if ss else None
        return {
            "chunk_id": ch.id,
            "chunk_index": ch.idx,
            "text": ch.text,
            "tokens": ch.tokens,
            "article_id": a.id,
            "article_title": a.title,
            "subsection_id": ss.id if ss else None,
            "subsection_title": ss.title if ss else None,
            "section_id": s.id if s else None,
            "section_title": s.title if s else None,
            "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "last_editor": a.updated_by_login or a.created_by_login,
            "path": f"/sections/{s.id if s else ''}/subsections/{ss.id if ss else ''}/articles/{a.id}",
        }

    @app.route("/api/chunks.ndjson")
    @login_required
    def api_chunks_ndjson():
        def generate():
            q = (Chunk.query
                 .join(Article, Article.id == Chunk.article_id)
                 .filter(Article.is_deleted.is_(False))
                 .order_by(Chunk.id)
                 .yield_per(200))
            for ch in q:
                yield json.dumps(_chunk_to_dict(ch), ensure_ascii=False) + "\n"
        return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

    @app.route("/api/chunks.json")
    @login_required
    def api_chunks_json():
        chunks = [
            _chunk_to_dict(ch) for ch in
            Chunk.query.join(Article, Article.id == Chunk.article_id)
            .filter(Article.is_deleted.is_(False))
            .order_by(Chunk.id).limit(5000).all()
        ]
        return jsonify(chunks)

    @app.route("/api/articles.json")
    @login_required
    def api_articles_json():
        data = []
        for a in Article.query.filter(Article.is_deleted.is_(False)).order_by(Article.id).all():
            ss = a.subsection
            s = ss.section if ss else None
            try:
                from bs4 import BeautifulSoup
                text = BeautifulSoup(a.content or "", "html.parser").get_text("\n")
            except Exception:
                text = ""
            data.append({
                "article_id": a.id,
                "article_title": a.title,
                "html": a.content,
                "text": text,
                "section_title": s.title if s else None,
                "subsection_title": ss.title if ss else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                "author": a.created_by_login,
                "last_editor": a.updated_by_login or a.created_by_login,
            })
        return jsonify(data)

    @app.route("/admin/rag/rebuild", methods=["POST"])
    @admin_required
    def admin_rag_rebuild():
        rebuild_all_chunks()
        flash("RAG-чанки пересобраны для всех статей.", "success")
        return redirect(url_for("index"))

    return app


# For gunicorn entrypoint
app = create_app()
