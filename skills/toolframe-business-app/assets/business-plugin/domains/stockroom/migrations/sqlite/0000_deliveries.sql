CREATE TABLE stockroom_deliveries (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL UNIQUE,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);
