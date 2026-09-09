"""HealthHandlers capability handlers. Log, diagnostic, backup, and update endpoints."""

import json
import copy
import urllib.parse
import zipfile

from api_errors import BadRequest, Conflict
from crash_report import build_preview
from cloud_sync import LibrarySyncUnavailable
from openbox import DATA, load_state
from routes.registry import route
from openbox_logging import read_diagnostic_log
from parity_backup import BACKUP_ITEMS, create_backup, restore_backup, diff_manifests
from pkg.parity.parity_redact import detach_state_view, redact_state_for_export
from updates import check_update, install_desktop_entry, install_update
from webapp_state import JOB_MANAGER, RUNNING, approved_backup_file, bump_media_epoch, load_state_view, sync_cloud, transact_state


class HealthHandlers:
    @route("GET", "/api/log")
    def _api_get_api_log(self, parsed):
        self.send_json(200, {"log": read_diagnostic_log(DATA.parent)})
        return

    @route("GET", "/api/diagnostic")
    def _api_get_api_diagnostic(self, parsed):
        recent_job_ids = []
        for job in JOB_MANAGER.history(limit=10):
            job_id = job.get("job_id")
            if job_id:
                recent_job_ids.append(job_id)
        for job in JOB_MANAGER.snapshots().values():
            job_id = job.get("job_id")
            if job_id and job_id not in recent_job_ids:
                recent_job_ids.append(job_id)
        preview = build_preview(DATA.parent, recent_job_ids=recent_job_ids[:10])
        self.send_json(200, {"report": json.dumps(preview, indent=2)})
        return

    @route("GET", "/api/update")
    def _api_get_api_update(self, parsed):
        try:
            payload = check_update()
        except (ValueError, OSError, TypeError, AttributeError) as error:
            raise BadRequest(str(error)) from None
        last_checked = load_state_view().get("settings", {}).get("last_update_check", "")
        self.send_json(200, {**payload, "last_checked": last_checked})
        return

    @route("GET", "/api/backup")
    def _api_get_api_backup(self, parsed):
        export = redact_state_for_export(detach_state_view(load_state_view()))
        data = json.dumps(export, indent=2).encode()
        self.send_response(200)
        self.headers_common("application/json")
        self.send_header(
            "Content-Disposition",
            'attachment; filename="openbox-portable-redacted-export.json"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return

    @route("GET", "/api/backup/manifest")
    def _api_get_api_backup_manifest(self, parsed):
        self.send_json(200, {"items": sorted(BACKUP_ITEMS)})
        return

    @route("GET", "/api/backups")
    def _api_get_api_backups(self, parsed):
        folder = DATA.parent / "backups"
        backups = []
        for path in sorted(folder.glob("OpenBoxBackup-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                with zipfile.ZipFile(path) as package:
                    manifest = json.loads(package.read("manifest.json")) if "manifest.json" in package.namelist() else {}
            except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError):
                manifest = {"items": [], "invalid": True}
            backups.append({
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "created": manifest.get("created", ""),
                "items": manifest.get("items", []),
                "invalid": bool(manifest.get("invalid")),
            })
        self.send_json(200, {"backups": backups})
        return

    @route("POST", "/api/cloud/sync")
    def _api_post_api_cloud_sync(self, payload):
        self.run_cloud_sync(payload)

    @route("POST", "/api/update/install")
    def _api_post_api_update_install(self, payload):
        self.run_update_install(payload)

    @route("POST", "/api/desktop/install")
    def _api_post_api_desktop_install(self, payload):
        self.send_json(200, {"desktop": install_desktop_entry()})

    @route("POST", "/api/backup/create")
    def _api_post_api_backup_create(self, payload):
        self.create_library_backup(payload)

    @route("POST", "/api/backup/restore")
    def _api_post_api_backup_restore(self, payload):
        self.restore_library_backup(payload)

    @route("GET", "/api/v2/backup/diff")
    def _api_get_api_v2_backup_diff(self, parsed):
        """Compare current library state against a backup archive (1.7.2)."""
        qs = getattr(parsed, "query", "") or ""
        params = urllib.parse.parse_qs(qs)
        archive_name = (params.get("archive", [""])[0] or "").strip()
        if not archive_name:
            self.send_json(400, {"error": "archive parameter required"})
            return
        archive = approved_backup_file(archive_name)
        if not archive:
            self.send_json(404, {"error": "backup archive not found"})
            return
        try:
            state = load_state_view()
            result = diff_manifests(state, archive)
            self.send_json(200, result)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        return

    def create_library_backup(self, payload):
        items = payload.get("items", ["library", "settings"])
        keep = int(payload.get("keep", 0))

        def worker(_cancel_event):
            state = load_state()
            archive = create_backup(DATA.parent, state, items, keep=keep, running_map=RUNNING)
            return {"archive": str(archive), "name": archive.name}

        job = JOB_MANAGER.submit("library-backup", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def restore_library_backup(self, payload):
        raw_path = str(payload.get("path", "") or "").strip()
        if not raw_path:
            raise BadRequest("Backup path is required.")
        try:
            archive = approved_backup_file(raw_path)
        except (ValueError, FileNotFoundError) as error:
            raise BadRequest(str(error)) from error
        items = payload.get("items")
        force = bool(payload.get("force"))

        def worker(_cancel_event):
            restored = restore_backup(archive, DATA.parent, items=items, running_map=RUNNING, force=force)
            if "media" in restored:
                bump_media_epoch()
            return {"restored": restored}

        job = JOB_MANAGER.submit("library-restore", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def run_cloud_sync(self, payload):
        def worker(_cancel_event):
            return sync_cloud()

        job = JOB_MANAGER.submit("cloud-sync", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    def run_update_install(self, payload):
        def worker(_cancel_event):
            update = check_update()
            return install_update(update)

        job = JOB_MANAGER.submit("updater-install", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})

    @route("POST", "/api/v2/library/sync/publish")
    def _api_post_api_v2_library_sync_publish(self, payload):
        if isinstance(payload, dict) and payload.get("protocol") == "v3":
            from pkg.parity.parity_library_sync import SyncFolderError, SyncStaleError, publish_outbox, state_token, sync_enabled

            state = load_state_view()
            if not sync_enabled(state):
                self.send_json(400, {"error": "Enable library synchronization before publishing.", "code": "SYNC_NOT_ENABLED"})
                return
            folder = str(state.get("settings", {}).get("cloud_folder", "") or "")
            if not folder:
                raise BadRequest("Configure a mounted cloud sync folder first.")
            base_token = state_token(state)
            pending = list((state.get("library_sync") or {}).get("outbox", []))
            detached = copy.deepcopy(state)
            try:
                result = publish_outbox(detached, folder)
            except (SyncFolderError, OSError, ValueError) as error:
                raise BadRequest(str(error)) from None

            def acknowledge(current):
                if state_token(current) != base_token:
                    raise SyncStaleError("Local library changed while sync was publishing; retry the operation.")
                metadata = current.setdefault("library_sync", {})
                ids = {item.get("event_id") for item in pending if isinstance(item, dict)}
                metadata["outbox"] = [item for item in metadata.get("outbox", []) if item.get("event_id") not in ids]
                return len(ids)

            try:
                acknowledged = transact_state(acknowledge)[1]
            except SyncStaleError as error:
                raise Conflict(str(error), code="SYNC_PUBLISH_STALE") from None
            self.send_json(200, {**result, "acknowledged": acknowledged, "protocol": "v3"})
            return
        error = LibrarySyncUnavailable()
        self.send_json(503, {"error": error.message, "code": error.code})

    @route("POST", "/api/v2/library/sync/pull")
    def _api_post_api_v2_library_sync_pull(self, payload):
        error = LibrarySyncUnavailable()
        self.send_json(503, {"error": error.message, "code": error.code})

    @route("POST", "/api/v2/library/sync/preview")
    def _api_post_api_v2_library_sync_preview(self, payload):
        """Preview the opt-in causal catalog transport without mutating state."""
        from pkg.parity.parity_library_sync import (
            SyncValidationError,
            preview_sync,
            read_events,
            sync_enabled,
        )

        state = load_state_view()
        if not sync_enabled(state):
            self.send_json(400, {"error": "Enable library synchronization before reviewing changes.", "code": "SYNC_NOT_ENABLED"})
            return
        folder = str(state.get("settings", {}).get("cloud_folder", "") or "")
        try:
            incoming = payload.get("events") if isinstance(payload, dict) and isinstance(payload.get("events"), list) else read_events(folder)
            plan = preview_sync(state, incoming)
        except (SyncValidationError, OSError, ValueError) as error:
            self.send_json(400, {"error": str(error), "code": "SYNC_INVALID"})
            return
        self.send_json(200, plan)

    @route("POST", "/api/v2/library/sync/apply")
    def _api_post_api_v2_library_sync_apply(self, payload):
        """Apply a reviewed causal sync plan at the state transaction boundary."""
        from pkg.parity.parity_library_sync import SyncStaleError, SyncValidationError, apply_sync, sync_enabled

        state = load_state_view()
        if not sync_enabled(state):
            self.send_json(400, {"error": "Enable library synchronization before applying changes.", "code": "SYNC_NOT_ENABLED"})
            return
        plan = payload.get("plan") if isinstance(payload, dict) else None
        if not isinstance(plan, dict):
            raise BadRequest("A reviewed sync plan is required.")
        conflicts = payload.get("conflicts") if isinstance(payload, dict) else None
        # Preserve a local recovery point before replacing catalog records.
        # The archive is created outside the state transaction so filesystem
        # work never extends the commit lock; a failed snapshot aborts apply.
        try:
            recovery = create_backup(DATA.parent, state, ["library"], keep=3, running_map=RUNNING)
        except (OSError, ValueError) as error:
            raise BadRequest(f"Unable to create sync recovery backup: {error}") from None

        def mutate(current):
            current.setdefault("library_sync", {})["_suppress_local_recording"] = True
            return apply_sync(current, plan, conflicts=conflicts)

        try:
            result = transact_state(mutate)[1]
        except SyncStaleError as error:
            raise Conflict(str(error), code="SYNC_PREVIEW_STALE") from None
        except SyncValidationError as error:
            raise BadRequest(str(error)) from None
        self.send_json(200, {
            "applied": result.get("applied", 0),
            "conflicts": result.get("conflicts", []),
            "changed": result.get("changed", False),
            "recovery_backup": recovery.name,
        })
