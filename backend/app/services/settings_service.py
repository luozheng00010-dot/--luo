"""设置服务：settings 表的读写。密钥相关值只存引用名，不存明文。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

# 包含这些子串的设置键被视为密钥引用，读写时做特殊处理
SECRET_KEYS = {"vision_api_key_ref", "text_api_key_ref", "tts_api_key_ref", "api_key_ref"}


def get_setting(session: Session, key: str, default=None):
    row = session.get(Setting, key)
    if row is None:
        return default
    return row.value_json


def set_setting(session: Session, key: str, value) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value_json=value)
        session.add(row)
    else:
        row.value_json = value
    session.commit()


def all_settings(session: Session) -> dict:
    rows = session.execute(select(Setting)).scalars().all()
    return {row.key: row.value_json for row in rows}
