"""Tests for pkg.state modularization and webapp_state shim re-exports."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
class TestStateSplit(unittest.TestCase):
    def test_pkg_state_imports(self):
        import pkg.state
        import pkg.state.cache
        import pkg.state.launch
        import pkg.state.media_probe
        import pkg.state.registry
        import pkg.state.sse

        self.assertIsNotNone(pkg.state)
        self.assertIsNotNone(pkg.state.cache)
        self.assertIsNotNone(pkg.state.launch)
        self.assertIsNotNone(pkg.state.media_probe)
        self.assertIsNotNone(pkg.state.registry)
        self.assertIsNotNone(pkg.state.sse)

    def test_cache_exports(self):
        from pkg.state.cache import (
            FILE_PROBE_CACHE,
            FILE_PROBE_LOCK,
            MEDIA_EPOCH,
            MEDIA_EPOCH_LOCK,
            PUBLIC_SETTINGS_CACHE,
            PUBLIC_STATE_CACHE,
            STATE_LOCK,
            STATE_VIEW_CACHE,
            bump_media_epoch,
            clear_file_probe_cache,
            load_state_view,
            public_settings,
            public_state,
            public_state_bytes,
            public_state_etag,
            transact_state,
        )

        self.assertIsNotNone(STATE_LOCK)
        self.assertIsNotNone(FILE_PROBE_LOCK)
        self.assertIsNotNone(MEDIA_EPOCH_LOCK)
        self.assertIsInstance(PUBLIC_SETTINGS_CACHE, dict)
        self.assertIsInstance(PUBLIC_STATE_CACHE, dict)
        self.assertIsInstance(STATE_VIEW_CACHE, dict)
        self.assertIsInstance(MEDIA_EPOCH, dict)
        self.assertIsInstance(FILE_PROBE_CACHE, dict)
        self.assertTrue(callable(public_state))
        self.assertTrue(callable(public_state_bytes))
        self.assertTrue(callable(public_state_etag))
        self.assertTrue(callable(public_settings))
        self.assertTrue(callable(load_state_view))
        self.assertTrue(callable(bump_media_epoch))
        self.assertTrue(callable(clear_file_probe_cache))
        self.assertTrue(callable(transact_state))

    def test_media_probe_exports(self):
        from pkg.state.media_probe import (
            FIELDS,
            MEDIA_PATH_FIELDS,
            MEDIA_ROOTS_ENV,
            MEDIA_TYPES_ALL,
            approved_backup_file,
            approved_media_path,
            download_image,
            game_media_paths,
            media_probe_path,
            probe_path,
            safe_document_file,
            sanitize_document_records,
            sanitize_media_path,
            update_steam_metadata,
        )

        self.assertIn("name", FIELDS)
        self.assertIn("cover", MEDIA_TYPES_ALL)
        self.assertIn("cover", MEDIA_PATH_FIELDS)
        self.assertEqual(MEDIA_ROOTS_ENV, "OPENBOX_MEDIA_ROOTS")
        self.assertTrue(callable(approved_media_path))
        self.assertTrue(callable(probe_path))
        self.assertTrue(callable(safe_document_file))
        self.assertTrue(callable(media_probe_path))
        self.assertTrue(callable(sanitize_media_path))
        self.assertTrue(callable(sanitize_document_records))
        self.assertTrue(callable(approved_backup_file))
        self.assertTrue(callable(download_image))
        self.assertTrue(callable(update_steam_metadata))
        self.assertTrue(callable(game_media_paths))

    def test_launch_exports(self):
        from pkg.state.launch import (
            PROCESS_LOCK,
            PROCESSES,
            RUNNING,
            SESSION_EVENTS,
            control_game_session,
            finish_session,
            game_from_payload,
            game_from_query,
            reattach_session,
            reconcile_sessions_on_startup,
            resolve_library_game,
            start_game,
        )

        self.assertIsNotNone(PROCESS_LOCK)
        self.assertIsInstance(RUNNING, dict)
        self.assertIsInstance(PROCESSES, dict)
        self.assertIsInstance(SESSION_EVENTS, list)
        self.assertTrue(callable(start_game))
        self.assertTrue(callable(control_game_session))
        self.assertTrue(callable(finish_session))
        self.assertTrue(callable(reattach_session))
        self.assertTrue(callable(reconcile_sessions_on_startup))
        self.assertTrue(callable(resolve_library_game))
        self.assertTrue(callable(game_from_payload))
        self.assertTrue(callable(game_from_query))

    def test_registry_exports(self):
        from pkg.state.registry import (
            EVENT_SEQUENCE,
            PROCESS_LOCK,
            PROCESSES,
            RUNNING,
            SESSION_EVENTS,
            Session,
        )

        self.assertIsInstance(EVENT_SEQUENCE, int)
        self.assertIsNotNone(PROCESS_LOCK)
        self.assertIsInstance(RUNNING, dict)
        self.assertIsInstance(PROCESSES, dict)
        self.assertIsInstance(SESSION_EVENTS, list)
        self.assertTrue(callable(Session))

    def test_sse_exports(self):
        from pkg.state.sse import (
            EVENT_SEQUENCE,
            EVENT_SUBSCRIBERS,
            EVENT_SUBSCRIBERS_LOCK,
            GZIP_THRESHOLD,
            METADATA_DATABASE,
            broadcast_event,
            emit_notification,
            event_matches,
            get_webhook_dispatcher,
            public_webhook_configs,
            publish_event,
            register_event_subscriber,
            session_event,
            shutdown_webhooks,
            unregister_event_subscriber,
            webhook_configs,
        )

        self.assertEqual(GZIP_THRESHOLD, 1024)
        self.assertIsInstance(EVENT_SEQUENCE, int)
        self.assertIsNotNone(METADATA_DATABASE)
        self.assertIsInstance(EVENT_SUBSCRIBERS, set)
        self.assertIsNotNone(EVENT_SUBSCRIBERS_LOCK)
        self.assertTrue(callable(broadcast_event))
        self.assertTrue(callable(emit_notification))
        self.assertTrue(callable(event_matches))
        self.assertTrue(callable(get_webhook_dispatcher))
        self.assertTrue(callable(public_webhook_configs))
        self.assertTrue(callable(publish_event))
        self.assertTrue(callable(register_event_subscriber))
        self.assertTrue(callable(session_event))
        self.assertTrue(callable(shutdown_webhooks))
        self.assertTrue(callable(unregister_event_subscriber))
        self.assertTrue(callable(webhook_configs))

    def test_webapp_state_shim_identity(self):
        import pkg.state.cache
        import pkg.state.launch
        import pkg.state.media_probe
        import pkg.state.registry
        import pkg.state.sse
        import webapp_state

        self.assertIs(webapp_state.STATE_LOCK, pkg.state.cache.STATE_LOCK)
        self.assertIs(webapp_state.PUBLIC_STATE_CACHE, pkg.state.cache.PUBLIC_STATE_CACHE)
        self.assertIs(webapp_state.MEDIA_EPOCH, pkg.state.cache.MEDIA_EPOCH)
        self.assertIs(webapp_state.FILE_PROBE_CACHE, pkg.state.cache.FILE_PROBE_CACHE)
        # Registry is the canonical source for process globals
        self.assertIs(webapp_state.RUNNING, pkg.state.registry.RUNNING)
        self.assertIs(webapp_state.PROCESSES, pkg.state.registry.PROCESSES)
        self.assertIs(webapp_state.PROCESS_LOCK, pkg.state.registry.PROCESS_LOCK)
        self.assertIs(webapp_state.SESSION_EVENTS, pkg.state.registry.SESSION_EVENTS)
        self.assertIs(webapp_state.EVENT_SEQUENCE, pkg.state.registry.EVENT_SEQUENCE)
        # Launch re-exports from registry (same object)
        self.assertIs(pkg.state.launch.RUNNING, pkg.state.registry.RUNNING)
        self.assertIs(pkg.state.launch.PROCESSES, pkg.state.registry.PROCESSES)
        self.assertIs(pkg.state.launch.PROCESS_LOCK, pkg.state.registry.PROCESS_LOCK)
        self.assertIs(webapp_state.FIELDS, pkg.state.media_probe.FIELDS)
        self.assertIs(webapp_state.EVENT_SUBSCRIBERS, pkg.state.sse.EVENT_SUBSCRIBERS)
        self.assertIsNotNone(webapp_state.TOKEN)
        self.assertEqual(len(webapp_state.TOKEN), 32)


if __name__ == "__main__":
    unittest.main()
