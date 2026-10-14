//! Optional, separately published native dependencies. The executable contains
//! only immutable download pins; every cached or supplied wheel is reverified.

use anyhow::{Context, Result, bail};
use reqwest::{Client, Url, redirect::Policy};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

const MANIFEST: &str = include_str!("../companion-wheels.txt");
const MAX_BYTES: u64 = 64 * 1024 * 1024;

struct Wheel<'a> {
    filename: &'a str,
    sha256: &'a str,
    url: &'a str,
}

fn parse(manifest: &str) -> Result<Vec<Wheel<'_>>> {
    let mut wheels = Vec::new();
    for line in manifest.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let parts: Vec<_> = line.split_whitespace().collect();
        if parts.len() != 3 {
            bail!("Invalid companion wheel manifest");
        }
        let [filename, sha256, url] = [parts[0], parts[1], parts[2]];
        if !filename.ends_with(".whl")
            || filename.contains(['/', '\\'])
            || sha256.len() != 64
            || !sha256.bytes().all(|c| c.is_ascii_hexdigit())
        {
            bail!("Invalid companion filename or checksum");
        }
        let parsed = Url::parse(url)?;
        if parsed.scheme() != "https"
            || parsed.host_str() != Some("github.com")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || !parsed
                .path()
                .starts_with("/toolframe/toolframe-core/releases/download/")
            || !parsed.path().ends_with(&format!("/{filename}"))
        {
            bail!("Companion wheels must use the pinned Toolframe GitHub Release");
        }
        wheels.push(Wheel {
            filename,
            sha256,
            url,
        });
    }
    Ok(wheels)
}

fn verified(path: &Path, expected: &str) -> Result<bool> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    if !metadata.file_type().is_file() || metadata.len() > MAX_BYTES {
        return Ok(false);
    }
    let mut input = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()) == expected)
}

pub async fn prepare(
    root: &Path,
    supplied: Option<&Path>,
    offline: bool,
) -> Result<Option<PathBuf>> {
    prepare_manifest(MANIFEST, root, supplied, offline).await
}

async fn prepare_manifest(
    manifest: &str,
    root: &Path,
    supplied: Option<&Path>,
    offline: bool,
) -> Result<Option<PathBuf>> {
    let wheels = parse(manifest)?;
    if wheels.is_empty() {
        return Ok(None);
    }
    let directory = root.join("companions");
    for wheel in wheels {
        let parent = directory.join(wheel.sha256);
        let destination = parent.join(wheel.filename);
        if verified(&destination, wheel.sha256)? {
            continue;
        }
        fs::create_dir_all(&parent)?;
        let mut temporary = tempfile::NamedTempFile::new_in(&parent)?;
        if let Some(source) = supplied
            .map(|p| p.join(wheel.filename))
            .filter(|p| p.exists())
        {
            if !verified(&source, wheel.sha256)? {
                bail!("Companion wheel checksum mismatch: {}", source.display());
            }
            std::io::copy(&mut fs::File::open(source)?, &mut temporary)?;
        } else {
            if offline {
                bail!(
                    "Companion wheel {} is missing or damaged; --offline prevents download. Run once online or supply its verified release wheel with --wheelhouse",
                    wheel.filename
                );
            }
            eprintln!("Downloading verified companion wheel {}", wheel.filename);
            let client = Client::builder()
                .https_only(true)
                .redirect(Policy::limited(5))
                .timeout(Duration::from_secs(120))
                .build()?;
            let mut response = client
                .get(wheel.url)
                .send()
                .await?
                .error_for_status()
                .context("Cannot download the companion wheel from this GitHub Release")?;
            let mut size = 0u64;
            while let Some(chunk) = response.chunk().await? {
                size += chunk.len() as u64;
                if size > MAX_BYTES {
                    bail!("Companion wheel exceeds 64 MiB");
                }
                temporary.write_all(&chunk)?;
            }
        }
        temporary.flush()?;
        if !verified(temporary.path(), wheel.sha256)? {
            bail!("Companion wheel checksum mismatch: {}", wheel.filename);
        }
        temporary.as_file().sync_all()?;
        temporary.persist(&destination)?;
    }
    Ok(Some(directory))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(bytes: &[u8]) -> String {
        format!(
            "crypto-1-py3-none-any.whl {:x} https://github.com/toolframe/toolframe-core/releases/download/v1/crypto-1-py3-none-any.whl",
            Sha256::digest(bytes)
        )
    }

    #[test]
    fn pins_reject_unsafe_paths_and_urls() {
        assert!(parse("# no downloads\n").unwrap().is_empty());
        let good = manifest(b"wheel");
        assert_eq!(parse(&good).unwrap().len(), 1);
        for invalid in [
            good.replace("crypto-1", "../crypto-1"),
            good.replace("https://", "http://"),
            good.replace("github.com", "example.com"),
            good.replace(".whl ", ".zip "),
        ] {
            assert!(parse(&invalid).is_err());
        }
    }

    #[tokio::test]
    async fn offline_requires_verified_bytes_and_rechecks_the_cache() {
        let root = tempfile::tempdir().unwrap();
        let source = tempfile::tempdir().unwrap();
        let pin = manifest(b"wheel");
        assert!(
            prepare_manifest(&pin, root.path(), None, true)
                .await
                .is_err()
        );
        let input = source.path().join("crypto-1-py3-none-any.whl");
        fs::write(&input, b"wrong").unwrap();
        assert!(
            prepare_manifest(&pin, root.path(), Some(source.path()), true)
                .await
                .is_err()
        );
        fs::write(&input, b"wheel").unwrap();
        let directory = prepare_manifest(&pin, root.path(), Some(source.path()), true)
            .await
            .unwrap()
            .unwrap();
        assert!(
            prepare_manifest(&pin, root.path(), None, true)
                .await
                .is_ok()
        );
        let cached = directory
            .join(format!("{:x}", Sha256::digest(b"wheel")))
            .join(input.file_name().unwrap());
        fs::write(cached, b"tampered").unwrap();
        assert!(
            prepare_manifest(&pin, root.path(), None, true)
                .await
                .is_err()
        );
    }
}
