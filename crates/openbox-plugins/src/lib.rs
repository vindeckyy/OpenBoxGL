//! Plugin API v2 host, bubblewrap sandbox command builder, JSON Lines runner client,
//! bundled catalog, read-only host services, and capability plugin bridges.

pub mod catalog;
pub mod discovery;
pub mod host;
pub mod providers;
pub mod services;
pub mod stats;
pub mod storefront;

pub use host::{
    PLUGIN_API_RETIRED, PLUGIN_API_RETIRED_MESSAGE, PLUGIN_API_VERSION,
    PLUGIN_API_VERSION_SUPPORTED, PluginError,
};

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use tempfile::TempDir;

    fn write_sample_plugin(dir: &std::path::Path, id: &str, api_version: Option<i64>) {
        let pkg = dir.join(id);
        fs::create_dir_all(&pkg).unwrap();
        let mut manifest = json!({
            "id": id,
            "name": "Sample Plugin",
            "version": "2.0.0",
            "entry": "plugin.py",
            "hooks": ["command"],
            "commands": [{"id": "hello", "label": "Hello"}],
            "permissions": ["network"],
            "settings": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "default": "https://example.com"},
                    "token": {"type": "string", "format": "password"}
                }
            }
        });
        if let Some(v) = api_version {
            manifest
                .as_object_mut()
                .unwrap()
                .insert("api_version".into(), json!(v));
        }
        fs::write(
            pkg.join("plugin.json"),
            serde_json::to_string_pretty(&manifest).unwrap(),
        )
        .unwrap();
        fs::write(
            pkg.join("plugin.py"),
            "def command(payload):\n    return {'notification': {'level': 'info', 'message': 'hi ' + str(payload.get('command', ''))}}\n",
        )
        .unwrap();
    }

    #[test]
    fn api_version_1_and_missing_rejected_with_plugin_api_retired() {
        let tmp = TempDir::new().unwrap();
        write_sample_plugin(tmp.path(), "demo.missing", None);
        let err = host::read_manifest(&tmp.path().join("demo.missing")).unwrap_err();
        assert_eq!(err.code(), Some(PLUGIN_API_RETIRED));

        write_sample_plugin(tmp.path(), "demo.v1", Some(1));
        let err1 = host::read_manifest(&tmp.path().join("demo.v1")).unwrap_err();
        assert_eq!(err1.code(), Some(PLUGIN_API_RETIRED));

        // list_plugins surfaces invalid plugins with code=PLUGIN_API_RETIRED
        let listed = host::list_plugins(tmp.path());
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0]["valid"], false);
        assert_eq!(listed[0]["code"], PLUGIN_API_RETIRED);
    }

    #[test]
    fn framed_checksum_trust_permissions_settings_data_and_credentials() {
        let tmp = TempDir::new().unwrap();
        write_sample_plugin(tmp.path(), "demo.v2", Some(2));
        let manifest = host::read_manifest(&tmp.path().join("demo.v2")).unwrap();
        assert_eq!(manifest["api_version"], 2);

        // Trust binding + legacy checksum retrust detection
        let legacy = host::legacy_plugin_checksum(tmp.path(), "demo.v2").unwrap();
        let framed = host::plugin_checksum(tmp.path(), "demo.v2").unwrap();
        assert_ne!(legacy, framed);

        host::set_plugin_trust(tmp.path(), "demo.v2", true).unwrap();
        let st = host::plugin_trust_status(tmp.path(), "demo.v2");
        assert_eq!(st["trusted"], true);
        assert_eq!(st["checksum_matches"], true);
        assert_eq!(st["retrust"], false);

        // Tampering invalidates trust
        fs::write(tmp.path().join("demo.v2/plugin.py"), "# modified\n").unwrap();
        let st2 = host::plugin_trust_status(tmp.path(), "demo.v2");
        assert_eq!(st2["trusted"], false);
        assert_eq!(st2["checksum_matches"], false);

        // Permissions cannot exceed declared
        assert!(host::set_plugin_permissions(tmp.path(), "demo.v2", &["fs".into()]).is_err());
        let granted =
            host::set_plugin_permissions(tmp.path(), "demo.v2", &["network".into()]).unwrap();
        assert_eq!(granted, vec!["network".to_owned()]);

        // Settings hide password fields in public view
        host::set_plugin_settings(
            tmp.path(),
            "demo.v2",
            &json!({"host": "https://a.test", "token": "s3cr3t"}),
        )
        .unwrap();
        let pub_s = host::get_plugin_settings_public(tmp.path(), "demo.v2").unwrap();
        assert_eq!(pub_s["values"]["host"], "https://a.test");
        assert!(pub_s["values"].get("token").is_none());
        assert_eq!(pub_s["secret_set"]["token"], true);

        // Data depth bound (max 4)
        host::set_plugin_data(tmp.path(), "demo.v2", &json!({"a": {"b": {"c": 1}}})).unwrap();
        assert!(
            host::set_plugin_data(
                tmp.path(),
                "demo.v2",
                &json!({"a": {"b": {"c": {"d": {"e": 1}}}}})
            )
            .is_err()
        );

        // Credentials never leak in summary
        let keys =
            host::set_plugin_credentials(tmp.path(), "demo.v2", &json!({"api_key": "xyz"}), &[])
                .unwrap();
        assert_eq!(keys, vec!["api_key".to_owned()]);
        let summary = host::plugin_credentials_summary(tmp.path(), "demo.v2");
        assert_eq!(summary["keys"], json!(["api_key"]));
        assert_eq!(summary["set"]["api_key"], true);
        assert!(!summary.to_string().contains("xyz"));
    }

    #[test]
    fn sandbox_data_dir_mask_and_storefront_launch_guard() {
        assert!(!host::needs_data_dir_mask(
            "/home/user/.local/share/openbox"
        ));
        assert!(!host::needs_data_dir_mask("/tmp/openbox"));
        assert!(host::needs_data_dir_mask("/srv/openbox-data"));
        assert!(host::needs_data_dir_mask("/var/lib/openbox"));

        assert!(storefront::clean_launch("/bin/sh -c id").is_err());
        assert!(storefront::clean_launch("python3 -c 'print(1)'").is_err());
        assert!(storefront::clean_launch("/usr/bin/curl https://example.com").is_err());
        assert!(storefront::clean_launch("/usr/bin/steam steam://rungameid/10").is_ok());
    }

    #[test]
    fn bundled_catalog_and_readonly_services_strip_paths() {
        let cat = catalog::load_local_catalog(None);
        assert!(!cat.is_empty());

        let state = json!({
            "games": [
                {
                    "game_id": "g1",
                    "name": "Chrono Trigger",
                    "platform": "SNES",
                    "path": "/home/user/roms/ct.sfc",
                    "playtime_seconds": 3600,
                    "favorite": true,
                    "ra_achievements_earned": 5,
                    "ra_achievements_total": 10
                }
            ]
        });
        let q = services::handle_service(
            "library.query",
            Some(&json!({"query": "chrono"})),
            &state,
            Some(&json!(1)),
            None,
        )
        .unwrap();
        assert_eq!(q["games"].as_array().unwrap().len(), 1);
        assert!(q["games"][0].get("path").is_none());

        let prof = services::handle_service("profile", None, &state, None, None).unwrap();
        assert_eq!(prof["games"], 1);
        assert_eq!(prof["playtime_seconds"], 3600);
        assert_eq!(prof["achievements_earned"], 5);
    }

    #[test]
    fn native_v2_executable_hook_runs_without_python() {
        let tmp = TempDir::new().unwrap();
        let pkg = tmp.path().join("demo.native");
        fs::create_dir_all(&pkg).unwrap();
        fs::write(
            pkg.join("plugin.json"),
            serde_json::to_string_pretty(&json!({
                "id": "demo.native",
                "name": "Native v2",
                "version": "1.0.0",
                "api_version": 2,
                "entry": "run.sh",
                "hooks": ["command"],
                "commands": [{"id": "ping", "label": "Ping"}]
            }))
            .unwrap(),
        )
        .unwrap();
        let script = pkg.join("run.sh");
        fs::write(
            &script,
            "#!/bin/sh\nread line\necho '{\"id\":\"1\",\"type\":\"result\",\"result\":{\"ok\":true,\"data\":{\"ran\":1}}}'\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        }
        host::set_plugin_trust(tmp.path(), "demo.native", true).unwrap();
        let (res, err) = host::run_plugin_hook(
            tmp.path(),
            "demo.native",
            "command",
            &json!({"command": "ping"}),
            Some(5),
            None,
            None,
            None,
        );
        assert_eq!(err, "");
        let obj = res.expect("result object");
        assert_eq!(obj["ok"], true);
        // Data bag persisted automatically
        let bag = host::plugin_data_values(tmp.path(), "demo.native");
        assert_eq!(bag["ran"], 1);
    }
}
