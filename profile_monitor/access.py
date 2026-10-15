"""Explicit access to the operator's MTProto account, separate from /grant."""


class MTProtoAccess:
    def __init__(self, routes, owner_ids, extra_ids=""):
        self.routes = routes
        try:
            extra = {int(value) for value in extra_ids.replace(",", " ").split()}
        except ValueError as exc:
            raise ValueError("MTPROTO_ALLOWED_USER_IDS: нужны Telegram ID через запятую") from exc
        if any(value <= 0 for value in extra):
            raise ValueError("MTPROTO_ALLOWED_USER_IDS: ID должны быть положительными")
        self.operator_ids = set(owner_ids)
        self.allowed_ids = set(owner_ids) | extra

    def actor(self, actor_id):
        return (actor_id in self.allowed_ids and actor_id in self.routes
                and self.owner(self.routes[actor_id]))

    def owner(self, owner_id):
        return (owner_id in self.allowed_ids and owner_id in self.routes.values()
                and (not hasattr(self, "collection_check") or self.collection_check(owner_id)))
