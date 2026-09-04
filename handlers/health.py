"""HealthHandlers capability handlers. Log, diagnostic, backup, and update endpoints."""

import json
import urllib.parse
import zipfile

from api_errors import BadRequest
from crash_report import build_preview
from openbox import DATA, load_state
from routes.registry import route
from openbox_logging import read_diagnostic_log
from parity_backup import BACKUP_ITEMS, create_backup, restore_backup, diff_manifests
from pkg.parity.parity_redact import detach_state_view, redact_state_for_export
from updates import check_update, install_desktop_entry, install_update
from webapp_state import JOB_MANAGER, RUNNING, approved_backup_file, bump_media_epoch, load_state_view, sync_cloud


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
        archive = approved_backup_file(payload.get("path", ""))
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
        from cloud_sync import publish_library
        import openbox

        state = openbox.load_state()
        folder = state.get("settings", {}).get("cloud_folder", "")
        if not folder:
            self.send_json(400, {"error": "Configure a mounted cloud sync folder first."})
            return
        device_id = str(payload.get("device_id") or "local")
        result = publish_library(state, folder, device_id=device_id)
        self.send_json(200, result)

    @route("POST", "/api/v2/library/sync/pull")
    def _api_post_api_v2_library_sync_pull(self, payload):
        from cloud_sync import pull_library
        import openbox

        state = openbox.load_state()
        folder = state.get("settings", {}).get("cloud_folder", "")
        if not folder:
            self.send_json(400, {"error": "Configure a mounted cloud sync folder first."})
            return
        device_id = str(payload.get("device_id") or "local")
        confirm = bool(payload.get("confirm"))
        result = pull_library(state, folder, device_id=device_id, confirm=confirm)

        if result.get("needs_confirm"):
            self.send_json(409, {
                "error": (
                    f"Pull would delete {result['deleted']} of "
                    f"{result.get('local_count', 0)} games. Resend with confirm:true to apply."
                ),
                "code": "SYNC_NEEDS_CONFIRM",
                "needs_confirm": True,
                "added": result["added"],
                "updated": result["updated"],
                "deleted": result["deleted"],
                "skipped": result["skipped"],
                "local_count": result.get("local_count", 0),
                "synced_at": result["synced_at"],
            })
            return

        from webapp_state import transact_state

        def mutate(current):
            current["games"] = result["games"]
            current.setdefault("settings", {})["last_library_sync"] = result["synced_at"]

        transact_state(mutate)
        self.send_json(200, {
            "added": result["added"],
            "updated": result["updated"],
            "deleted": result["deleted"],
            "skipped": result["skipped"],
            "synced_at": result["synced_at"],
            "needs_confirm": False,
            "conflicts": result.get("conflicts", []),
        })


