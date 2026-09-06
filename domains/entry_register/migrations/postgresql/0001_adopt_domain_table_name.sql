-- Preserve the legacy revision sequence while using PostgreSQL-native DDL.
ALTER TABLE dashboard_example_entries RENAME TO entry_register_entries;
