"""岗位权限与联系方式脱敏。

角色：
* dispatcher 调度员/网格员：登记报修、补充、派工、合并、转交、暂停；可见完整联系方式；
* crew       维修班组（绑定 crew_id）：上报进度、提交验收；仅见本人工单的脱敏联系方式；
* admin      管理员：负荷看板、超时与验收意见；仅见脱敏联系方式；
* anonymous  未识别身份：隐藏联系方式。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ROLES = ("dispatcher", "crew", "admin", "anonymous")


@dataclass(frozen=True)
class Identity:
    user_id: str
    role: str = "anonymous"
    crew_id: str | None = None
    name: str = ""

    @classmethod
    def from_headers(cls, headers: dict[str, str] | None) -> "Identity":
        headers = headers or {}
        role = headers.get("X-User-Role", "anonymous").strip().lower()
        if role not in ROLES:
            role = "anonymous"
        crew_id = headers.get("X-Crew-Id") or None
        return Identity(
            user_id=headers.get("X-User-Id", "anonymous"),
            role=role,
            crew_id=crew_id,
            name=headers.get("X-User-Name", ""),
        )


def mask_phone(value: str) -> str:
    digits = value.replace("-", "").replace(" ", "")
    if len(digits) >= 7:
        return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]
    if len(digits) <= 2:
        return "*" * len(digits)
    return digits[0] + "*" * (len(digits) - 1)


def mask_email(value: str) -> str:
    if "@" not in value:
        return value
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[:2] + "*" * (len(name) - 2)
    return f"{masked}@{domain}"


def mask_value(value: str | None) -> str:
    """脱敏为仅可辨认首尾的形式，如 138****5678 / 张**。"""
    if not value:
        return ""
    value = value.strip()
    if re.search(r"@", value):
        return mask_email(value)
    if re.search(r"[\d-]{6,}", value):
        return mask_phone(value)
    if len(value) == 1:
        return value
    if len(value) == 2:
        return value[0] + "*"
    return value[0] + "*" * (len(value) - 2) + value[-1]


def contact_visibility(identity: Identity, ticket_crew_id: str | None) -> str:
    """返回该身份对某工单联系方式的可见级别：full / masked / hidden。"""
    if identity.role == "dispatcher":
        return "full"
    if identity.role == "admin":
        return "masked"
    if identity.role == "crew":
        if ticket_crew_id and identity.crew_id == ticket_crew_id:
            return "masked"
        return "hidden"
    return "hidden"


def render_contact(identity: Identity, ticket_crew_id: str | None, contact: str) -> str | None:
    level = contact_visibility(identity, ticket_crew_id)
    if level == "hidden":
        return None
    if level == "masked":
        return mask_value(contact)
    return contact
