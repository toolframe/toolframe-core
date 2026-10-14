"""Build a native-only PyPI wheel and the identical GitHub Release executable."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
)
COMPANION = "cryptography-50.0.2-cp314-abi3-macosx_12_0_x86_64.whl"


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def prepare(args):
    stage = args.stage.resolve()
    stage.mkdir(parents=True, exist_ok=False)
    for name in (
        "Cargo.toml",
        "Cargo.lock",
        "uv-release-manifest.txt",
        "companion-wheels.txt",
        "LICENSE",
    ):
        shutil.copy2(ROOT / name, stage / name)
    (stage / "src").mkdir()
    for source in (ROOT / "src").glob("*.rs"):
        shutil.copy2(source, stage / "src" / source.name)
    for source in (ROOT / "packaging/launcher").iterdir():
        shutil.copy2(source, stage / source.name)
    version = tomllib.loads((stage / "Cargo.toml").read_text())["package"]["version"]
    manifest = {
        "target": args.target,
        "version": version,
        "embedded_wheels": [],
        "companions": [],
    }
    if args.target == "x86_64-apple-darwin":
        native = args.native_wheelhouse / COMPANION if args.native_wheelhouse else None
        if native is None or not native.is_file():
            raise ValueError(
                f"Intel builds require a separately published companion: {COMPANION}"
            )
        with zipfile.ZipFile(native) as wheel:
            metadata = wheel.read(
                next(n for n in wheel.namelist() if n.endswith(".dist-info/WHEEL"))
            ).decode()
            assert "Tag: cp314-abi3-macosx_12_0_x86_64" in metadata
        digest = sha256(native)
        url = f"https://github.com/toolframe/toolframe-core/releases/download/v{version}/{COMPANION}"
        (stage / "companion-wheels.txt").write_text(f"{COMPANION} {digest} {url}\n")
        # Keep external payloads outside the Cargo/maturin source tree.
        companion_output = args.output.resolve() / "companions"
        companion_output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(native, companion_output / native.name)
        manifest["companions"].append(
            {"filename": COMPANION, "sha256": digest, "url": url}
        )
    assert not list(stage.rglob("*.whl")), "No Python wheels may enter the native build"
    (stage / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def build(args):
    if "apple-darwin" in args.target:
        os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "12.0")
    subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--from",
            "maturin==1.14.1",
            "maturin",
            "build",
            "--release",
            "--locked",
            "--target",
            args.target,
            "--compatibility",
            "manylinux_2_28" if "linux" in args.target else "off",
            "--out",
            str(args.output.resolve() / "wheels"),
        ],
        cwd=args.stage.resolve(),
        check=True,
    )


def finish(args):
    stage, output = args.stage.resolve(), args.output.resolve()
    manifest = json.loads((stage / "release-manifest.json").read_text())
    if manifest["target"] != args.target:
        raise ValueError("The staged launcher belongs to another platform")
    wheels = list((output / "wheels").glob("toolframe-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected one native launcher wheel for this platform")
    release = output / "release"
    release.mkdir(parents=True, exist_ok=False)
    binary = release / "toolframe"
    with zipfile.ZipFile(wheels[0]) as wheel:
        scripts = [
            n for n in wheel.namelist() if ".data/scripts/" in n and not n.endswith("/")
        ]
        if len(scripts) != 1 or not scripts[0].endswith("/toolframe"):
            raise ValueError(
                "The launcher wheel must contain exactly the native toolframe command"
            )
        if any(n.endswith((".py", ".so", ".pyd", ".whl")) for n in wheel.namelist()):
            raise ValueError(
                "The launcher wheel must not contain Python code, extensions or nested wheels"
            )
        binary.write_bytes(wheel.read(scripts[0]))
    binary.chmod(0o755)
    archive = release / f"toolframe-{manifest['version']}-{args.target}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in (binary, stage / "LICENSE", stage / "NOTICE"):
            tar.add(path, arcname=path.name)
    manifest.update(
        binary_sha256=sha256(binary),
        wheel=wheels[0].name,
        wheel_sha256=sha256(wheels[0]),
    )
    checksums = [f"{sha256(archive)}  {archive.name}"]
    for companion in manifest["companions"]:
        path = output / "companions" / companion["filename"]
        assert sha256(path) == companion["sha256"]
        shutil.copy2(path, release / path.name)
        checksums.append(f"{companion['sha256']}  {path.name}")
    (release / f"manifest-{args.target}.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    (release / f"SHA256SUMS-{args.target}.txt").write_text("\n".join(checksums) + "\n")
    print(f"Native wheel and release executable: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "build", "finish"))
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--stage", type=Path, default=ROOT / "build/launcher")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/launcher")
    parser.add_argument("--native-wheelhouse", type=Path)
    args = parser.parse_args()
    {"prepare": prepare, "build": build, "finish": finish}[args.action](args)


if __name__ == "__main__":
    main()
