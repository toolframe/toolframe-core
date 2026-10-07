# Object storage providers

Object storage is an optional provider, independent of managed SQL databases.
The `object-store-local` and `object-store-s3` plugins implement the internal
`toolframe.object_store` v1 service. Business plugins declare this dependency and
use `RuntimeContext.object_store()` for binary data such as Dashboard images.

Enable one object-store provider per application. Core blocks dependent plugins
when the provider is unavailable, drains their calls during replacement, and
supplies a separate persistent state directory at `data/objects/<provider-id>/`.
Provider state and completed objects survive reload, disable and uninstall.
Selecting another provider does not migrate data.

## Local development installation

Build the matching object-storage SDK and Core revision before installing the
new providers. Existing SDK 0.3.0 releases do not include this development API.
For sibling development checkouts, build wheels into one directory:

```sh
# From the workspace containing the repositories:
uv build --no-sources --wheel --out-dir ./wheelhouse ./toolframe-sdk
uv build --no-sources --wheel --out-dir ./wheelhouse ./toolframe-core
uv build --no-sources --wheel --out-dir ./wheelhouse ./toolframe-object-store-local
uv build --no-sources --wheel --out-dir ./wheelhouse ./toolframe-object-store-s3
uv build --no-sources --wheel --out-dir ./wheelhouse ./toolframe-demo-image-gallery
```

Run the matching Core installation with `TOOLFRAME_WHEELHOUSE` pointing to the
absolute wheelhouse directory. With Dashboard installed, add the local provider
and the optional example gallery:

```sh
toolframe-core --home APP plugin add ./toolframe-object-store-local --enable
toolframe-core --home APP plugin add ./toolframe-demo-image-gallery --enable
```

The gallery contributes an Image Gallery page with upload, display, pagination
and deletion. Core enforces its `image-gallery.read` and `image-gallery.write`
scopes. It uses the same business service through the authenticated service API:
`POST /_toolframe/v1/services/image-gallery/image-gallery.images/<method>`.
The storage service itself is never a visitor-facing API. Dashboard media stays
behind Core's existing access policy; no public file route is added.

For S3, configure the plugin's existing bucket, endpoint, region and optional
prefix before enabling it. Credentials come from the standard AWS credential
chain in the Core environment; keep them outside composition and source control.
The provider never creates or makes a bucket public. Use a separate application
and bucket/prefix for runtime acceptance checks against RustFS or another S3
service. The S3 plugin README documents its configuration and integration test.

Current bounds are 64 MiB per object, 256 KiB per wire chunk, and ten-minute upload
sessions; the sample gallery accepts PNG/JPEG/WebP files up to 20 MiB. Transfers
stage on local temporary storage before atomic publication. Multipart/resumable
uploads across restarts, signed download URLs, remote data migration and
cross-resource transactions are outside this version.
