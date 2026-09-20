# Identify password-only logins without reusing credential identity

Login requires only a password; credential names remain operator labels.
A secret may identify at most one valid credential at a time.
An expired or revoked secret may be reused as
a new authorization. Stable credential identity must therefore remain distinct
from the secret: new grants do not reactivate or transfer old browser sessions.

A person who knows a reused secret can explicitly log in using the new grant.
This is an accepted consequence of allowing reuse, not a means of identifying a
particular person. Creation and expiry edits must preserve unambiguous matching
even under concurrency; inactive records are not selected for login.
