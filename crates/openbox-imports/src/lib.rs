//! OpenBox catalog, storefront/folder/XML importers, bulk edit, platform admin, and Setup Center.

pub mod arcade_import;
pub mod bulk_edit;
pub mod catalog;
pub mod esde_import;
pub mod exclusions;
pub mod folder_import;
pub mod identity;
pub mod importers;
pub mod launchbox_import;
pub mod legacy_import;
pub mod platform_admin;
pub mod setup_preview;
pub mod state_imports;
pub mod storefront;
pub mod storefront_windows;

pub use arcade_import::{import_arcade, load_catalog, parse_catalog_bytes};
pub use bulk_edit::{
    BULK_EDIT_FIELDS, BULK_WIZARD_FIELDS, DEFAULT_PLATFORM_CATEGORIES, LIST_COLUMNS_DEFAULT,
    bulk_wizard_changes, custom_field_defs, find_replace, import_loose_arcade,
    import_xbox360_folder, normalize_custom_fields, pick_best_rom, platform_categories,
    rank_rom_group, resolve_vita_title,
};
pub use catalog::{
    MAX_PRICE_CENTS, MAX_TAG_LENGTH, MAX_TAGS, MEDIA_FIELDS, PROGRESS, apply_progress_automation,
    apply_tag_changes, bulk_update, canon_progress, clean_currency, clean_price_cents,
    clean_user_rating, display_progress, game_media_paths, is_valid_progress,
    normalize_manual_sessions, normalize_notes, normalize_tags, parse_timestamp,
    progress_suggest_due, related_game_ids, tag_counts, total_playtime_seconds, value_to_str,
};
pub use exclusions::{
    add_exclusion, exclusion_key, exclusion_set, filter_imported, list_exclusions, remove_exclusion,
};
pub use folder_import::{
    detect_dependencies, discover_profiles, extensions_set, generate_m3u, group_multi_disc,
    import_multi_platform, import_rpcs3_hdd, import_scummvm, import_vita3k,
    import_with_emulator_choice, parse_cue, parse_m3u, platform_by_extension, recommend_emulators,
};
pub use identity::{
    PC_PLATFORMS, PC_STORE_SOURCES, backfill_source_identity, cross_source_identity,
    detect_duplicate_identities, game_identity_key, normalize_identity, normalize_path_identity,
    normalize_rom_name, normalize_title_identity, source_family, source_identities,
};
pub use importers::{
    heroic_bases, import_epic, import_heroic, import_lutris, import_steam, steam_command,
    steam_libraries, steam_roots, url_open_command, url_opener_command, vdf_values,
};
pub use legacy_import::{import_msu1, import_pico8, import_whdload};
pub use platform_admin::{
    bulk_update_with_media, delete_platform, migrate_game_media, rename_platform, trash_entry_for,
};
pub use setup_preview::{
    DEFAULT_ITEMS_LIMIT, MAX_ITEMS_LIMIT, apply_decisions, commit_preview, compat_build_report,
    compute_summary, create_preview_record, list_preview_items, load_preview, preview_document,
    readiness_checks, revalidate_preview_record, run_scan_job, save_preview,
};
pub use state_imports::{
    INTEGRATION_PROVIDERS, STOREFRONT_SOURCES, clean_tokens, consolidate_existing_games,
    explorer_facets, import_folder_path, launcher_menu_items, masked_tokens, merge_imported_game,
    merge_imported_games, normalize_filter_rules, public_tokens, read_storefront_status,
    storefront_scan_worker, validate_provider, validate_token,
};
pub use storefront::{
    catalog_entries_to_games, catalog_heroic, catalog_lutris, catalog_steam, storefront_catalog,
};
pub use storefront_windows::{import_ea, import_gog, import_ubisoft};

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::tempdir;

    #[test]
    fn catalog_tags_progress_and_bulk_update_parity() {
        assert_eq!(canon_progress(Some(&json!("Unplayed"))), "");
        assert_eq!(canon_progress(Some(&json!("Playing"))), "Playing");
        assert_eq!(display_progress(Some(&json!(""))), "Unplayed");
        assert_eq!(clean_currency(Some(&json!("usd"))).unwrap(), "USD");
        assert!(clean_currency(Some(&json!("US"))).is_err());

        let mut games = vec![
            json!({"game_id": "g1", "name": "Quake", "platform": "PC", "genre": "FPS", "tags": ["Retro"]}),
            json!({"game_id": "g2", "name": "Doom", "platform": "PC", "genre": "FPS"}),
        ];
        let changed = bulk_update(
            &mut games,
            Some(&json!(["g1", "g2"])),
            Some(&json!({"tags_add": ["Classic", "retro"], "user_rating": 5})),
        )
        .unwrap();
        assert_eq!(changed, 2);
        assert_eq!(games[0]["tags"], json!(["Retro", "Classic"]));
        assert_eq!(games[1]["user_rating"], json!(5));
        let counts = tag_counts(&games);
        assert_eq!(counts[0]["count"], json!(2));
    }

    #[test]
    fn launchbox_and_esde_xml_plan_and_apply_roundtrip() {
        let lb_xml = br#"<LaunchBox>
          <Game>
            <ID>lb-100</ID>
            <DatabaseID>42</DatabaseID>
            <Title>Chrono Trigger</Title>
            <ApplicationPath>D:\Roms\SNES\Chrono.sfc</ApplicationPath>
            <Platform>SNES</Platform>
            <ReleaseDate>1995-03-11</ReleaseDate>
          </Game>
        </LaunchBox>"#;
        let parsed = launchbox_import::parse_launchbox_xml_bytes(lb_xml, "<mem>").unwrap();
        assert_eq!(parsed["games"].as_array().unwrap().len(), 1);
        assert_eq!(parsed["games"][0]["year"], json!("1995"));
        let mut state = json!({"games": [], "settings": {}});
        let opts = json!({"path_mappings": {"D:/Roms": "/games"}});
        let plan = launchbox_import::build_import_plan(&parsed, &state, &opts);
        assert_eq!(plan["counts"]["added"], json!(1));
        assert_eq!(
            plan["normalized_games"][0]["path"],
            json!("/games/SNES/Chrono.sfc")
        );
        let res = launchbox_import::apply_import_plan(
            &plan,
            &mut state,
            plan["preview_token"].as_str(),
            parsed["source_digest"].as_str(),
            Some(&parsed),
            &opts,
        )
        .unwrap();
        assert_eq!(res["added"], json!(1));
        assert_eq!(state["games"].as_array().unwrap().len(), 1);

        let esde_xml = br#"<gameList>
          <game>
            <id>9</id>
            <path>/roms/metroid.sfc</path>
            <name>Super Metroid</name>
            <system>SNES</system>
            <rating>0.9</rating>
            <releasedate>19940319T000000</releasedate>
          </game>
        </gameList>"#;
        let eparsed = esde_import::parse_gamelist_bytes(esde_xml, "<mem>", None).unwrap();
        assert_eq!(eparsed["games"][0]["year"], json!("1994"));
        assert_eq!(eparsed["games"][0]["rating"], json!(4.5));
        let eplan = esde_import::build_import_plan(&eparsed, &state, &json!({}));
        let eres = esde_import::apply_import_plan(
            &eplan,
            &mut state,
            eplan["preview_token"].as_str(),
            eparsed["source_digest"].as_str(),
            Some(&eparsed),
            &json!({}),
        )
        .unwrap();
        assert_eq!(eres["added"], json!(1));
        assert_eq!(state["games"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn multi_disc_grouping_and_legacy_importers_work() {
        let dir = tempdir().unwrap();
        let d1 = dir.path().join("Final Fantasy VII (Disc 1).chd");
        let d2 = dir.path().join("Final Fantasy VII (Disc 2).chd");
        let cart = dir.path().join("celeste.p8.png");
        let rom = dir.path().join("Zelda.sfc");
        let msu = dir.path().join("Zelda.msu");
        std::fs::write(&d1, b"1").unwrap();
        std::fs::write(&d2, b"2").unwrap();
        std::fs::write(&cart, b"png").unwrap();
        std::fs::write(&rom, b"rom").unwrap();
        std::fs::create_dir(&msu).unwrap();

        let groups = group_multi_disc(&[d1.clone(), d2.clone()]);
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].len(), 2);

        let mut errs = Vec::new();
        let pico = import_pico8(&dir.path().to_string_lossy(), &mut errs).unwrap();
        assert_eq!(pico.len(), 1);
        assert_eq!(pico[0]["name"], json!("celeste"));

        let msu1 = import_msu1(&dir.path().to_string_lossy(), &mut errs).unwrap();
        assert_eq!(msu1.len(), 1);
        assert_eq!(msu1[0]["msu1"], json!(true));
    }

    #[test]
    fn platform_rename_delete_and_find_replace_work() {
        let dir = tempdir().unwrap();
        let mut state = json!({
            "games": [
                {"game_id": "g1", "name": "Super Mario World", "platform": "SNES", "genre": "Platformer"},
                {"game_id": "g2", "name": "Chrono Trigger", "platform": "SNES", "genre": "RPG"},
            ],
            "profiles": {"SNES": "retroarch {path}"},
            "playlists": [],
            "settings": {},
        });
        let fr = find_replace(
            state["games"].as_array_mut().unwrap(),
            &[json!("g1"), json!("g2")],
            "name",
            "Super ",
            "",
            false,
        )
        .unwrap();
        assert_eq!(fr["matched"], json!(1));
        assert_eq!(state["games"][0]["name"], json!("Mario World"));

        let ren = rename_platform(&mut state, "SNES", "Super Nintendo", false, dir.path()).unwrap();
        assert_eq!(ren["games"], json!(2));
        assert_eq!(
            state["profiles"]["Super Nintendo"],
            json!("retroarch {path}")
        );

        let del = delete_platform(&mut state, "Super Nintendo", false, dir.path()).unwrap();
        assert_eq!(del["games"], json!(2));
        assert_eq!(state["games"].as_array().unwrap().len(), 0);
        assert_eq!(state["trash"].as_array().unwrap().len(), 2);
    }
}
