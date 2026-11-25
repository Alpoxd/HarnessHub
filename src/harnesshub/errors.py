class HubError(Exception):
    """An actionable error suitable for both CLI and TUI."""


class BusyError(HubError):
    pass
