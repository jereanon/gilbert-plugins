"""Google plugin — registers Google OAuth, Workspace directory, Gmail, Drive, Calendar, and Tasks backends."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class GooglePlugin(Plugin):
    """Side-effect plugin: importing the modules registers the backends.

    Covers:
    - ``google_auth`` — AuthBackend (OAuth ID token verification)
    - ``google_directory`` — UserProviderBackend (Google Workspace directory)
    - ``gmail`` — EmailBackend
    - ``gdrive_documents`` — DocumentBackend (Google Drive)
    - ``google_calendar`` — CalendarBackend (Google Calendar v3)
    - ``google_tasks`` — TaskBackend (Google Tasks v1)
    """

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="google",
            version="1.0.0",
            description="Google integration suite (auth, directory, Gmail, Drive, Calendar, Tasks)",
            provides=[
                "google_auth",
                "google_directory",
                "gmail",
                "google_drive",
                "google_calendar",
                "google_tasks",
            ],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import (  # noqa: F401
            gdrive_documents,
            gmail,
            google_auth,
            google_calendar,
            google_directory,
            google_tasks,
        )

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return GooglePlugin()
