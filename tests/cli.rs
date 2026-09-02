use std::{fs, path::PathBuf, process::Command};

fn copied_binary(directory: &tempfile::TempDir) -> PathBuf {
    let source = PathBuf::from(env!("CARGO_BIN_EXE_lite-mcp"));
    let destination = directory.path().join("lite-mcp");
    fs::copy(&source, &destination).expect("copy lite-mcp binary");
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
    let output = Command::new(env!("CARGO_BIN_EXE_lite-mcp"))
        .arg("check")
        .arg("--format")
        .arg("json")
        .arg("--database")
        .arg(&database)
        .arg("--migrations-dir")
        .arg(directory.path().join("missing-migrations"))
        .arg("--tools-dir")
        .arg(directory.path().join("missing-tools"))
        .arg("--dashboard-dir")
        .arg(directory.path().join("missing-dashboard"))
        .arg("--workspace-dir")
        .arg(directory.path().join("missing-workspace"))
        .arg("--runtime-dir")
        .arg(directory.path().join("runtime"))
        .output()
        .expect("run lite-mcp check");

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
            .contains("migration")
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
                    .arg("--migrations-dir")
                    .arg(&missing);
            }
            "invoke" => {
                command
                    .arg("invoke")
                    .arg("missing_tool")
                    .arg("--json")
                    .arg("{}")
                    .arg("--migrations-dir")
                    .arg(&missing);
            }
            "scaffold" => {
                command
                    .arg("scaffold")
                    .arg("tool")
                    .arg("sample_tool")
                    .arg("--tools-dir")
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
    }
}
