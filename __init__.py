"""MusicBrainz Picard 3 Library Repair plugin."""


_dialogs = []


def enable(api):
    from picard.plugin3.api import BaseAction
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


def disable():
    for dialog in list(_dialogs):
        dialog.close()
