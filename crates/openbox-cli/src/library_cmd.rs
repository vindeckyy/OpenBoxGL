//! Command-line library maintenance: check, repair, and migrate.
//! Mirrors `pkg/state/library_cli.py` with identical exit codes (0/1/2/3/4) and stdout JSON.

use std::path::{Path, PathBuf};

use openbox_platform::resolved_data_dir;
use openbox_store::{
    JsonStateStore, PRIMARY_NAME, ROLLBACK_REMOVED, check_library, repair_library, server_running,
};
use serde_json::json;

pub const LIBRARY_COMMANDS: &[&str] = &["--check-library", "--repair-library", "--migrate-library"];
pub const REMOVED_ROLLBACK_CMD: &str = "--rollback-library";

pub const EXIT_OK: i32 = 0;
#[allow(dead_code)]
pub const EXIT_WARNINGS: i32 = 1;
#[allow(dead_code)]
pub const EXIT_ERRORS: i32 = 2;
pub const EXIT_REFUSED: i32 = 3;
pub const EXIT_USAGE: i32 = 4;

pub const USAGE: &str =
    "usage: openbox --check-library | --repair-library [--refresh-backup] | --migrate-library";

pub fn is_library_command(argv: &[String]) -> bool {
    argv.iter()
        .any(|arg| LIBRARY_COMMANDS.contains(&arg.as_str()) || arg == REMOVED_ROLLBACK_CMD)
}

pub fn run(argv: &[String], data_dir: Option<&Path>) -> i32 {
    if argv.iter().any(|a| a == REMOVED_ROLLBACK_CMD) {
        eprintln!("{ROLLBACK_REMOVED}");
        return EXIT_USAGE;
    }
    let commands: Vec<&str> = argv
        .iter()
        .filter(|a| LIBRARY_COMMANDS.contains(&a.as_str()))
        .map(String::as_str)
        .collect();
    if commands.len() != 1 {
        eprintln!("{USAGE}");
        return EXIT_USAGE;
    }
    let command = commands[0];
    let directory: PathBuf = data_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(resolved_data_dir);

    match command {
        "--check-library" => {
            let report = check_library(&directory);
            let exit_code = report
                .get("exit_code")
                .and_then(serde_json::Value::as_i64)
                .unwrap_or(0) as i32;
            println!(
                "{}",
                serde_json::to_string_pretty(&report).unwrap_or_else(|_| "{}".to_owned())
            );
            exit_code
        }
        "--repair-library" => {
            let refresh_backup = argv.iter().any(|a| a == "--refresh-backup");
            match repair_library(&directory, refresh_backup) {
                Ok(report) => {
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&report).unwrap_or_else(|_| "{}".to_owned())
                    );
                    EXIT_OK
                }
                Err(err) => {
                    eprintln!("{err}");
                    EXIT_REFUSED
                }
            }
        }
        "--migrate-library" => {
            if server_running(&directory) {
                eprintln!("OpenBox is running. Quit OpenBox before migrating the library.");
                return EXIT_REFUSED;
            }
            let store = JsonStateStore::new(directory.join(PRIMARY_NAME));
            match store.migrate_primary_to_v7() {
                Ok((state, report)) => {
                    let payload = json!({
                        "migrated": state.is_some(),
                        "report": report,
                    });
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_owned())
                    );
                    EXIT_OK
                }
                Err(err) => {
                    eprintln!("{err}");
                    EXIT_REFUSED
                }
            }
        }
        _ => {
            eprintln!("{USAGE}");
            EXIT_USAGE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn library_cli_exit_codes() {
        let dir = tempfile::tempdir().unwrap();
        // Missing library => healthy empty report or exit 0
        assert_eq!(
            run(&["--rollback-library".to_owned()], Some(dir.path())),
            EXIT_USAGE
        );
        assert_eq!(
            run(
                &["--check-library".to_owned(), "--repair-library".to_owned()],
                Some(dir.path())
            ),
            EXIT_USAGE
        );
        let store = JsonStateStore::new(dir.path().join(PRIMARY_NAME));
        store.save(&openbox_core::default_state()).unwrap();
        assert_eq!(
            run(&["--check-library".to_owned()], Some(dir.path())),
            EXIT_OK
        );
        assert_eq!(
            run(&["--repair-library".to_owned()], Some(dir.path())),
            EXIT_OK
        );
        assert_eq!(
            run(&["--migrate-library".to_owned()], Some(dir.path())),
            EXIT_OK
        );
    }
}
