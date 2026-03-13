from abc import ABC, abstractmethod

class DealSource(ABC):
    @abstractmethod
    def get_stores(self) -> list[dict]:
        ...

    @abstractmethod
    def get_deals_by_store(self, store_id: str) -> list[dict]:
        ...

    @abstractmethod
    def get_store(self, store_id: str) -> dict:
        ...