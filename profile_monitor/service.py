import time
from collections import defaultdict, deque
import uuid
from html import escape

from .models import compare_snapshots
from .style import icon


FIELD_NAMES = {
    "username": "Username", "first_name": "Имя", "last_name": "Фамилия",
    "bio": "Bio", "photo_id": "Аватарка",
}


class ProfileMonitorService:
    def __init__(self, repository, telegram, notifier, access_check=None):
        self.repo = repository
        self.telegram = telegram
        self.notifier = notifier
        self.access_check = access_check
        self.manual_requests=defaultdict(deque)

    def manual_budget(self, owner):
        now=time.monotonic()
        for key,limit in ((owner,10),('session',30)):
            bucket=self.manual_requests[key]
            while bucket and bucket[0]<now-60:bucket.popleft()
            if len(bucket)>=limit:raise ValueError('Слишком много ручных проверок. Подождите минуту.')
        self.manual_requests[owner].append(now);self.manual_requests['session'].append(now)

    def require_access(self, owner_id):
        if self.access_check is not None and not self.access_check(owner_id):
            raise PermissionError("Мониторинг профилей доступен только выбранным пользователям")

    async def watch(self, owner_id, target):
        self.require_access(owner_id)
        self.manual_budget(owner_id)
        if len(self.repo.list(owner_id))>=100 and not self.repo.find(owner_id,target):
            raise ValueError("Доступно до 100 отслеживаемых профилей; сначала отключите лишние.")
        target = target.strip()
        known = self.repo.find(owner_id, target)
        if known and known["enabled"]:
            if target.isdigit() and known["access_hash"] is None:
                # An old ID-only row can be repaired after the session account
                # has opened a dialog with this user, without resetting history.
                snapshot, entity = await self.telegram.fetch(int(target))
                if snapshot.user_id == known["user_id"]:
                    self.repo.set_access_hash(
                        owner_id, snapshot.user_id, getattr(entity, "access_hash", None),
                        snapshot.username,
                    )
            return self.repo.snapshot(self.repo.get(owner_id, known["user_id"])), False
        if target.startswith("@"):
            target = target[1:]
        elif target.isdigit():
            target = int(target)
        snapshot, entity = await self.telegram.fetch(target)
        self.require_access(owner_id)
        existing = self.repo.get(owner_id, snapshot.user_id)
        if existing and existing["enabled"]:
            self.repo.set_access_hash(
                owner_id, snapshot.user_id, getattr(entity, "access_hash", None),
                snapshot.username,
            )
            return self.repo.snapshot(self.repo.get(owner_id, snapshot.user_id)), False
        photo_path = None
        settings = self.repo.settings(owner_id)
        if snapshot.photo_id and settings["keep_photos"]:
            photo_path = await self.telegram.download_photo(entity, snapshot.user_id, snapshot.photo_id)
        self.require_access(owner_id)
        self.repo.add(owner_id, snapshot, photo_path, getattr(entity, "access_hash", None))
        return snapshot, True

    def unwatch(self, owner_id, target):
        row = self.repo.find(owner_id, target)
        if not row:
            return False
        self.repo.disable(owner_id, row["user_id"])
        return True

    async def check_row(self, row, notify=True):
        self.require_access(row["owner_id"])
        old = self.repo.snapshot(row)
        snapshot, entity = await self.telegram.fetch(
            row["user_id"], access_hash=row["access_hash"], username=row["username"],
            allow_id_search=False,
        )
        self.require_access(row["owner_id"])
        changes = compare_snapshots(old, snapshot)
        photo_path = None
        if old.photo_id != snapshot.photo_id and snapshot.photo_id:
            settings = self.repo.settings(row["owner_id"])
            if settings["keep_photos"]:
                photo_path = await self.telegram.download_photo(entity, snapshot.user_id, snapshot.photo_id)
        self.require_access(row["owner_id"])
        batch_id = uuid.uuid4().hex
        self.repo.save_check(
            row["owner_id"], old, snapshot, changes, batch_id, photo_path,
            access_hash=getattr(entity, "access_hash", None),
        )
        if changes and notify and self.repo.settings(row["owner_id"])["notifications"]:
            format_name = self.repo.settings(row["owner_id"])["notification_format"]
            await self.notifier(row["owner_id"], old, snapshot, changes, format_name)
        return changes

    async def check(self, owner_id, user_id, notify=True):
        self.require_access(owner_id)
        self.manual_budget(owner_id)
        row = self.repo.get(owner_id, user_id)
        if not row or not row["enabled"]:
            raise LookupError("Пользователь не отслеживается")
        try:
            return await self.check_row(row, notify)
        except Exception as exc:
            self.repo.set_error(owner_id, user_id, exc)
            raise


def display_value(field, value):
    if field == "photo_id":
        return "есть" if value else "нет"
    if value is None or value == "":
        return "—"
    if field == "username":
        return "@" + value
    text = str(value).replace("\n", " ")
    return text[:250] + ("…" if len(text) > 250 else "")


def notification_text(old, new, changes, format_name="compact"):
    old_handle = "@" + old.username if old.username else (old.first_name or str(old.user_id))
    lines = [f"{icon('changed')} {escape(old_handle)} изменил профиль"]
    for field, before, after in changes:
        if field == "photo_id":
            action = "удалена" if not after else "изменена" if before else "добавлена"
            action_icon = icon("avatar_removed" if not after else "avatar_added")
            lines.append(f"{icon('avatar')} Аватарка: {action_icon} {action.capitalize()}")
        else:
            before_text = escape(display_value(field, before))
            after_text = escape(display_value(field, after))
            field_icon = icon({"username": "username", "first_name": "name", "last_name": "name", "bio": "bio"}[field])
            if format_name == "detailed":
                lines.extend((f"{field_icon} {FIELD_NAMES[field]}:", f"«{before_text}»", "↓", f"«{after_text}»"))
            else:
                lines.append(f"{field_icon} {FIELD_NAMES[field]}: {before_text} → {after_text}")
    return "\n".join(lines)
