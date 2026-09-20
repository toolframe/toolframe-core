# Own credentials in the auth plugin and present them through Dashboard Auth

`toolframe-auth` and `toolframe-dashboard-auth` are separate repositories.
Auth owns password records, sessions and
scope decisions through its isolated managed SQLite store. Dashboard Auth owns
login and password-management presentation. Core applies decisions, carries
trusted caller context and determines public fallback; the Dashboard provider
renders the persistent access-mode notice.

Keeping credentials out of core SQLite and out of the Dashboard presentation
plugin preserves data ownership and allows other consumers to adopt the auth
capability later. Core retains its independent management/recovery entry, and a
failed auth page cannot remove the host-supplied public-access notice.
