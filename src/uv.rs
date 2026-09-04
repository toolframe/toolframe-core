use std::{
    env, fs,
    io::{self, IsTerminal, Read, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, bail};
use flate2::read::GzDecoder;
use reqwest::{Client, redirect::Policy};
use sha2::{Digest, Sha256};
use tokio::{process::Command, time};
use tracing::info;
use zip::ZipArchive;

#[cfg(unix)]
use std::fs::OpenOptions;

const RELEASE_MANIFEST: &str = include_str!("../uv-release-manifest.txt");
const UV_RELEASE_BASE_URL: &str = "https://github.com/astral-sh/uv/releases/download";
const MINIMUM_UV_VERSION: (u64, u64, u64) = (0, 12, 8);
pub const MINIMUM_MACOS_VERSION: &str = "12.0";
const VERSION_CHECK_TIMEOUT: Duration = Duration::from_secs(30);
const DOWNLOAD_STALL_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_ARCHIVE_BYTES: u64 = 256 * 1024 * 1024;
const MAX_EXECUTABLE_BYTES: u64 = 256 * 1024 * 1024;
const PROGRESS_WIDTH: usize = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ArchiveKind {
    TarGz,
    Zip,
}

impl ArchiveKind {
    const fn extension(self) -> &'static str {
        match self {
            Self::TarGz => "tar.gz",
            Self::Zip => "zip",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UvRelease {
    target: String,
    version: String,
    archive_sha256: String,
    executable_sha256: String,
}

impl UvRelease {
    fn archive_kind(&self) -> ArchiveKind {
        if self.target.ends_with("-pc-windows-msvc") {
            ArchiveKind::Zip
        } else {
            ArchiveKind::TarGz
        }
    }

    fn archive_name(&self) -> String {
        format!("uv-{}.{}", self.target, self.archive_kind().extension())
    }

    fn archive_member(&self) -> String {
        match self.archive_kind() {
            ArchiveKind::TarGz => format!("uv-{}/uv", self.target),
            ArchiveKind::Zip => "uv.exe".to_owned(),
        }
    }

    fn download_url(&self) -> String {
        format!(
            "{}/{}/{}",
            UV_RELEASE_BASE_URL,
            self.version,
            self.archive_name()
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UvSource {
    Configured,
    Managed,
    Downloaded,
    Path,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedUv {
    pub path: PathBuf,
    pub version: String,
    pub source: UvSource,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UvBuild {
    version: String,
    target: Option<String>,
    semantic_version: (u64, u64, u64),
}

/// Resolve the target-matched UV release. An explicit path is honored only
/// when it matches the pin; otherwise the managed copy is verified or fetched.
pub async fn resolve(
    explicit: Option<&Path>,
    runtime_root: &Path,
    offline: bool,
) -> Result<ResolvedUv> {
    let release = release_for_current_target()?;

    if let Some(path) = explicit {
        return inspect_candidate(path, release.as_ref(), UvSource::Configured)
            .await
            .context("the configured UV executable is invalid");
    }

    let executable_name = if cfg!(windows) { "uv.exe" } else { "uv" };
    let managed_uv = runtime_root.join(executable_name);
    let managed_problem = match fs::symlink_metadata(&managed_uv) {
        Ok(metadata) if metadata.is_dir() => {
            bail!(
                "the managed UV path {} is a directory; move it aside so toolframe can install the pinned executable",
                managed_uv.display()
            );
        }
        Ok(_) => match inspect_candidate(&managed_uv, release.as_ref(), UvSource::Managed).await {
            Ok(candidate) => return Ok(candidate),
            Err(error) => Some(error),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            return Err(error).with_context(|| {
                format!("could not inspect managed UV at {}", managed_uv.display())
            });
        }
    };

    if let Some(release) = release.as_ref() {
        if offline {
            let reason = managed_problem
                .as_ref()
                .map(|error| format!(" ({error:#})"))
                .unwrap_or_default();
            bail!(
                "pinned UV {} for {} is not available at {}{reason}; --offline prevents downloading it, so run once online or pass an exact --uv-path",
                release.version,
                release.target,
                managed_uv.display()
            );
        }

        fs::create_dir_all(runtime_root).with_context(|| {
            format!(
                "could not create private runtime directory {}",
                runtime_root.display()
            )
        })?;
        if let Some(error) = managed_problem.as_ref() {
            info!(
                target: "toolframe::uv",
                reason = %format!("{error:#}"),
                "managed uv does not match the pinned release; downloading a replacement"
            );
        } else {
            info!(
                target: "toolframe::uv",
                version = %release.version,
                target_triple = %release.target,
                "managed uv is missing; downloading the pinned release"
            );
        }

        download_and_install(release, runtime_root, &managed_uv).await?;
        return inspect_candidate(&managed_uv, Some(release), UvSource::Downloaded)
            .await
            .context("the newly downloaded UV executable failed verification");
    }

    if let Some(candidate) = matching_path_candidate(executable_name, None).await {
        return Ok(candidate);
    }
    if let Some(error) = managed_problem {
        return Err(error).context("the managed UV executable is invalid");
    }
    bail!(
        "this platform has no pinned UV download; pass --uv-path, set TOOLFRAME_UV, or install a compatible uv on PATH"
    )
}

async fn matching_path_candidate(name: &str, release: Option<&UvRelease>) -> Option<ResolvedUv> {
    let path = env::var_os("PATH")?;
    for directory in env::split_paths(&path) {
        let candidate = directory.join(name);
        if let Ok(candidate) = inspect_candidate(&candidate, release, UvSource::Path).await {
            return Some(candidate);
        }
    }
    None
}

async fn inspect_candidate(
    path: &Path,
    release: Option<&UvRelease>,
    source: UvSource,
) -> Result<ResolvedUv> {
    let path = validate_executable(path)?;
    if let Some(release) = release {
        let actual_sha256 = sha256_file(&path)?;
        if actual_sha256 != release.executable_sha256 {
            bail!(
                "UV executable checksum mismatch: expected {}, got {}",
                release.executable_sha256,
                actual_sha256
            );
        }
    }

    let output = time::timeout(
        VERSION_CHECK_TIMEOUT,
        Command::new(&path)
            .arg("--version")
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .with_context(|| format!("timed out checking UV version at {}", path.display()))?
    .with_context(|| format!("could not run {} --version", path.display()))?;
    if !output.status.success() {
        bail!(
            "{} --version failed ({}): {}",
            path.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let output = String::from_utf8(output.stdout).context("UV version output was not UTF-8")?;
    let build = parse_uv_version(&output)?;
    if let Some(release) = release {
        if build.version != release.version {
            bail!(
                "UV {} does not match pinned UV {}",
                build.version,
                release.version
            );
        }
        if build.target.as_deref() != Some(release.target.as_str()) {
            bail!(
                "UV {} reports target {}, expected {}",
                build.version,
                build.target.as_deref().unwrap_or("unknown"),
                release.target
            );
        }
    } else if build.semantic_version < MINIMUM_UV_VERSION {
        bail!(
            "UV {} is too old; toolframe requires UV {}.{}.{} or newer",
            build.version,
            MINIMUM_UV_VERSION.0,
            MINIMUM_UV_VERSION.1,
            MINIMUM_UV_VERSION.2
        );
    }

    Ok(ResolvedUv {
        path,
        version: build.version,
        source,
    })
}

async fn download_and_install(
    release: &UvRelease,
    runtime_root: &Path,
    destination: &Path,
) -> Result<()> {
    let url = release.download_url();
    let client = Client::builder()
        .https_only(true)
        .redirect(Policy::limited(10))
        .connect_timeout(Duration::from_secs(30))
        .user_agent(concat!("toolframe/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("could not initialize the built-in HTTPS client")?;
    let mut response = client
        .get(&url)
        .send()
        .await
        .with_context(|| format!("could not download {url}"))?
        .error_for_status()
        .with_context(|| format!("UV download returned an error for {url}"))?;
    let expected_size = response.content_length();
    if expected_size.is_some_and(|size| size > MAX_ARCHIVE_BYTES) {
        bail!(
            "UV archive is larger than the {} MiB safety limit",
            MAX_ARCHIVE_BYTES / 1024 / 1024
        );
    }

    let mut archive = tempfile::Builder::new()
        .prefix(".toolframe-uv-archive-")
        .suffix(&format!(".{}", release.archive_kind().extension()))
        .tempfile_in(runtime_root)
        .with_context(|| {
            format!(
                "could not create a UV download file in {}",
                runtime_root.display()
            )
        })?;

    let mut digest = Sha256::new();
    let mut downloaded = 0_u64;
    let mut progress = DownloadProgress::new(
        format!("uv {} ({})", release.version, release.target),
        expected_size,
    );

    while let Some(chunk) = time::timeout(DOWNLOAD_STALL_TIMEOUT, response.chunk())
        .await
        .context("UV download stalled for 30 seconds")?
        .context("could not read the UV download stream")?
    {
        downloaded = downloaded
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| anyhow!("UV download size overflowed"))?;
        if downloaded > MAX_ARCHIVE_BYTES {
            bail!(
                "UV download exceeded the {} MiB safety limit",
                MAX_ARCHIVE_BYTES / 1024 / 1024
            );
        }
        archive
            .write_all(&chunk)
            .context("could not write the UV download")?;
        digest.update(&chunk);
        progress.update(downloaded);
    }
    archive
        .as_file_mut()
        .sync_all()
        .context("could not sync the downloaded UV archive")?;
    progress.finish(downloaded);

    let archive_sha256 = format!("{:x}", digest.finalize());
    if archive_sha256 != release.archive_sha256 {
        bail!(
            "downloaded UV archive checksum mismatch: expected {}, got {}",
            release.archive_sha256,
            archive_sha256
        );
    }

    let extracted = extract_uv(release, archive.path(), runtime_root)?;
    let executable_sha256 = sha256_file(extracted.path())?;
    if executable_sha256 != release.executable_sha256 {
        bail!(
            "extracted UV executable checksum mismatch: expected {}, got {}",
            release.executable_sha256,
            executable_sha256
        );
    }

    publish_executable(extracted.path(), runtime_root, destination)?;
    info!(
        target: "toolframe::uv",
        version = %release.version,
        target_triple = %release.target,
        destination = %destination.display(),
        "downloaded and installed verified uv"
    );
    Ok(())
}

fn extract_uv(
    release: &UvRelease,
    archive_path: &Path,
    runtime_root: &Path,
) -> Result<tempfile::NamedTempFile> {
    let mut extracted = tempfile::Builder::new()
        .prefix(".toolframe-uv-extract-")
        .tempfile_in(runtime_root)
        .with_context(|| {
            format!(
                "could not create a UV extraction file in {}",
                runtime_root.display()
            )
        })?;
    let member = release.archive_member();
    match release.archive_kind() {
        ArchiveKind::TarGz => {
            let archive = fs::File::open(archive_path).with_context(|| {
                format!("could not open verified archive {}", archive_path.display())
            })?;
            let mut archive = tar::Archive::new(GzDecoder::new(archive));
            let mut found = false;
            for entry in archive
                .entries()
                .context("could not read the verified UV tar archive")?
            {
                let mut entry = entry.context("could not read a verified UV tar entry")?;
                if entry
                    .path()
                    .context("verified UV archive contained an invalid path")?
                    != Path::new(&member)
                {
                    continue;
                }
                if found {
                    bail!("verified UV archive contains duplicate member {member}");
                }
                if !entry.header().entry_type().is_file() {
                    bail!("verified UV archive member {member} is not a regular file");
                }
                copy_bounded(&mut entry, extracted.as_file_mut(), &member)?;
                found = true;
            }
            if !found {
                bail!("verified UV archive did not contain {member}");
            }
        }
        ArchiveKind::Zip => {
            let archive = fs::File::open(archive_path).with_context(|| {
                format!("could not open verified archive {}", archive_path.display())
            })?;
            let mut archive =
                ZipArchive::new(archive).context("could not read the verified UV zip archive")?;
            let mut entry = archive
                .by_name(&member)
                .with_context(|| format!("verified UV archive did not contain {member}"))?;
            if !entry.is_file() {
                bail!("verified UV archive member {member} is not a regular file");
            }
            copy_bounded(&mut entry, extracted.as_file_mut(), &member)?;
        }
    }
    extracted
        .as_file_mut()
        .sync_all()
        .context("could not sync the extracted UV executable")?;
    Ok(extracted)
}

fn copy_bounded(reader: &mut impl Read, writer: &mut impl Write, member: &str) -> Result<()> {
    let mut limited = reader.take(MAX_EXECUTABLE_BYTES + 1);
    let written = io::copy(&mut limited, writer)
        .with_context(|| format!("could not extract verified UV archive member {member}"))?;
    if written > MAX_EXECUTABLE_BYTES {
        bail!(
            "verified UV archive member {member} exceeds the {} MiB safety limit",
            MAX_EXECUTABLE_BYTES / 1024 / 1024
        );
    }
    Ok(())
}

fn publish_executable(source: &Path, runtime_root: &Path, destination: &Path) -> Result<()> {
    if fs::symlink_metadata(destination).is_ok_and(|metadata| metadata.is_dir()) {
        bail!(
            "UV destination {} became a directory; refusing to replace it",
            destination.display()
        );
    }
    let staged = tempfile::Builder::new()
        .prefix(".toolframe-uv-install-")
        .tempfile_in(runtime_root)
        .with_context(|| {
            format!(
                "could not create a UV installation file in {}",
                runtime_root.display()
            )
        })?;
    fs::copy(source, staged.path()).with_context(|| {
        format!(
            "could not stage UV from {} to {}",
            source.display(),
            staged.path().display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        staged
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o755))
            .with_context(|| format!("could not make {} executable", staged.path().display()))?;
    }
    staged
        .as_file()
        .sync_all()
        .with_context(|| format!("could not sync staged UV at {}", staged.path().display()))?;
    let installed = staged
        .persist(destination)
        .map_err(|error| error.error)
        .with_context(|| {
            format!(
                "could not atomically install downloaded UV at {}",
                destination.display()
            )
        })?;
    installed
        .sync_all()
        .with_context(|| format!("could not sync installed UV at {}", destination.display()))?;

    #[cfg(unix)]
    OpenOptions::new()
        .read(true)
        .open(runtime_root)
        .and_then(|directory| directory.sync_all())
        .with_context(|| {
            format!(
                "could not sync UV installation directory {}",
                runtime_root.display()
            )
        })?;
    Ok(())
}

struct DownloadProgress {
    label: String,
    total: Option<u64>,
    started: Instant,
    last_draw: Option<Instant>,
    terminal: bool,
    next_percentage: u64,
    finished: bool,
}

impl DownloadProgress {
    fn new(label: String, total: Option<u64>) -> Self {
        let mut progress = Self {
            label,
            total,
            started: Instant::now(),
            last_draw: None,
            terminal: std::io::stderr().is_terminal(),
            next_percentage: 10,
            finished: false,
        };
        progress.draw(0, false);
        progress
    }

    fn update(&mut self, downloaded: u64) {
        let now = Instant::now();
        let should_draw = if self.terminal {
            self.last_draw
                .is_none_or(|last| now.duration_since(last) >= Duration::from_millis(100))
        } else if let Some(total) = self.total {
            let percentage = percentage(downloaded, total);
            if percentage >= 100 {
                false
            } else if percentage >= self.next_percentage {
                self.next_percentage = ((percentage / 10) + 1) * 10;
                true
            } else {
                false
            }
        } else {
            self.last_draw
                .is_none_or(|last| now.duration_since(last) >= Duration::from_secs(5))
        };
        if should_draw {
            self.draw(downloaded, false);
        }
    }

    fn finish(&mut self, downloaded: u64) {
        self.draw(downloaded, true);
        self.finished = true;
    }

    fn draw(&mut self, downloaded: u64, force_newline: bool) {
        let line =
            format_progress_line(&self.label, downloaded, self.total, self.started.elapsed());
        if self.terminal && !force_newline {
            eprint!("\r{line}");
            let _ = std::io::stderr().flush();
        } else if self.terminal {
            eprintln!("\r{line}");
        } else {
            eprintln!("{line}");
        }
        self.last_draw = Some(Instant::now());
    }
}

impl Drop for DownloadProgress {
    fn drop(&mut self) {
        if self.terminal && !self.finished {
            eprintln!();
        }
    }
}

fn format_progress_line(
    label: &str,
    downloaded: u64,
    total: Option<u64>,
    elapsed: Duration,
) -> String {
    let elapsed_seconds = elapsed.as_secs_f64();
    let bytes_per_second = if elapsed_seconds > 0.0 {
        downloaded as f64 / elapsed_seconds
    } else {
        0.0
    };
    let elapsed = format_duration(elapsed);

    if let Some(total) = total {
        let percentage = percentage(downloaded, total);
        let filled = ((downloaded.min(total) as u128 * PROGRESS_WIDTH as u128)
            / total.max(1) as u128) as usize;
        let bar = if filled >= PROGRESS_WIDTH {
            "=".repeat(PROGRESS_WIDTH)
        } else {
            format!(
                "{}>{}",
                "=".repeat(filled),
                ".".repeat(PROGRESS_WIDTH - filled - 1)
            )
        };
        let eta = if bytes_per_second > 0.0 && downloaded < total {
            format_duration(Duration::from_secs(
                ((total - downloaded) as f64 / bytes_per_second).ceil() as u64,
            ))
        } else if downloaded >= total {
            "00:00".to_owned()
        } else {
            "--:--".to_owned()
        };
        format!(
            "Downloading {label} [{bar}] {percentage:>3}% {}/{} {}/s elapsed {elapsed} ETA {eta}",
            format_bytes(downloaded),
            format_bytes(total),
            format_bytes(bytes_per_second as u64)
        )
    } else {
        format!(
            "Downloading {label} [size unknown] {} {}/s elapsed {elapsed} ETA unknown",
            format_bytes(downloaded),
            format_bytes(bytes_per_second as u64)
        )
    }
}

fn percentage(downloaded: u64, total: u64) -> u64 {
    ((downloaded.min(total) as u128 * 100) / total.max(1) as u128) as u64
}

fn format_bytes(bytes: u64) -> String {
    const MIB: f64 = 1024.0 * 1024.0;
    const KIB: f64 = 1024.0;
    if bytes as f64 >= MIB {
        format!("{:.1} MiB", bytes as f64 / MIB)
    } else if bytes as f64 >= KIB {
        format!("{:.1} KiB", bytes as f64 / KIB)
    } else {
        format!("{bytes} B")
    }
}

fn format_duration(duration: Duration) -> String {
    let seconds = duration.as_secs();
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let seconds = seconds % 60;
    if hours > 0 {
        format!("{hours:02}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

fn validate_executable(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .context("could not read current directory")?
            .join(path)
    };
    let metadata =
        fs::metadata(&path).with_context(|| format!("could not inspect {}", path.display()))?;
    if !metadata.is_file() {
        bail!("{} is not a file", path.display());
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            bail!("{} is not executable", path.display());
        }
    }

    Ok(path.canonicalize().unwrap_or(path))
}

fn parse_uv_version(output: &str) -> Result<UvBuild> {
    let mut parts = output.split_whitespace();
    if parts.next() != Some("uv") {
        bail!("unexpected UV version output: {}", output.trim());
    }
    let version = parts
        .next()
        .ok_or_else(|| anyhow!("UV version output did not include a version"))?;
    let semantic_version = parse_semantic_version(version)?;
    let target = output
        .split(|character: char| character.is_whitespace() || matches!(character, '(' | ')'))
        .find(|part| {
            part.ends_with("-apple-darwin")
                || part.ends_with("-unknown-linux-gnu")
                || part.ends_with("-unknown-linux-musl")
                || part.ends_with("-pc-windows-msvc")
        })
        .map(str::to_owned);
    Ok(UvBuild {
        version: version.to_owned(),
        target,
        semantic_version,
    })
}

fn parse_semantic_version(version: &str) -> Result<(u64, u64, u64)> {
    let components = version
        .split('.')
        .take(3)
        .map(|component| {
            let digits = component
                .chars()
                .take_while(|character| character.is_ascii_digit())
                .collect::<String>();
            if digits.is_empty() {
                bail!("invalid UV version component {component:?}");
            }
            digits
                .parse::<u64>()
                .with_context(|| format!("invalid UV version component {component:?}"))
        })
        .collect::<Result<Vec<_>>>()?;
    if components.len() != 3 {
        bail!("UV version must contain major, minor, and patch components");
    }
    Ok((components[0], components[1], components[2]))
}

fn release_for_current_target() -> Result<Option<UvRelease>> {
    current_target().map(release_for_target).transpose()
}

pub fn current_target() -> Option<&'static str> {
    let target_environment = if cfg!(target_env = "musl") {
        "musl"
    } else if cfg!(target_env = "gnu") {
        "gnu"
    } else if cfg!(target_env = "msvc") {
        "msvc"
    } else {
        ""
    };
    target_for(env::consts::OS, env::consts::ARCH, target_environment)
}

fn target_for(os: &str, architecture: &str, target_environment: &str) -> Option<&'static str> {
    match (os, architecture, target_environment) {
        ("macos", "aarch64", _) => Some("aarch64-apple-darwin"),
        ("macos", "x86_64", _) => Some("x86_64-apple-darwin"),
        ("linux", "aarch64", "musl") => Some("aarch64-unknown-linux-musl"),
        ("linux", "x86_64", "musl") => Some("x86_64-unknown-linux-musl"),
        ("linux", "aarch64", _) => Some("aarch64-unknown-linux-gnu"),
        ("linux", "x86_64", _) => Some("x86_64-unknown-linux-gnu"),
        ("windows", "aarch64", _) => Some("aarch64-pc-windows-msvc"),
        ("windows", "x86_64", _) => Some("x86_64-pc-windows-msvc"),
        _ => None,
    }
}

fn release_for_target(target: &str) -> Result<UvRelease> {
    let mut matched = None;
    for (line_number, line) in RELEASE_MANIFEST.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields = line.split_whitespace().collect::<Vec<_>>();
        if fields.len() != 4 {
            bail!(
                "invalid UV release manifest line {}: expected target, version, archive SHA-256, and executable SHA-256",
                line_number + 1
            );
        }
        if fields[0] != target {
            continue;
        }
        if matched.is_some() {
            bail!("duplicate UV release manifest entry for {target}");
        }
        for (label, digest) in [("archive", fields[2]), ("executable", fields[3])] {
            if digest.len() != 64
                || !digest
                    .chars()
                    .all(|character| character.is_ascii_hexdigit())
            {
                bail!("invalid UV {label} SHA-256 in release manifest for {target}");
            }
        }
        parse_semantic_version(fields[1])?;
        matched = Some(UvRelease {
            target: fields[0].to_owned(),
            version: fields[1].to_owned(),
            archive_sha256: fields[2].to_ascii_lowercase(),
            executable_sha256: fields[3].to_ascii_lowercase(),
        });
    }
    matched.ok_or_else(|| anyhow!("UV release manifest does not support target {target}"))
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)
        .with_context(|| format!("could not open {} for checksum", path.display()))?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .with_context(|| format!("could not read {} for checksum", path.display()))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_pins_supported_platforms() {
        let arm = release_for_target("aarch64-apple-darwin").expect("read arm release");
        assert_eq!(arm.version, "0.12.8");
        assert_eq!(
            arm.archive_sha256,
            "8ce083658dbff20143607ca7af8e0c1d64b6fd7bf03a5cdcb62bf3d47d991b5f"
        );
        assert_eq!(
            arm.executable_sha256,
            "e053a85d61ffa0790b564afe56bb7314d69334ff33b77175df03f9de2f11dd46"
        );
        assert_eq!(
            arm.download_url(),
            "https://github.com/astral-sh/uv/releases/download/0.12.8/uv-aarch64-apple-darwin.tar.gz"
        );

        let intel = release_for_target("x86_64-apple-darwin").expect("read Intel release");
        assert_eq!(intel.version, "0.12.8");
        assert_eq!(
            intel.archive_sha256,
            "bfcd4407de99e0a2c1904df0902fa1795653d4edd145358e6561527e746a4f16"
        );
        assert_eq!(
            intel.executable_sha256,
            "c7dd0293072831b718924b82f390bb2d179666cec739118eb781a755c5504334"
        );

        for target in [
            "aarch64-unknown-linux-gnu",
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-musl",
            "x86_64-unknown-linux-musl",
            "aarch64-pc-windows-msvc",
            "x86_64-pc-windows-msvc",
        ] {
            let release = release_for_target(target).expect("read portable release");
            assert_eq!(release.version, "0.12.8");
            assert_eq!(release.archive_sha256.len(), 64);
            assert_eq!(release.executable_sha256.len(), 64);
        }
        let windows = release_for_target("x86_64-pc-windows-msvc").expect("read Windows release");
        assert_eq!(windows.archive_kind(), ArchiveKind::Zip);
        assert_eq!(windows.archive_member(), "uv.exe");
        assert_eq!(
            windows.download_url(),
            "https://github.com/astral-sh/uv/releases/download/0.12.8/uv-x86_64-pc-windows-msvc.zip"
        );
        assert!(release_for_target("powerpc-apple-darwin").is_err());
    }

    #[test]
    fn selects_uv_asset_for_supported_compile_targets() {
        assert_eq!(
            target_for("macos", "aarch64", ""),
            Some("aarch64-apple-darwin")
        );
        assert_eq!(
            target_for("linux", "x86_64", "gnu"),
            Some("x86_64-unknown-linux-gnu")
        );
        assert_eq!(
            target_for("linux", "aarch64", "musl"),
            Some("aarch64-unknown-linux-musl")
        );
        assert_eq!(
            target_for("windows", "x86_64", "gnu"),
            Some("x86_64-pc-windows-msvc")
        );
        assert_eq!(target_for("freebsd", "x86_64", ""), None);
    }

    #[test]
    fn parses_uv_version_and_target() {
        let build = parse_uv_version("uv 0.12.8 (68209e5c6 2026-08-31 aarch64-apple-darwin)\n")
            .expect("parse official version output");
        assert_eq!(build.version, "0.12.8");
        assert_eq!(build.semantic_version, (0, 12, 8));
        assert_eq!(build.target.as_deref(), Some("aarch64-apple-darwin"));

        assert!(parse_uv_version("uv unknown").is_err());
        assert!(parse_uv_version("not-uv 0.12.8").is_err());
    }

    #[test]
    fn extracts_only_the_uv_executable_from_tar_gz() {
        use flate2::{Compression, write::GzEncoder};

        let directory = tempfile::tempdir().expect("create temp directory");
        let archive_path = directory.path().join("uv.tar.gz");
        let file = fs::File::create(&archive_path).expect("create tar archive");
        let encoder = GzEncoder::new(file, Compression::default());
        let mut archive = tar::Builder::new(encoder);
        let payload = b"portable tar uv";
        let mut header = tar::Header::new_gnu();
        header.set_path("uv-x86_64-unknown-linux-gnu/uv").unwrap();
        header.set_size(payload.len() as u64);
        header.set_mode(0o755);
        header.set_cksum();
        archive
            .append(&header, &payload[..])
            .expect("append uv member");
        let encoder = archive.into_inner().expect("finish tar archive");
        encoder.finish().expect("finish gzip archive");

        let release = release_for_target("x86_64-unknown-linux-gnu").expect("read Linux release");
        let extracted =
            extract_uv(&release, &archive_path, directory.path()).expect("extract tar uv");
        assert_eq!(fs::read(extracted.path()).unwrap(), payload);
    }

    #[test]
    fn extracts_uv_executable_from_windows_zip() {
        use zip::write::SimpleFileOptions;

        let directory = tempfile::tempdir().expect("create temp directory");
        let archive_path = directory.path().join("uv.zip");
        let file = fs::File::create(&archive_path).expect("create zip archive");
        let mut archive = zip::ZipWriter::new(file);
        archive
            .start_file(
                "uv.exe",
                SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated),
            )
            .expect("start uv member");
        archive.write_all(b"portable windows uv").unwrap();
        archive.finish().expect("finish zip archive");

        let release = release_for_target("x86_64-pc-windows-msvc").expect("read Windows release");
        let extracted =
            extract_uv(&release, &archive_path, directory.path()).expect("extract zip uv");
        assert_eq!(fs::read(extracted.path()).unwrap(), b"portable windows uv");
    }

    #[test]
    fn progress_line_includes_bar_rate_elapsed_and_eta() {
        let line = format_progress_line(
            "uv 0.12.8 (aarch64-apple-darwin)",
            5 * 1024 * 1024,
            Some(10 * 1024 * 1024),
            Duration::from_secs(2),
        );
        assert!(line.contains("[===============>..............]"));
        assert!(line.contains(" 50%"));
        assert!(line.contains("2.5 MiB/s"));
        assert!(line.contains("elapsed 00:02"));
        assert!(line.contains("ETA 00:02"));
    }

    #[test]
    fn hashes_files_without_loading_them_whole() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("payload");
        fs::write(&path, b"toolframe\n").expect("write payload");
        assert_eq!(
            sha256_file(&path).expect("hash payload"),
            "1b7f64127ed303e96233451d3be76798898167ee182fe1a435c64ec01aac780f"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn accepts_only_the_exact_version_and_target_for_a_pinned_release() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempfile::tempdir().expect("create temp directory");
        let path = directory.path().join("uv");
        fs::write(
            &path,
            "#!/bin/sh\necho 'uv 0.12.8 (test 2026-09-03 aarch64-apple-darwin)'\n",
        )
        .expect("write fake uv");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755))
            .expect("make fake uv executable");
        let mut release = release_for_target("aarch64-apple-darwin").expect("read release");
        release.executable_sha256 = sha256_file(&path).expect("hash exact fake uv");
        let resolved = inspect_candidate(&path, Some(&release), UvSource::Managed)
            .await
            .expect("accept exact fake uv");
        assert_eq!(resolved.version, "0.12.8");

        fs::write(
            &path,
            "#!/bin/sh\necho 'uv 0.12.7 (test 2026-09-03 aarch64-apple-darwin)'\n",
        )
        .expect("write old fake uv");
        release.executable_sha256 = sha256_file(&path).expect("hash old fake uv");
        assert!(
            inspect_candidate(&path, Some(&release), UvSource::Managed)
                .await
                .unwrap_err()
                .to_string()
                .contains("does not match pinned UV")
        );

        fs::write(
            &path,
            "#!/bin/sh\necho 'uv 0.12.8 (test 2026-09-03 x86_64-apple-darwin)'\n",
        )
        .expect("write wrong-target fake uv");
        release.executable_sha256 = sha256_file(&path).expect("hash wrong-target fake uv");
        assert!(
            inspect_candidate(&path, Some(&release), UvSource::Managed)
                .await
                .unwrap_err()
                .to_string()
                .contains("expected aarch64-apple-darwin")
        );
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    async fn offline_mode_does_not_download_a_missing_managed_uv() {
        let directory = tempfile::tempdir().expect("create temp directory");
        let error = resolve(None, directory.path(), true)
            .await
            .expect_err("offline resolution should fail")
            .to_string();
        assert!(error.contains("--offline prevents downloading"));
        assert!(!directory.path().join("uv").exists());
    }
}
