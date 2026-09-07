from typing import Protocol, runtime_checkable

from fastapi_cordis import ServiceKey


@runtime_checkable
class Stockroom(Protocol):
    def receive_delivery(self, reference: str, item: str, quantity: int) -> dict: ...

    def stock_levels(self) -> list[dict]: ...


STOCKROOM = ServiceKey("stockroom.service", Stockroom)
