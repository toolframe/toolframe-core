use std::{fs, path::PathBuf, process::Command};

fn copied_binary(directory: &tempfile::TempDir) -> PathBuf {
    let source = PathBuf::from(env!("CARGO_BIN_EXE_toolframe"));
    let destination = directory.path().join("toolframe");
    fs::copy(&source, &destination).expect("copy toolframe binary");
    fs::set_permissions(
        &destination,
        fs::metadata(&source)
            .expect("read source binary metadata")
            .permissions(),
    )
    .expect("preserve executable permissions");
    destination
}

#[test]
fn json_check_failure_is_machine_readable_and_does_not_create_data_db() {
    let directory = tempfile::tempdir().expect("create temp directory");
    let database = directory.path().join("data.db");
    let output = Command::new(env!("CARGO_BIN_EXE_toolframe"))
        .arg("check")
        .arg("--format")
        .arg("json")
        .arg("--database")
        .arg(&database)
        .arg("--domains-dir")
        .arg(directory.path().join("missing-domains"))
        .arg("--app-dir")
        .arg(directory.path().join("missing-app"))
        .arg("--workspace-dir")
        .arg(directory.path().join("missing-workspace"))
        .arg("--runtime-dir")
        .arg(directory.path().join("runtime"))
        .output()
        .expect("run toolframe check");

    assert!(!output.status.success());
    assert!(output.stderr.is_empty());
    let report: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("parse JSON failure");
    assert_eq!(report["version"], 1);
    assert_eq!(report["ok"], false);
    assert!(
        report["error"]["message"]
            .as_str()
            .unwrap()
            .contains("domains")
    );
    assert!(!database.exists());
}

#[test]
fn non_server_commands_do_not_install_the_agent_skill() {
    for command_name in ["check", "invoke", "scaffold"] {
        let directory = tempfile::tempdir().expect("create temp directory");
        let executable = copied_binary(&directory);
        let missing = directory.path().join("missing");
        let mut command = Command::new(executable);

        match command_name {
            "check" => {
                command
                    .arg("check")
                    .arg("--format")
                    .arg("json")
                    .arg("--domains-dir")
                    .arg(&missing);
            }
            "invoke" => {
                command
                    .arg("invoke")
                    .arg("missing_tool")
                    .arg("--json")
                    .arg("{}")
                    .arg("--domains-dir")
                    .arg(&missing);
            }
            "scaffold" => {
                command
                    .arg("scaffold")
                    .arg("tool")
                    .arg("sample_domain")
                    .arg("sample_tool")
                    .arg("--domains-dir")
                    .arg(&missing);
            }
            _ => unreachable!(),
        }

        let output = command.output().expect("run non-server command");
        assert!(
            !output.status.success(),
            "fixture should fail before doing useful work for {command_name}"
        );
        assert!(
            !directory.path().join(".agents").exists(),
            "{command_name} must not install the production skill"
        );
        assert!(!directory.path().join("app").exists());
        assert!(!directory.path().join("domains").exists());
        assert!(!directory.path().join("APP_CONTEXT.md").exists());
    }
}

#[test]
fn export_skill_is_offline_create_only_and_contains_production_files() {
    let directory = tempfile::tempdir().expect("create test directory");
    let executable = copied_binary(&directory);
    let destination = directory.path().join("fresh-skill");
    let output = Command::new(&executable)
        .args(["--offline", "export-skill"])
        .arg(&destination)
        .output()
        .expect("export skill");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read(destination.join("SKILL.md")).unwrap(),
        fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("skills/toolframe-business-app/SKILL.md")
        )
        .unwrap()
    );
    assert!(destination.join("references/plugins-and-data.md").is_file());
    assert!(!destination.join("evals").exists());
    assert!(!directory.path().join(".agents").exists());
    assert!(!directory.path().join(".toolframe").exists());
    assert!(!directory.path().join("data").exists());
    fs::write(destination.join("SKILL.md"), "personal changes").unwrap();
    let again = Command::new(&executable)
        .arg("export-skill")
        .arg(&destination)
        .output()
        .unwrap();
    assert!(!again.status.success());
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "personal changes"
    );
    let file = directory.path().join("existing-file");
    fs::write(&file, "keep").unwrap();
    assert!(
        !Command::new(&executable)
            .arg("export-skill")
            .arg(&file)
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(fs::read_to_string(file).unwrap(), "keep");
}

#[cfg(unix)]
#[test]
fn export_skill_rejects_a_symlink_target() {
    let directory = tempfile::tempdir().unwrap();
    let executable = copied_binary(&directory);
    let original = directory.path().join("personal");
    fs::create_dir(&original).unwrap();
    fs::write(original.join("SKILL.md"), "keep").unwrap();
    let link = directory.path().join("linked-skill");
    std::os::unix::fs::symlink(&original, &link).unwrap();
    let output = Command::new(executable)
        .arg("export-skill")
        .arg(&link)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(
        fs::read_to_string(original.join("SKILL.md")).unwrap(),
        "keep"
    );
    assert!(fs::symlink_metadata(link).unwrap().file_type().is_symlink());
}
