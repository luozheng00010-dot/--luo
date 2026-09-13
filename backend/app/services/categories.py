"""品类管理（计划 4.6.3 / P2-09）。

- 三个预置品类幂等初始化，带稳定系统标识（system_code），不能删除或停用；
- 自定义品类：统一规范化键去重（name_key 唯一），重命名保持 ID，停用只禁止新上传；
- “通用”的补充镜头语义按 system_code == "general" 判断，不按名称模糊判断。
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Category

BUILTIN_CATEGORIES: list[tuple[str, str]] = [
    ("通用", "general"),
    ("文胸", "bra"),
    ("内裤", "panties"),
]

MAX_NAME_LEN = 30


def normalize_name_key(name: str) -> str:
    """统一规范化键：NFC + 去首尾空格 + 内部空白折叠 + casefold。前后端共用此规则。"""
    normalized = unicodedata.normalize("NFC", name).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.casefold()


def normalize_name(name: str) -> str:
    """规范显示名：NFC + 去首尾空格 + 内部空白折叠（保留大小写）。"""
    normalized = unicodedata.normalize("NFC", name).strip()
    return re.sub(r"\s+", " ", normalized)


def init_builtin_categories(session: Session) -> None:
    """幂等初始化：重复启动不重复创建（T29 验收点）。"""
    for name, system_code in BUILTIN_CATEGORIES:
        existing = session.execute(
            select(Category).where(Category.system_code == system_code)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Category(
                    name=name,
                    name_key=normalize_name_key(name),
                    system_code=system_code,
                    is_builtin=True,
                    is_active=True,
                )
            )
    session.commit()


def list_categories(session: Session, *, include_inactive: bool = False) -> list[Category]:
    stmt = select(Category).order_by(Category.is_builtin.desc(), Category.name)
    if not include_inactive:
        stmt = stmt.where(Category.is_active.is_(True))
    return list(session.execute(stmt).scalars().all())


def get_category(session: Session, category_id: str) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise NotFoundError("品类不存在")
    return category


def get_by_system_code(session: Session, system_code: str) -> Category | None:
    return session.execute(
        select(Category).where(Category.system_code == system_code)
    ).scalar_one_or_none()


def create_category(session: Session, name: str) -> Category:
    display = normalize_name(name)
    if not display:
        raise ValidationError("品类名称不能为空")
    if len(display) > MAX_NAME_LEN:
        raise ValidationError(f"品类名称不能超过 {MAX_NAME_LEN} 个字符")
    name_key = normalize_name_key(name)
    dup = session.execute(select(Category).where(Category.name_key == name_key)).scalar_one_or_none()
    if dup is not None:
        raise ConflictError(f"品类「{dup.name}」已存在")
    category = Category(name=display, name_key=name_key, is_builtin=False, is_active=True)
    session.add(category)
    session.commit()
    return category


def rename_category(session: Session, category_id: str, new_name: str) -> Category:
    category = get_category(session, category_id)
    if category.is_builtin:
        raise ValidationError("预置品类不可重命名")
    display = normalize_name(new_name)
    if not display:
        raise ValidationError("品类名称不能为空")
    if len(display) > MAX_NAME_LEN:
        raise ValidationError(f"品类名称不能超过 {MAX_NAME_LEN} 个字符")
    name_key = normalize_name_key(new_name)
    dup = session.execute(select(Category).where(Category.name_key == name_key)).scalar_one_or_none()
    if dup is not None and dup.id != category.id:
        raise ConflictError(f"品类「{dup.name}」已存在")
    category.name = display
    category.name_key = name_key  # ID 不变，素材关联继续有效（T31）
    session.commit()
    return category


def set_active(session: Session, category_id: str, is_active: bool) -> Category:
    category = get_category(session, category_id)
    if category.is_builtin and not is_active:
        raise ValidationError("预置品类不可停用")
    category.is_active = is_active  # 已有素材不失联，只是新上传不可选（4.6.3）
    session.commit()
    return category


def validate_for_upload(session: Session, category_id: str | None) -> Category:
    """新上传的品类校验：必须存在且启用（4.6.1：不存在或已停用 ID 不可用于新上传）。"""
    if not category_id:
        raise ValidationError("必须选择品类")
    category = get_category(session, category_id)
    if not category.is_active:
        raise ValidationError(f"品类「{category.name}」已停用，不可用于新上传")
    return category


def validate_sku(sku: str | None) -> str:
    """货号校验（计划 4.6.1）：去除首尾空格后 1~64 字符；不转数字、保留原样。"""
    if sku is None:
        raise ValidationError("货号必填")
    cleaned = sku.strip()
    if not cleaned:
        raise ValidationError("货号必填")
    if len(cleaned) > 64:
        raise ValidationError("货号不能超过 64 个字符")
    return cleaned
