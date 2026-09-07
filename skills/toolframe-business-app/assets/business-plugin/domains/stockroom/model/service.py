"""Business rules and storage stay in the owning domain, behind one service."""

from dataclasses import dataclass

from toolframe_database_service import DatabaseService


class DeliveryError(ValueError):
    """A delivery can be corrected without changing the application source."""


@dataclass
class StockroomService:
    _database: DatabaseService
    max_delivery_quantity: int

    def receive_delivery(self, reference: str, item: str, quantity: int) -> dict:
        reference, item = reference.strip(), item.strip()
        if not reference or len(reference) > 64 or not item or len(item) > 80:
            raise DeliveryError(
                "Provide a delivery reference (up to 64 characters) and item (up to 80)."
            )
        if type(quantity) is not int or not 1 <= quantity <= self.max_delivery_quantity:
            raise DeliveryError(
                f"Quantity must be a whole number from 1 to {self.max_delivery_quantity}."
            )
        values = {"reference": reference, "item": item, "quantity": quantity}

        def record(transaction):
            identifier = transaction.insert("stockroom_deliveries", values)
            return {"deliveryId": identifier, **values}

        result = self._database.run_once(
            "stockroom.receive_delivery", reference, values, record
        )
        return {**result.value, "replayed": result.replayed}

    def stock_levels(self) -> list[dict]:
        rows = self._database.query(
            "SELECT item, SUM(quantity) AS quantity FROM stockroom_deliveries "
            "GROUP BY item ORDER BY item LIMIT 100",
            max_rows=100,
        )
        return [
            {"item": str(row.item), "quantity": int(row.quantity)}
            for row in rows.itertuples(index=False)
        ]
