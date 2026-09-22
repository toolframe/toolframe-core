//! Thin bootstrap: package/runtime preparation only. Python owns all plugin policy.

mod bundle;
mod uv;

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::{env, fs, path::PathBuf, process::Stdio};
use tokio::process::Command;

include!(concat!(env!("OUT_DIR"), "/bundle.rs"));

#[tokio::main]
async fn main() -> Result<()> {
    let arguments: Vec<_> = env::args_os().skip(1).collect();
    if matches!(arguments.as_slice(), [argument] if argument == "--version" || argument == "-V") {
        println!("toolframe {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    tracing_subscriber::fmt()
        .with_env_filter("warn")
        .with_writer(std::io::stderr)
        .init();
    let mut forwarded = Vec::new();
    let mut offline = false;
    let mut uv_path = None;
    let mut wheelhouse = env::var_os("TOOLFRAME_WHEELHOUSE").map(PathBuf::from);
    let mut args = arguments.into_iter();
    while let Some(argument) = args.next() {
        match argument.to_str() {
            Some("--offline") => offline = true,
            Some("--uv-path") => {
                uv_path = Some(PathBuf::from(
                    args.next().context("--uv-path needs a path")?,
                ))
            }
            Some("--wheelhouse") => {
                wheelhouse = Some(PathBuf::from(
                    args.next().context("--wheelhouse needs a path")?,
                ))
            }
            _ => forwarded.push(argument),
        }
    }
    if forwarded.is_empty() {
        forwarded.push("serve".into());
    }
    let root = env::var_os("TOOLFRAME_RUNTIME_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("HOME").unwrap_or_else(|| ".".into()))
                .join(".cache/toolframe")
        });
    fs::create_dir_all(&root)?;
    let resolved = uv::resolve(uv_path.as_deref(), &root.join("uv"), offline).await?;
    let mut fingerprint = Sha256::new();
    let python_request = format!(
        "cpython-3.14.7-{}-{}-none",
        env::consts::OS,
        env::consts::ARCH
    );
    fingerprint.update(b"toolframe-core==0.5.2;bootstrap3");
    fingerprint.update(python_request.as_bytes());
    let bundles = bundle::extract(&root.join("bundles"), WHEELS)?;
    for (name, content) in WHEELS {
        fingerprint.update(name.as_bytes());
        fingerprint.update(content);
    }
    if let Some(path) = &wheelhouse {
        fingerprint.update(path.canonicalize()?.as_os_str().as_encoded_bytes());
        // Local development rebuilds can retain versions, so include their wheel bytes.
        let mut paths: Vec<_> = fs::read_dir(path)?
            .filter_map(|entry| entry.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|e| e == "whl"))
            .collect();
        paths.sort();
        for path in paths {
            fingerprint.update(fs::read(path)?);
        }
    }
    let environment = root
        .join("core")
        .join(format!("{:x}", fingerprint.finalize()));
    let python = environment.join("bin/python");
    // Serialize bootstrap across simultaneous invocations; never mutate a published environment.
    fs::create_dir_all(environment.parent().unwrap())?;
    let lock_path = root.join("bootstrap.lock");
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(lock_path)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
            bail!("Cannot lock bootstrap");
        }
    }
    if !environment.join(".ready").exists() {
        if environment.exists() {
            fs::remove_dir_all(&environment)?;
        }
        let mut create = Command::new(&resolved.path);
        create
            .args(["--no-config", "venv", "--python", &python_request])
            .arg(&environment);
        if offline {
            create.arg("--offline");
        }
        if !create.status().await?.success() {
            bail!("Python environment preparation failed");
        }
        let mut requirements = Vec::new();
        for (name, _) in WHEELS {
            if bundle::is_bootstrap(name) {
                requirements.push(
                    bundle::wheel_path(&bundles, name)
                        .to_string_lossy()
                        .to_string(),
                );
            }
        }
        if requirements.is_empty() {
            requirements.push("toolframe-core==0.5.2".into());
        }
        let input = environment.join("requirements.in");
        fs::write(&input, requirements.join("\n") + "\n")?;
        let lockfile = environment.join("pylock.toml");
        let mut compile = Command::new(&resolved.path);
        compile
            .args([
                "--no-config",
                "pip",
                "compile",
                "--format",
                "pylock.toml",
                "--python",
            ])
            .arg(&python)
            .arg("--output-file")
            .arg(&lockfile)
            .arg(&input)
            .stdout(Stdio::null());
        if let Some(path) = &wheelhouse {
            compile.arg("--find-links").arg(path);
        }
        for (name, _) in WHEELS {
            compile
                .arg("--find-links")
                .arg(bundle::wheel_path(&bundles, name).parent().unwrap());
        }
        if offline {
            compile.arg("--offline");
        }
        if !compile.status().await?.success() {
            bail!(
                "Core dependency resolution failed; use a release bundle or --wheelhouse for local builds"
            );
        }
        let mut sync = Command::new(&resolved.path);
        sync.args(["--no-config", "pip", "sync", "--python"])
            .arg(&python)
            .arg(&lockfile);
        if offline {
            sync.arg("--offline");
        }
        if !sync.status().await?.success() {
            bail!("Core installation failed");
        }
        fs::write(environment.join(".ready"), b"ready\n")?;
    }
    drop(lock);
    let mut command = Command::new(&python);
    command
        .args(["-I", "-m", "toolframe_core.cli"])
        .args(forwarded)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    let mut paths = vec![resolved.path.parent().unwrap().to_path_buf()];
    if let Some(existing) = env::var_os("PATH") {
        paths.extend(env::split_paths(&existing));
    }
    command.env("PATH", env::join_paths(paths)?);
    if let Some(path) = wheelhouse {
        command.env("TOOLFRAME_WHEELHOUSE", path.canonicalize()?);
    }
    command.env("TOOLFRAME_BUNDLED_WHEELHOUSE", &bundles);
    for (name, _) in WHEELS {
        if name.starts_with("toolframe_sdk-") {
            command.env("TOOLFRAME_SDK_WHEEL", bundle::wheel_path(&bundles, name));
        }
    }
    if offline {
        command.env("TOOLFRAME_OFFLINE", "1");
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let error = command.as_std_mut().exec();
        Err(error).context("Failed to start Python core")
    }
    #[cfg(not(unix))]
    {
        let status = command.status().await?;
        std::process::exit(status.code().unwrap_or(1));
    }
}
