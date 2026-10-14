# Intel macOS cryptography companion

Intel launcher builds reuse a cryptography wheel published by Toolframe. The
source is pinned in [`scripts/ci/intel-companion.json`](../scripts/ci/intel-companion.json).
Core CI and publication fetch that wheel; they never build cryptography or
OpenSSL. A missing asset or failed verification stops the build.

## Fetch and verify

From the Core checkout:

```sh
python3.14 scripts/ci/intel_companion.py fetch --out-dir build/intel-companion
python3.14 scripts/ci/intel_companion.py verify \
  --wheel build/intel-companion/cryptography-50.0.2-cp314-abi3-macosx_12_0_x86_64.whl
```

Both commands check the pinned SHA-256, byte size, distribution name/version,
non-pure wheel metadata and exact Python ABI/platform tag. Existing local files
are checked again before reuse. Downloads use temporary files and become visible
only after verification. Transient network failures receive at most three
attempts with short backoff; missing assets and integrity failures do not select
another source or trigger compilation. Remove a corrupt local copy explicitly
before fetching again.

The manifest separates the native recipe from its published artifact. The recipe
identity covers OpenSSL and cryptography versions, source URLs and SHA-256 pins,
Python version and ABI, target architecture, macOS deployment target, Rust, uv,
and the build recipe revision. Revision 1 specifies static OpenSSL with
`darwin64-x86_64-cc`, `no-shared`, `no-tests`, Intel Clang and the explicit Rust
target. Changes to these fixed build settings require updating the recipe revision
and its supported implementation. This identity does not contain a Core version,
Core commit or the fastmcp repository revision. The wheel's own SHA-256 identifies
the exact published bytes; the recipe identity is not a promise of byte-for-byte
reproducible compilation across compiler/SDK installations.

`prepare-launcher.py` and `validate.py --checks launcher` fetch the same pin.
`build-launcher.py prepare` checks it again and stages it outside the Cargo build
tree. Each Core release copies those bytes to its own Release asset and compiles
that Core version's companion URL into the launcher. Keep the source asset
available even after newer Core releases ship. Native wheel/archive identity and
installed-wheel acceptance remain part of the normal validation pipeline.

## Update the native dependency

Only an explicit `build` command can compile a changed recipe. Use macOS with
Xcode command-line tools, make, Perl, the recipe's uv and Rust versions, and
Rosetta when running Intel Python on Apple Silicon. The command downloads and
hash-checks the upstream sources, builds static OpenSSL and cryptography, then
installs the wheel into a fresh Intel environment. It must pass an encryption
round-trip and an OpenSSL version check before a candidate manifest is written.

1. Copy the manifest to an ignored build directory and edit only the intended
   native inputs. Confirm new source hashes against upstream release evidence.
   Leave the old artifact identity unchanged so it cannot match the new recipe.

   ```sh
   mkdir -p build/native-update
   cp scripts/ci/intel-companion.json build/native-update/input.json
   ```

2. Choose a new fixed asset URL under Toolframe's Core Releases. Substitute the
   actual tag and wheel filename below, then build explicitly:

   ```sh
   python3.14 scripts/ci/intel_companion.py build \
     --manifest build/native-update/input.json \
     --out-dir build/native-update/candidate \
     --publish-url 'https://github.com/toolframe/toolframe-core/releases/download/<native-tag>/<wheel-filename>'
   ```

   This writes the wheel and `candidate/intel-companion.json` locally and does
   not publish them. If the recipe already has a matching artifact pin, `build`
   fetches or reuses that wheel. Repeating a successful update in the same output
   directory also verifies and reuses its candidate. A failed build leaves no
   candidate manifest; use a fresh output directory when retrying or changing
   the recipe.

3. Review the candidate metadata, recipe, smoke result and digest. Publish the
   exact candidate bytes to the planned URL through the normal release approval
   process. Keep this native asset separate from an unreleased Core version:
   ordinary Core CI must be able to fetch it before building that Core release.

4. Fetch into a fresh directory using the candidate manifest to verify the
   published asset, then replace the checked-in manifest with the candidate:

   ```sh
   python3.14 scripts/ci/intel_companion.py fetch \
     --manifest build/native-update/candidate/intel-companion.json \
     --out-dir build/native-update/published-check
   cp build/native-update/candidate/intel-companion.json scripts/ci/intel-companion.json
   python3.14 scripts/ci/validate.py --checks launcher \
     --target x86_64-apple-darwin --source-root /path/to/sibling-repositories \
     --output build/check-updated-companion
   ```

   Commit the manifest only after this installed-wheel validation succeeds.
   Updating the recipe without publishing and pinning its wheel intentionally
   makes ordinary fetch fail. Never overwrite an existing published wheel with
   different bytes.
