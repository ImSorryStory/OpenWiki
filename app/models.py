from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime


db = SQLAlchemy()


class Section(db.Model):
    __tablename__ = "sections"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_login = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_login = db.Column(db.String(64))

    # soft-delete
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime)
    deleted_by_login = db.Column(db.String(64))

    subsections = db.relationship(
        "Subsection",
        backref="section",
        cascade="all, delete-orphan",
        order_by="Subsection.title",
    )

    def __repr__(self):
        return f"<Section {self.id} {self.title!r}>"


class Subsection(db.Model):
    __tablename__ = "subsections"

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_login = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_login = db.Column(db.String(64))

    # soft-delete
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime)
    deleted_by_login = db.Column(db.String(64))

    articles = db.relationship(
        "Article",
        backref="subsection",
        cascade="all, delete-orphan",
        order_by="Article.updated_at.desc()",
    )

    def __repr__(self):
        return f"<Subsection {self.id} {self.title!r}>"


class Article(db.Model):
    __tablename__ = "articles"

    id = db.Column(db.Integer, primary_key=True)
    subsection_id = db.Column(db.Integer, db.ForeignKey("subsections.id"), nullable=False)

    title = db.Column(db.String(300), nullable=False)
    content = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_login = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_login = db.Column(db.String(64))

    # soft-delete
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime)
    deleted_by_login = db.Column(db.String(64))

    attachments = db.relationship(
        "Attachment",
        backref="article",
        cascade="all, delete-orphan",
        order_by="Attachment.uploaded_at",
    )
    revisions = db.relationship(
        "ArticleRevision",
        backref="article",
        cascade="all, delete-orphan",
        order_by="ArticleRevision.created_at.desc()",
    )
    chunks = db.relationship(
        "Chunk",
        backref="article",
        cascade="all, delete-orphan",
        order_by="Chunk.idx",
    )
    favorites = db.relationship(
        "Favorite",
        back_populates="article",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )   

    def __repr__(self):
        return f"<Article {self.id} {self.title!r}>"


class Attachment(db.Model):
    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False)

    filename = db.Column(db.String(400), nullable=False)
    mime_type = db.Column(db.String(100))

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_login = db.Column(db.String(64))


class ArticleRevision(db.Model):
    __tablename__ = "article_revisions"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False)

    content = db.Column(db.Text, default="")
    editor_login = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # снимок вложений на момент ревизии
    attachments_json = db.Column(db.Text)


class Chunk(db.Model):
    __tablename__ = "chunks"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False)
    idx = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, default="")
    tokens = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class Favorite(db.Model):
    __tablename__ = "favorites"

    id = db.Column(db.Integer, primary_key=True)
    user_login = db.Column(db.String(64), nullable=False, index=True)
    article_id = db.Column(
        db.Integer,
        db.ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # связь к статье
    article = db.relationship(
        "Article",
        back_populates="favorites",
    )

    __table_args__ = (
        db.UniqueConstraint("user_login", "article_id", name="uq_favorite_user_article"),
    )
