"""MusicBrainz Picard 3 Library Repair plugin."""


_dialogs = []


def enable(api):
    from picard.plugin3.api import BaseAction
    from .artwork import ensure_folder_cover
    from .ui import LibraryRepairDialog

    class AuditLibraryAction(BaseAction):
        TITLE = "Audit music folders…"
        MENU = ("Library Repair",)

        def callback(self, objs):
            dialog = LibraryRepairDialog(self.api, self.api.tagger.window)
            _dialogs.append(dialog)
            dialog.finished.connect(lambda: _dialogs.remove(dialog) if dialog in _dialogs else None)
            dialog.show()

    api.register_tools_menu_action(AuditLibraryAction)

    def save_missing_cover(api, file):
        try:
            created = ensure_folder_cover(file)
            if created:
                api.logger.info("Wrote album cover: %s", created)
        except Exception as exc:
            api.logger.warning("Could not write album-folder cover for %s: %s", file.filename, exc)

    api.register_file_post_save_processor(save_missing_cover)


def disable():
    for dialog in list(_dialogs):
        dialog.close()
