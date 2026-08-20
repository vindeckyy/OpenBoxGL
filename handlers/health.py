"""HealthHandlers capability handlers. Jobs, log, diagnostic, backup, and update endpoints."""

import json
import zipfile

from api_errors import BadRequest
from crash_report import build_report
from openbox import DATA, load_state
from openbox_logging import read_diagnostic_log
from parity_backup import BACKUP_ITEMS, create_backup, restore_backup
from updates import check_update, install_desktop_entry, install_update
from webapp_state import JOB_MANAGER, RUNNING, approved_backup_file, bump_media_epoch, load_state_view, sync_cloud


class HealthHandlers:
    def _api_get_api_jobs(self, parsed):
        self.send_json(200, {"jobs": JOB_MANAGER.snapshots(), "history": JOB_MANAGER.history()})

    def _api_get_api_log(self, parsed):
        self.send_json(200, {"log": read_diagnostic_log(DATA.parent)})
        return

    def _api_get_api_diagnostic(self, parsed):
        self.send_json(200, {"report": build_report(DATA.parent)})
        return

    def _api_get_api_update(self, parsed):
        try:
            payload = check_update()
        except (ValueError, OSError, TypeError, AttributeError) as error:
            raise BadRequest(str(error)) from None
        last_checked = load_state_view().get("settings", {}).get("last_update_check", "")
        self.send_json(200, {**payload, "last_checked": last_checked})
        return

    def _api_get_api_backup(self, parsed):
        data = json.dumps(load_state_view(), indent=2).encode()
        self.send_response(200)
        self.headers_common("application/json")
        self.send_header("Content-Disposition", "attachment; filename=openbox-library.json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return

    def _api_get_api_backup_manifest(self, parsed):
        self.send_json(200, {"items": sorted(BACKUP_ITEMS)})
        return

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

    def _api_post_api_cloud_sync(self, payload):
        self.send_json(200, sync_cloud())

    def _api_post_api_update_install(self, payload):
        update = check_update()
        self.send_json(200, install_update(update))

    def _api_post_api_desktop_install(self, payload):
        self.send_json(200, {"desktop": install_desktop_entry()})

    def _api_post_api_backup_create(self, payload):
        self.create_library_backup(payload)

    def _api_post_api_backup_restore(self, payload):
        self.restore_library_backup(payload)

    def create_library_backup(self, payload):
        items = payload.get("items", ["library", "settings"])
        keep = int(payload.get("keep", 0))
        state = load_state()
        archive = create_backup(DATA.parent, state, items, keep=keep, running_map=RUNNING)
        self.send_json(200, {"archive": str(archive), "name": archive.name})

    def restore_library_backup(self, payload):
        archive = approved_backup_file(payload.get("path", ""))
        items = payload.get("items")
        restored = restore_backup(archive, DATA.parent, items=items, running_map=RUNNING, force=bool(payload.get("force")))
        if "media" in restored:
            bump_media_epoch()
        self.send_json(200, {"restored": restored})


