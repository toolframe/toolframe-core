//! Embedded packages are a wheel source; only bootstrap packages enter core.

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub fn is_bootstrap(name: &str) -> bool {
    ["toolframe_core-", "toolframe_sdk-", "fastapi_cordis-"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}

pub fn wheel_path(directory: &Path, name: &str) -> PathBuf {
    directory.join(name.trim_end_matches(".whl")).join(name)
}

/// Publish the entire immutable set before any concurrent launcher uses it.
/// Each wheel has its own directory so Python can exclude a bundled wheel when
/// an explicit local wheelhouse supplies the same filename.
pub fn extract(root: &Path, wheels: &[(&str, &[u8])]) -> Result<PathBuf> {
    let mut digest = Sha256::new();
    for (name, content) in wheels {
        digest.update(name.as_bytes());
        digest.update(Sha256::digest(content));
    }
    let directory = root.join(format!("{:x}", digest.finalize()));
    fs::create_dir_all(root)?;
    if !directory.exists() {
        let staging = tempfile::tempdir_in(root)?;
        for (name, content) in wheels {
            let path = wheel_path(staging.path(), name);
            fs::create_dir_all(path.parent().unwrap())?;
            fs::write(path, content)?;
        }
        if let Err(error) = fs::rename(staging.path(), &directory) {
            // A competing launcher may have published this exact content set.
            if !directory.is_dir() {
                return Err(error).context("Cannot publish embedded wheels");
            }
        }
    }
    for (name, content) in wheels {
        let path = wheel_path(&directory, name);
        if fs::read(&path)
            .with_context(|| format!("Cannot read embedded wheel {}", path.display()))?
            != *content
        {
            bail!(
                "Embedded wheel cache is damaged: {}; move this bundle directory aside and retry",
                directory.display()
            );
        }
    }
    Ok(directory)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn providers_and_native_dependencies_are_not_bootstrap_requirements() {
        for name in [
            "toolframe_core-0.2.0-py3-none-any.whl",
            "toolframe_sdk-0.1.0-py3-none-any.whl",
            "fastapi_cordis-0.2.0-py3-none-any.whl",
        ] {
            assert!(is_bootstrap(name));
        }
        for name in [
            "toolframe_fastmcp-0.1.0-py3-none-any.whl",
            "toolframe_streamlit_dashboard-0.1.0-py3-none-any.whl",
            "cryptography-50.0.1-cp314-abi3-macosx_12_0_x86_64.whl",
        ] {
            assert!(!is_bootstrap(name));
        }
    }

    #[test]
    fn bundle_is_complete_reusable_and_content_addressed() {
        let root = tempfile::tempdir().unwrap();
        let wheels: &[(&str, &[u8])] = &[("core.whl", b"core"), ("provider.whl", b"provider")];
        let first = extract(root.path(), wheels).unwrap();
        assert_eq!(
            fs::read(wheel_path(&first, "provider.whl")).unwrap(),
            b"provider"
        );
        assert_eq!(extract(root.path(), wheels).unwrap(), first);
        let second = extract(
            root.path(),
            &[("core.whl", b"core"), ("provider.whl", b"updated")],
        )
        .unwrap();
        assert_ne!(first, second);
        assert_eq!(
            fs::read(wheel_path(&first, "provider.whl")).unwrap(),
            b"provider"
        );
        fs::write(wheel_path(&first, "provider.whl"), b"damaged").unwrap();
        assert!(
            extract(root.path(), wheels)
                .unwrap_err()
                .to_string()
                .contains("damaged")
        );
    }
}
