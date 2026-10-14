"""Fetch a pinned Intel companion; build a new recipe only via the build command."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

MANIFEST = Path(__file__).with_name("intel-companion.json")
RELEASE_URL = "https://github.com/toolframe/toolframe-core/releases/download/"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def recipe_identity(recipe):
    return hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def wheel_tag(recipe):
    major, minor, _ = recipe["python"].split(".")
    deployment = recipe["macos_deployment_target"].replace(".", "_")
    return f"cp{major}{minor}-{recipe['abi']}-macosx_{deployment}_x86_64"


def wheel_name(recipe):
    return f"cryptography-{recipe['cryptography']['version']}-{wheel_tag(recipe)}.whl"


def load_manifest(path=MANIFEST):
    manifest = json.loads(path.read_text())
    recipe = manifest["recipe"]
    if manifest["schema"] != 1 or recipe["revision"] != 1:
        raise ValueError(
            "Unsupported Intel companion manifest or build recipe revision"
        )
    if recipe["target"] != "x86_64-apple-darwin" or recipe["abi"] != "abi3":
        raise ValueError("The companion recipe must target macOS Intel with abi3")
    if not re.fullmatch(r"3\.\d+\.\d+", recipe["python"]):
        raise ValueError("The companion recipe requires an exact Python 3 version")
    if not re.fullmatch(r"\d+\.\d+", recipe["macos_deployment_target"]):
        raise ValueError("Invalid macOS deployment target")
    for source in (recipe["openssl"], recipe["cryptography"]):
        if not re.fullmatch(r"\d+(?:\.\d+)+", source["version"]):
            raise ValueError("Use an exact stable native dependency version")
        if not source["url"].startswith("https://") or not re.fullmatch(
            r"[0-9a-f]{64}", source["sha256"]
        ):
            raise ValueError("Native sources require HTTPS URLs and SHA-256 pins")
    return manifest


def published_artifact(manifest):
    recipe = manifest["recipe"]
    artifact = manifest.get("artifact", {})
    if artifact.get("recipe_sha256") != recipe_identity(recipe):
        raise ValueError(
            "No published wheel matches this recipe; explicitly build, verify and pin "
            "the updated dependency before running Core CI"
        )
    if artifact.get("filename") != wheel_name(recipe):
        raise ValueError(
            "Companion filename does not match its version, ABI or platform"
        )
    url = artifact.get("url", "")
    parsed = urllib.parse.urlsplit(url)
    if (
        not url.startswith(RELEASE_URL)
        or not parsed.path.endswith("/" + artifact["filename"])
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "The companion URL must identify a fixed Toolframe Release asset"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")) or not (
        isinstance(artifact.get("size"), int) and artifact["size"] > 0
    ):
        raise ValueError("The companion artifact requires a SHA-256 pin and byte size")
    return artifact


def verify_metadata(path, recipe):
    prefix = f"cryptography-{recipe['cryptography']['version']}.dist-info/"
    with zipfile.ZipFile(path) as wheel:
        metadata_names = [
            n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")
        ]
        wheel_names = [n for n in wheel.namelist() if n.endswith(".dist-info/WHEEL")]
        if metadata_names != [prefix + "METADATA"] or wheel_names != [prefix + "WHEEL"]:
            raise ValueError(
                "Companion distribution metadata does not match the recipe"
            )
        metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
        tags = BytesParser().parsebytes(wheel.read(wheel_names[0]))
    if (
        metadata.get("Name") != "cryptography"
        or metadata.get("Version") != recipe["cryptography"]["version"]
    ):
        raise ValueError("Companion package name or version does not match the recipe")
    if (
        tags.get_all("Tag") != [wheel_tag(recipe)]
        or tags.get("Root-Is-Purelib") != "false"
    ):
        raise ValueError("Companion wheel ABI or platform does not match the recipe")


def verify_digest(path, digest, size=None):
    if sha256(path) != digest:
        raise ValueError(f"Checksum mismatch: {path.name}")
    if size is not None and path.stat().st_size != size:
        raise ValueError(f"Byte size mismatch: {path.name}")


def verify_wheel(path, manifest):
    artifact = published_artifact(manifest)
    if path.name != artifact["filename"]:
        raise ValueError("Companion wheel filename does not match the manifest")
    verify_digest(path, artifact["sha256"], artifact["size"])
    verify_metadata(path, manifest["recipe"])


def download(url, destination, digest, *, size=None, recipe=None):
    """Verify before atomic placement; network failures never select another source."""
    if destination.exists():
        verify_digest(destination, digest, size)
        if recipe is not None:
            verify_metadata(destination, recipe)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
        try:
            with (
                urllib.request.urlopen(url, timeout=30) as response,
                temporary.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
            verify_digest(temporary, digest, size)
            if recipe is not None:
                verify_metadata(temporary, recipe)
            temporary.replace(destination)
            return destination
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (
                408,
                429,
                500,
                502,
                503,
                504,
            ):
                raise RuntimeError(f"Download failed: {url}: {error}") from error
            if attempt == 2:
                raise RuntimeError(
                    f"Download failed after 3 attempts: {url}"
                ) from error
            time.sleep(2**attempt)
        finally:
            temporary.unlink(missing_ok=True)


def fetch_companion(manifest, output):
    artifact = published_artifact(manifest)
    return download(
        artifact["url"],
        output / artifact["filename"],
        artifact["sha256"],
        size=artifact["size"],
        recipe=manifest["recipe"],
    )


def build_companion(manifest, output, publish_url=None):
    """Explicit maintainer entry point; ordinary fetch/CI can never call this."""
    recipe = manifest["recipe"]
    identity = recipe_identity(recipe)
    if manifest.get("artifact", {}).get("recipe_sha256") == identity:
        return fetch_companion(manifest, output)
    candidate = output / "intel-companion.json"
    if candidate.is_file():
        previous = load_manifest(candidate)
        if previous.get("artifact", {}).get("recipe_sha256") == identity:
            return fetch_companion(previous, output)
        raise ValueError("Use a new output directory for a different dependency recipe")
    if not publish_url:
        raise ValueError(
            "A new recipe requires --publish-url for its planned Release asset"
        )
    # Validate the planned destination before compiling anything. The digest and
    # size below are placeholders only for URL/identity validation, never saved.
    planned = {
        "recipe_sha256": identity,
        "filename": wheel_name(recipe),
        "url": publish_url,
        "sha256": "0" * 64,
        "size": 1,
    }
    published_artifact({**manifest, "artifact": planned})
    if sys.platform != "darwin":
        raise RuntimeError(
            "Build Intel companions on macOS with Xcode command-line tools"
        )
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / wheel_name(recipe)
    if wheel.exists():
        raise ValueError(
            "Unrecorded wheel already exists; use a fresh output directory"
        )
    if not subprocess.check_output(["uv", "--version"], text=True).startswith(
        f"uv {recipe['uv']} "
    ):
        raise RuntimeError(f"Use uv {recipe['uv']} for this dependency recipe")
    subprocess.run(
        ["rustup", "target", "add", "--toolchain", recipe["rust"], recipe["target"]],
        check=True,
    )
    python = f"cpython-{recipe['python']}-macos-x86_64-none"
    with tempfile.TemporaryDirectory(prefix="native-build-", dir=output) as directory:
        root = Path(directory)
        for name in ("openssl", "cryptography"):
            source = recipe[name]
            archive = download(source["url"], root / f"{name}.tar.gz", source["sha256"])
            with tarfile.open(archive) as bundle:
                bundle.extractall(root, filter="data")
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        }
        env.update(
            MACOSX_DEPLOYMENT_TARGET=recipe["macos_deployment_target"],
            CC="clang -arch x86_64",
            RUSTUP_TOOLCHAIN=recipe["rust"],
            CARGO_BUILD_TARGET=recipe["target"],
            CARGO_TARGET_DIR=str(root / "cargo-target"),
            CARGO_BUILD_JOBS="2",
        )
        prefix = root / "openssl"
        for command in (
            [
                "perl",
                "Configure",
                "darwin64-x86_64-cc",
                "no-shared",
                "no-tests",
                f"--prefix={prefix}",
            ],
            ["make", "-j8"],
            ["make", "install_sw"],
        ):
            subprocess.run(
                command,
                cwd=root / f"openssl-{recipe['openssl']['version']}",
                env=env,
                check=True,
            )
        env.update(OPENSSL_DIR=str(prefix), OPENSSL_STATIC="1")
        subprocess.run(
            [
                "uv",
                "build",
                "--no-sources",
                "--wheel",
                "--python",
                python,
                "--out-dir",
                str(output),
                str(root / f"cryptography-{recipe['cryptography']['version']}"),
            ],
            env=env,
            check=True,
        )
        verify_metadata(wheel, recipe)
        environment = root / "acceptance"
        subprocess.run(
            ["uv", "venv", "--python", python, str(environment)], env=env, check=True
        )
        executable = environment / "bin/python"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(executable),
                "--only-binary",
                ":all:",
                str(wheel),
            ],
            env=env,
            check=True,
        )
        subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                (
                    "import sys; from cryptography.fernet import Fernet; "
                    "from cryptography.hazmat.backends.openssl.backend import backend; "
                    "cipher = Fernet(Fernet.generate_key()); "
                    "assert cipher.decrypt(cipher.encrypt(b'companion')) == b'companion'; "
                    "assert backend.openssl_version_text().startswith('OpenSSL ' + sys.argv[1] + ' '); "
                    "print(backend.openssl_version_text())"
                ),
                recipe["openssl"]["version"],
            ],
            env=env,
            check=True,
        )
    artifact = {**planned, "sha256": sha256(wheel), "size": wheel.stat().st_size}
    updated = {**manifest, "artifact": artifact}
    verify_wheel(wheel, updated)
    candidate.write_text(json.dumps(updated, indent=2) + "\n")
    return wheel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "verify", "build"))
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--out-dir", type=Path, default=Path("build/intel-companion"))
    parser.add_argument("--wheel", type=Path, help="Existing wheel for verify")
    parser.add_argument(
        "--publish-url", help="Planned Toolframe Release URL for a new recipe"
    )
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.action == "fetch":
            wheel = fetch_companion(manifest, args.out_dir.resolve())
        elif args.action == "build":
            wheel = build_companion(manifest, args.out_dir.resolve(), args.publish_url)
        else:
            if not args.wheel:
                parser.error("verify requires --wheel")
            wheel = args.wheel
            verify_wheel(wheel, manifest)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Intel companion: {error}\n")
    print(f"Verified Intel companion: {wheel}")


if __name__ == "__main__":
    main()
