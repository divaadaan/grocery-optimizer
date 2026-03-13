from app.providers.base import DealSource

class Provider(DealSource):
    def get_stores(self) -> list[dict]:
        ...

    def get_deals_by_store(self, store_id: str) -> list[dict]:
        ...

    def get_store(self, store_id: str) -> dict:
        ...