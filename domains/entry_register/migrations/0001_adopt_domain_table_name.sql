-- Move the preserved example table under the Entry Register domain name.
--
-- 0000_bootstrap.sql remains byte-for-byte immutable so existing migration
-- ledgers keep their checksum. This next global migration performs the rename
-- for both existing installations and fresh scaffolds.

ALTER TABLE dashboard_example_entries RENAME TO entry_register_entries;
