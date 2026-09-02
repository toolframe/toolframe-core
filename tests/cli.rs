use std::process::Command;

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
