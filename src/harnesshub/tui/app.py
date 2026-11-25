from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Resize
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from harnesshub.agy import AgyMaintenance
from harnesshub.agy_workspace import agy_history_status, set_agy_shared_history, sync_agy_profiles
from harnesshub.errors import HubError
from harnesshub.ide import launch_ide
from harnesshub.installation import SOURCES
from harnesshub.models import (
    catalog_models,
    group_catalog_models,
    probe_all_profiles,
    probe_profile_telemetry,
    rank_candidates,
)
from harnesshub.telemetry import account_telemetry
from harnesshub.tui.formatters import (
    COLOR_INFO,
    COLOR_MUTED,
    COLOR_OK,
    COLOR_WARN,
    _fmt_quota,
    _fmt_reset,
    _fmt_tokens,
    _fmt_turns,
    esc,
    fmt_agy_group_quotas,
    fmt_codex_group_quotas,
    fmt_grouped_models_summary,
    fmt_quota_badge,
    fmt_status_badge,
    fmt_window_rows,
)
from harnesshub.tui.modals import (
    ConfirmRemove,
    ConsultForm,
    ExistingProfiles,
    HandoffForm,
    InstallForm,
    ProfileForm,
    ProxyForm,
    ResumeProfileForm,
    _model_choices_from_hub,
)
from harnesshub.tui.runner import Request, execute_request

if TYPE_CHECKING:
    from harnesshub.registry import Harness
    from harnesshub.service import Hub


class HarnessHubApp(App[Request | None]):
    TITLE = "HarnessHub"
    SUB_TITLE = "аккаунты · модели · диалоги · настройки"
    CSS_PATH = Path(__file__).resolve().parent.parent / "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None
    BINDINGS = [
        ("1", "tab('profiles-tab')", "Аккаунты"),
        ("2", "tab('models-tab')", "По модели"),
        ("3", "tab('sessions-tab')", "Диалоги"),
        ("4", "tab('settings-tab')", "Настройки"),
        ("enter", "launch", "Запуск"),
        ("u", "usage", "Квоты"),
        ("a", "toggle_automode", "Автомод"),
        ("p", "focus_project", "Проект"),
        ("slash", "focus_project", "Проект"),
        ("n", "new_profile", "Новый"),
        ("l", "login", "Войти"),
        ("r", "refresh", "Обновить"),
        ("q", "quit", "Выход"),
    ]

    def __init__(
        self,
        hub: Hub,
        provider: str | None = None,
        message: str = "",
        *,
        interactive: bool = False,
    ):
        super().__init__()
        self.hub = hub
        self.provider = provider or next(iter(hub.harnesses()))
        self.message = message
        self.interactive = interactive
        self.rows: list[dict[str, Any]] = []
        self.telemetry_map: dict[tuple[str, str], dict[str, Any]] = {}
        self.registry_rows: list[dict[str, Any]] = []
        self.candidate_rows: list[dict[str, Any]] = []
        self.session_rows: list[dict[str, Any]] = []
        self.all_projects_sessions: bool = False
        self._specs: dict[str, Harness] = {}
        self._exec_map: dict[str, str | None] = {}
        self._all_profiles: list[dict[str, Any]] = []
        self._global_proxy_info: dict[str, Any] | None = None
        self._profile_proxy_map: dict[tuple[str, str], dict[str, Any]] = {}
        self._agy_history_info: dict[str, Any] | None = None
        self._models_dirty: bool = True
        self._sessions_dirty: bool = True
        self._profiles_table_schema: str = ""
        self._last_models_filter: tuple[str, str, str] | None = None
        self._last_smart_model: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="topbar"):
            yield Static("[bold #e6edf3]HARNESS[/][bold #58a6ff]HUB[/]", id="brand")
            yield Static("Проект:", id="project-label")
            yield Input(value=str(Path.cwd()), id="project", compact=True)
            yield Static(id="totals")
            yield Button("Автомод: ВЫКЛ", id="toggle-automode", compact=True)
            yield Button("Квоты [u]", id="probe-all", compact=True)
        with TabbedContent(id="main-tabs"):
            with TabPane("[1] Аккаунты", id="profiles-tab"):
                with Horizontal(id="profiles-layout"):
                    with Vertical(id="sidebar"):
                        yield Label("ХАРНЕССЫ", classes="section-label")
                        yield OptionList(id="harness-list")
                    with Vertical(id="workspace"):
                        with Horizontal(id="harness-heading"):
                            yield Static(id="harness-title")
                            yield Button(
                                "Установить",
                                id="install-harness",
                                variant="primary",
                                compact=True,
                            )
                            yield Button("Опросить", id="probe-provider", compact=True)
                        yield Static(id="harness-note")
                        yield DataTable(id="profiles", cursor_type="row")
                        yield Static(id="profile-empty")
                        with VerticalScroll(id="account-inspector-box"):
                            yield Static(id="account-inspector")
                        with Horizontal(id="account-launch-row"):
                            yield Select(
                                [("Модель по умолчанию клиента", "__default__")],
                                value="__default__",
                                allow_blank=False,
                                compact=True,
                                id="account-model-select",
                            )
                            yield Button("▶ Запустить", id="run", variant="primary", compact=True)
                            yield Button("IDE", id="launch-ide", compact=True)
                            yield Button("Родной", id="native", compact=True)
                        with Horizontal(id="profile-mgmt-row"):
                            yield Button("Войти", id="login", compact=True)
                            yield Button("★ Основной", id="default", compact=True)
                            yield Button("+ Новый", id="new", compact=True)
                            yield Button("Импорт", id="import", compact=True)
                            yield Button("Найти", id="discover", compact=True)
                            yield Button("Имя", id="rename", compact=True)
                            yield Static(id="mgmt-spacer")
                            yield Button("✕ Убрать", id="remove", variant="error", compact=True)
            with TabPane("[2] По модели", id="models-tab"):
                with Horizontal(id="models-filters"):
                    yield Select(
                        [("Все харнессы", "__all__")],
                        value="__all__",
                        allow_blank=False,
                        compact=True,
                        id="registry-provider-filter",
                    )
                    yield Select(
                        [
                            ("Все семейства", "__all__"),
                            ("OpenAI / GPT", "gpt"),
                            ("Google Gemini", "gemini"),
                            ("Anthropic Claude", "claude"),
                            ("Другие", "other"),
                        ],
                        value="__all__",
                        allow_blank=False,
                        compact=True,
                        id="registry-family-filter",
                    )
                    yield Select(
                        [
                            ("Effort: High", "high"),
                            ("Effort: XHigh", "xhigh"),
                            ("Effort: Medium", "medium"),
                            ("Effort: Low", "low"),
                        ],
                        value="high",
                        allow_blank=False,
                        compact=True,
                        id="effort-filter",
                    )
                    yield Select(
                        [("Нет моделей", "__none__")],
                        value="__none__",
                        allow_blank=False,
                        compact=True,
                        id="smart-model-select",
                    )
                yield DataTable(id="models-registry", cursor_type="row")
                yield Static("Рейтинг аккаунтов для выбранной модели", id="candidates-heading")
                yield DataTable(id="model-candidates", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button(
                        "▶ Запустить лучший", id="run-smart", variant="primary", compact=True
                    )
                    yield Button("▶ Запустить слот", id="run-candidate", compact=True)
                    yield Button("Опросить все", id="probe-agy", compact=True)
                    yield Button("Второе мнение", id="consult-open", compact=True)
            with TabPane("[3] Диалоги", id="sessions-tab"):
                with Horizontal(id="sessions-filters"):
                    yield Select(
                        [("Все харнессы", "__all__")],
                        value="__all__",
                        allow_blank=False,
                        compact=True,
                        id="sessions-provider-filter",
                    )
                    yield Select(
                        [("Все аккаунты", "__all__")],
                        value="__all__",
                        allow_blank=False,
                        compact=True,
                        id="sessions-profile-filter",
                    )
                    yield Input(
                        placeholder="Поиск по теме, проекту, модели, ID…",
                        compact=True,
                        id="sessions-search",
                    )
                    yield Button("Проект: текущий", id="session-scope", compact=True)
                with Horizontal(id="sessions-main"):
                    yield DataTable(id="sessions-table", cursor_type="row")
                    with VerticalScroll(id="session-preview-box"):
                        yield Static(
                            "Выбери диалог слева, чтобы прочитать сообщения.",
                            id="session-preview",
                        )
                with Horizontal(classes="actions"):
                    yield Button(
                        "▶ Продолжить", id="session-resume", variant="primary", compact=True
                    )
                    yield Button("С др. аккаунта", id="session-resume-as", compact=True)
                    yield Button("Handoff", id="session-handoff", compact=True)
                    yield Button("Обновить", id="sessions-refresh", compact=True)
            with TabPane("[4] Настройки", id="settings-tab"):
                with VerticalScroll(id="settings-scroll"):
                    with Vertical(classes="settings-card"):
                        yield Static("Сеть и прокси", classes="card-title")
                        yield Static(id="global-proxy-summary", classes="network-summary")
                        with Horizontal(classes="settings-row"):
                            yield Button("Настроить общий прокси", id="global-proxy", compact=True)
                        yield Static(id="profile-proxy-summary", classes="network-summary")
                        with Horizontal(classes="settings-row"):
                            yield Select(
                                [("Нет профилей", "__none__")],
                                value="__none__",
                                allow_blank=False,
                                compact=True,
                                id="settings-proxy-profile",
                            )
                            yield Button("Настроить профиль", id="profile-proxy", compact=True)
                    with Vertical(classes="settings-card"):
                        yield Static(
                            "AGY · Автомод, общая история и синхронизация", classes="card-title"
                        )
                        yield Static(id="agy-workspace-summary", classes="network-summary")
                        with Horizontal(classes="settings-row"):
                            yield Button(
                                "Автомод AGY: ВЫКЛ", id="agy-automode-toggle", compact=True
                            )
                            yield Button(
                                "Общая история: ВЫКЛ", id="agy-history-toggle", compact=True
                            )
                            yield Button("Синхр. скиллы", id="agy-sync", compact=True)
                    with Vertical(classes="settings-card"):
                        yield Static("AGY · Патч бинарника и автохил", classes="card-title")
                        yield Static(
                            "Встроенный патчер Linux x64: проверка сегментов, резервная копия "
                            "SHA-256, проверка запуска и автоматический откат.",
                            classes="muted",
                        )
                        with Horizontal(classes="settings-row"):
                            yield Button("Проверить", id="agy-status", compact=True)
                            yield Button(
                                "Применить", id="agy-heal", variant="primary", compact=True
                            )
                            yield Button("Откатить", id="agy-restore", compact=True)
                        with Horizontal(classes="settings-row"):
                            yield Select(
                                [("Нет профилей AGY", "__none__")],
                                value="__none__",
                                allow_blank=False,
                                compact=True,
                                id="settings-agy-profile",
                            )
                            yield Button("Автохил: ВЫКЛ", id="agy-toggle", compact=True)
        yield Static(
            Text(self.message or "Готово. [1-4] вкладки · [Enter] запуск · [u] квоты · [q] выход"),
            id="activity",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#models-registry", DataTable).add_columns(
            "Харнесс", "Модель", "Уровни", "Слоты", "Квота", "Сброс", "Токены"
        )
        self.query_one("#model-candidates", DataTable).add_columns(
            "#", "Аккаунт", "План", "Модель", "Квота", "Сброс", "Статус"
        )
        self.query_one("#sessions-table", DataTable).add_columns(
            "Аккаунт", "Модель", "Ходов", "Токены", "Обновлён", "Проект", "Тема"
        )
        self.refresh_data()

    def on_unmount(self) -> None:
        self.workers.cancel_all()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.height < 36 or event.size.width < 100, "compact")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if len(self.screen_stack) != 1:
            return False
        if isinstance(self.focused, Input) and action not in ("launch",):
            return False
        return True

    def action_tab(self, tab_id: str) -> None:
        if not self.is_running:
            return
        self.set_focus(None)
        self.query_one("#main-tabs", TabbedContent).active = tab_id

    def action_focus_project(self) -> None:
        if self.is_running:
            self.query_one("#project", Input).focus()

    def action_toggle_automode(self) -> None:
        self._toggle_current_automode()

    @staticmethod
    def _safe_row(rows: list[dict[str, Any]], table: DataTable[Any]) -> dict[str, Any] | None:
        if not rows:
            return None
        idx = table.cursor_row
        if idx < 0 or idx >= len(rows):
            idx = 0
        return rows[idx]

    def _dispatch_request(self, request: Request) -> None:
        if self.interactive and not self.is_headless:
            try:
                with self.suspend():
                    msg = execute_request(self.hub, request)
                if self.is_running:
                    self.refresh_data()
                    self.activity(msg)
                return
            except SuspendNotSupported:
                pass
        self.exit(request)

    @on(DataTable.RowSelected, "#profiles")
    def launch_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.request_launch()

    @on(DataTable.RowSelected, "#model-candidates")
    def launch_selected_candidate(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.request_candidate_launch()

    @on(DataTable.RowSelected, "#sessions-table")
    def resume_selected_session(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.request_session_resume()

    @on(DataTable.RowHighlighted, "#profiles")
    def selected_profile_changed(self) -> None:
        if not self.is_running:
            return
        try:
            self.refresh_account_inspector()
            self._sync_settings_selectors_from_current()
            self.refresh_network()
            self.refresh_agy_workspace()
        except NoMatches:
            return

    @on(DataTable.RowHighlighted, "#models-registry")
    def registry_row_highlighted(self) -> None:
        if not self.is_running or not self.registry_rows:
            return
        try:
            if self.query_one("#main-tabs", TabbedContent).active != "models-tab":
                return
            row = self._safe_row(self.registry_rows, self.query_one("#models-registry", DataTable))
            if row is None:
                return
            mid = str(row["model"])
            sel = self.query_one("#smart-model-select", Select)
            if sel.value != mid:
                with sel.prevent(Select.Changed):
                    sel.value = mid
                self._refresh_candidates_table()
        except NoMatches:
            return

    @on(Select.Changed, "#registry-provider-filter")
    @on(Select.Changed, "#registry-family-filter")
    @on(Select.Changed, "#effort-filter")
    def registry_filter_changed(self) -> None:
        if not self.is_running:
            return
        try:
            if self.query_one("#main-tabs", TabbedContent).active != "models-tab":
                self._models_dirty = True
                return
            key = (
                str(self.query_one("#registry-provider-filter", Select).value),
                str(self.query_one("#registry-family-filter", Select).value),
                str(self.query_one("#effort-filter", Select).value),
            )
            if key != self._last_models_filter:
                self.refresh_models_tab()
        except NoMatches:
            return

    @on(Select.Changed, "#smart-model-select")
    def smart_model_changed(self) -> None:
        if not self.is_running:
            return
        try:
            if self.query_one("#main-tabs", TabbedContent).active != "models-tab":
                self._models_dirty = True
                return
            val = str(self.query_one("#smart-model-select", Select).value)
            if val != self._last_smart_model:
                self._refresh_candidates_table()
        except NoMatches:
            return

    @on(Select.Changed, "#sessions-provider-filter")
    def sessions_provider_changed(self) -> None:
        if not self.is_running:
            return
        try:
            self._populate_session_profile_filter()
            if self.query_one("#main-tabs", TabbedContent).active != "sessions-tab":
                self._sessions_dirty = True
                return
            self.refresh_sessions_tab()
        except NoMatches:
            return

    @on(Select.Changed, "#sessions-profile-filter")
    def sessions_profile_changed(self) -> None:
        if not self.is_running:
            return
        try:
            if self.query_one("#main-tabs", TabbedContent).active != "sessions-tab":
                self._sessions_dirty = True
                return
            self.refresh_sessions_tab()
        except NoMatches:
            return

    @on(Select.Changed, "#settings-proxy-profile")
    def settings_proxy_profile_changed(self) -> None:
        if not self.is_running:
            return
        try:
            self.refresh_network()
        except NoMatches:
            return

    @on(Select.Changed, "#settings-agy-profile")
    def settings_agy_profile_changed(self) -> None:
        if not self.is_running:
            return
        try:
            self.refresh_agy_workspace()
        except NoMatches:
            return

    @on(Input.Changed, "#sessions-search")
    def sessions_search_changed(self) -> None:
        if not self.is_running:
            return
        try:
            if self.query_one("#main-tabs", TabbedContent).active != "sessions-tab":
                self._sessions_dirty = True
                return
            self.refresh_sessions_tab()
        except NoMatches:
            return

    @on(DataTable.RowHighlighted, "#sessions-table")
    def session_row_highlighted(self) -> None:
        if not self.is_running:
            return
        try:
            self._update_session_preview()
        except NoMatches:
            return

    @on(Input.Changed, "#project")
    def project_changed(self) -> None:
        self._sessions_dirty = True

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if not self.is_running:
            return
        try:
            pane_id = event.pane.id if event.pane else ""
            if pane_id == "models-tab" and self._models_dirty:
                self.refresh_models_tab()
            elif pane_id == "sessions-tab" and self._sessions_dirty:
                self.refresh_sessions_tab()
            elif pane_id == "settings-tab":
                self.refresh_network()
                self.refresh_agy_workspace()
        except NoMatches:
            return

    @on(OptionList.OptionSelected, "#harness-list")
    def focus_profiles(self) -> None:
        if not self.is_running:
            return
        try:
            self.query_one("#profiles", DataTable).focus()
        except NoMatches:
            return

    @on(OptionList.OptionHighlighted, "#harness-list")
    def select_harness(self, event: OptionList.OptionHighlighted) -> None:
        if not self.is_running:
            return
        if event.option.id and event.option.id != self.provider:
            self.provider = event.option.id
            try:
                self.refresh_provider_view()
            except NoMatches:
                return

    def _populate_harness_sidebar(self) -> None:
        options = self.query_one("#harness-list", OptionList)
        harnesses = list(self._specs.values())
        counts: dict[str, int] = {}
        for p in self._all_profiles:
            counts[p["provider"]] = counts.get(p["provider"], 0) + 1
        options.clear_options()
        for spec in harnesses:
            installed = bool(self._exec_map.get(spec.id))
            cnt = counts.get(spec.id, 0)
            status = "●" if installed else "○"
            suffix = f" ({cnt})" if cnt else ""
            label = Text(f"{status} {spec.name}{suffix}")
            label.stylize(COLOR_OK if installed else COLOR_MUTED, 0, 1)
            if not installed and cnt == 0:
                label.stylize(COLOR_MUTED, 2)
            options.add_option(Option(label, id=spec.id))
        idx = next((i for i, spec in enumerate(harnesses) if spec.id == self.provider), 0)
        options.highlighted = idx

    def reload_catalog(self) -> None:
        self._specs = self.hub.harnesses()
        self._exec_map = {k: s.executable() for k, s in self._specs.items()}
        self._all_profiles = self.hub.profiles()
        self.telemetry_map = {
            (p["provider"], p["name"]): account_telemetry(self.hub, p, live=False)
            for p in self._all_profiles
        }
        self._global_proxy_info = self.hub.proxy_info()
        self._profile_proxy_map = {
            (p["provider"], p["name"]): self.hub.proxy_info(p["provider"], p["name"])
            for p in self._all_profiles
        }
        self._agy_history_info = agy_history_status(self.hub)
        self._populate_harness_sidebar()
        self._populate_filter_selects()
        self._populate_settings_selectors()
        self._models_dirty = True
        self._sessions_dirty = True

    def _populate_settings_selectors(self) -> None:
        proxy_sel = self.query_one("#settings-proxy-profile", Select)
        proxy_opts: list[tuple[str, str]] = [
            (f"{p['provider']}/{p['name']}", f"{p['provider']}/{p['name']}")
            for p in self._all_profiles
        ] or [("Нет профилей", "__none__")]
        with proxy_sel.prevent(Select.Changed):
            cur_p = str(proxy_sel.value)
            proxy_sel.set_options(proxy_opts)
            if any(v == cur_p for _, v in proxy_opts):
                proxy_sel.value = cur_p
            else:
                proxy_sel.value = proxy_opts[0][1]

        agy_sel = self.query_one("#settings-agy-profile", Select)
        agy_opts: list[tuple[str, str]] = [
            (p["name"], p["name"]) for p in self._all_profiles if p["provider"] == "agy"
        ] or [("Нет профилей AGY", "__none__")]
        with agy_sel.prevent(Select.Changed):
            cur_a = str(agy_sel.value)
            agy_sel.set_options(agy_opts)
            if any(v == cur_a for _, v in agy_opts):
                agy_sel.value = cur_a
            else:
                agy_sel.value = agy_opts[0][1]

    def _sync_settings_selectors_from_current(self) -> None:
        if not self.rows:
            return
        row = self._safe_row(self.rows, self.query_one("#profiles", DataTable))
        if row is None:
            return
        key = f"{self.provider}/{row['name']}"
        proxy_sel = self.query_one("#settings-proxy-profile", Select)
        if any(f"{p['provider']}/{p['name']}" == key for p in self._all_profiles):
            with proxy_sel.prevent(Select.Changed):
                proxy_sel.value = key
        if self.provider == "agy":
            agy_sel = self.query_one("#settings-agy-profile", Select)
            with agy_sel.prevent(Select.Changed):
                agy_sel.value = str(row["name"])

    def _populate_filter_selects(self) -> None:
        reg_opts: list[tuple[str, str]] = [("Все харнессы", "__all__")]
        sess_opts: list[tuple[str, str]] = [("Все харнессы", "__all__")]
        seen_provs = {p["provider"] for p in self._all_profiles}
        for pid, spec in self._specs.items():
            if pid in seen_provs:
                reg_opts.append((spec.name, pid))
            if pid in ("agy", "codex", "claude") and pid in seen_provs:
                sess_opts.append((spec.name, pid))

        reg_sel = self.query_one("#registry-provider-filter", Select)
        with reg_sel.prevent(Select.Changed):
            cur_reg = str(reg_sel.value)
            reg_sel.set_options(reg_opts)
            reg_sel.value = cur_reg if any(v == cur_reg for _, v in reg_opts) else "__all__"

        sess_sel = self.query_one("#sessions-provider-filter", Select)
        with sess_sel.prevent(Select.Changed):
            cur_sess = str(sess_sel.value)
            sess_sel.set_options(sess_opts)
            sess_sel.value = cur_sess if any(v == cur_sess for _, v in sess_opts) else "__all__"
        self._populate_session_profile_filter()

    def _populate_session_profile_filter(self) -> None:
        prov_filter = str(self.query_one("#sessions-provider-filter", Select).value)
        prof_sel = self.query_one("#sessions-profile-filter", Select)
        opts: list[tuple[str, str]] = [("Все аккаунты", "__all__")]
        for p in self._all_profiles:
            if prov_filter != "__all__" and p["provider"] != prov_filter:
                continue
            if prov_filter == "__all__":
                opts.append((f"{p['provider']}/{p['name']}", f"{p['provider']}/{p['name']}"))
            else:
                opts.append((p["name"], p["name"]))
        with prof_sel.prevent(Select.Changed):
            cur_val = str(prof_sel.value)
            prof_sel.set_options(opts)
            prof_sel.value = cur_val if any(v == cur_val for _, v in opts) else "__all__"

    def _ensure_profiles_table_columns(self, provider: str) -> DataTable[Any]:
        table = self.query_one("#profiles", DataTable)
        schema = provider if provider in ("agy", "codex") else "generic"
        if self._profiles_table_schema != schema:
            table.clear(columns=True)
            if schema == "agy":
                table.add_columns("Аккаунт", "Тариф", "Gemini", "Claude / GPT", "Статус")
            elif schema == "codex":
                table.add_columns("Аккаунт", "План", "Окно 5ч", "Окно 7д", "Токены", "Статус")
            else:
                table.add_columns("Аккаунт", "План", "Квота", "Сброс", "Токены", "Статус")
            self._profiles_table_schema = schema
        else:
            table.clear()
        return table

    def refresh_provider_view(self, keep_profile: str | None = None) -> None:
        if not self._specs:
            self.reload_catalog()
        specs = self._specs
        spec = specs[self.provider]
        is_exec = bool(self._exec_map.get(self.provider))
        installed_count = sum(bool(v) for v in self._exec_map.values())
        self.query_one("#totals", Static).update(
            f"{installed_count}/{len(specs)} · {len(self._all_profiles)} проф."
        )
        title = Text(spec.name, style="bold")
        title.append(
            "  ● установлен" if is_exec else "  ○ не найден в PATH",
            style=COLOR_OK if is_exec else COLOR_WARN,
        )
        self.query_one("#harness-title", Static).update(title)
        install_button = self.query_one("#install-harness", Button)
        install_button.display = not is_exec
        install_button.disabled = self.provider not in SOURCES
        self.query_one("#harness-note", Static).update(
            Text(spec.note or "Отдельная конфигурация и вход через подписку в родном клиенте.")
        )

        table = self._ensure_profiles_table_columns(self.provider)
        self.rows = [p for p in self._all_profiles if p["provider"] == self.provider]
        for profile in self.rows:
            tel = self.telemetry_map.get((self.provider, profile["name"])) or account_telemetry(
                self.hub, profile
            )
            star = "★ " if profile["default"] else ""
            acc_text = star + profile["name"]
            if tel.get("account_label") and tel["account_label"] != profile["name"]:
                short_lbl = str(tel["account_label"])
                if len(short_lbl) > 20:
                    short_lbl = short_lbl[:18] + "…"
                acc_text = f"{acc_text} ({short_lbl})"

            status_cell = fmt_status_badge(tel)
            plan_cell = Text(str(tel.get("plan_detail") or "—"))

            if self.provider == "agy":
                gem_cell, third_cell = fmt_agy_group_quotas(tel)
                table.add_row(
                    Text(acc_text),
                    plan_cell,
                    gem_cell,
                    third_cell,
                    status_cell,
                    key=profile["name"],
                )
            elif self.provider == "codex":
                c5, c7 = fmt_codex_group_quotas(tel)
                tok_str = _fmt_tokens((tel.get("tokens") or {}).get("total_tokens"))
                table.add_row(
                    Text(acc_text),
                    plan_cell,
                    c5,
                    c7,
                    tok_str,
                    status_cell,
                    key=profile["name"],
                )
            else:
                q_known = tel["quota_source"] in ("live", "cache", "cooldown")
                q_cell = fmt_quota_badge(tel.get("remaining_percent"), known=q_known)
                reset_str = _fmt_reset(tel.get("resets_at"))
                tok_str = _fmt_tokens((tel.get("tokens") or {}).get("total_tokens"))
                table.add_row(
                    Text(acc_text),
                    plan_cell,
                    q_cell,
                    reset_str,
                    tok_str,
                    status_cell,
                    key=profile["name"],
                )

        self.query_one("#profile-empty", Static).update(
            ""
            if self.rows
            else "Профилей пока нет — нажми «+ Новый», «Импорт» или «Найти»."
            if spec.profile_supported()
            else "Пока доступен запуск с родной конфигурацией клиента."
        )
        if keep_profile:
            target_idx = next((i for i, p in enumerate(self.rows) if p["name"] == keep_profile), 0)
        else:
            target_idx = next((i for i, p in enumerate(self.rows) if p["default"]), 0)
        if self.rows:
            table.move_cursor(row=target_idx)

        for widget_id in ("new", "import"):
            self.query_one(f"#{widget_id}", Button).disabled = not spec.profile_supported()
        for widget_id in ("run", "login", "default", "rename", "remove"):
            self.query_one(f"#{widget_id}", Button).disabled = not bool(self.rows)
        self.query_one("#launch-ide", Button).disabled = (
            self.provider not in ("agy", "claude", "codex", "cursor") or not self.rows
        )
        can_probe = self.provider in ("codex", "agy", "claude") and bool(self.rows)
        self.query_one("#probe-provider", Button).disabled = not can_probe
        self.query_one("#toggle-automode", Button).disabled = self.provider not in (
            "agy",
            "claude",
            "codex",
        )

        self._sync_settings_selectors_from_current()
        self.refresh_account_inspector()
        self.refresh_network()
        self.refresh_agy_workspace()

    def _update_automode_button(self, is_auto: bool) -> None:
        auto_btn = self.query_one("#toggle-automode", Button)
        auto_btn.label = f"Автомод: {'ВКЛ' if is_auto else 'ВЫКЛ'}"
        auto_btn.set_class(is_auto, "-active-toggle")

    def refresh_account_inspector(self) -> None:
        if not self.is_running:
            return
        inspector = self.query_one("#account-inspector", Static)
        model_select = self.query_one("#account-model-select", Select)
        if not self.rows:
            self._update_automode_button(self.hub.automode_enabled(self.provider))
            with model_select.prevent(Select.Changed):
                model_select.set_options([("Модель по умолчанию клиента", "__default__")])
                model_select.value = "__default__"
            inspector.update(
                Text("Выбери или добавь аккаунт, чтобы увидеть квоты по группам, модели и токены.")
            )
            return

        prof = self._safe_row(self.rows, self.query_one("#profiles", DataTable))
        assert prof is not None
        is_auto = self.hub.automode_enabled(self.provider, prof)
        self._update_automode_button(is_auto)
        tel = self.telemetry_map.get((self.provider, prof["name"])) or account_telemetry(
            self.hub, prof
        )

        opts: list[tuple[str, str]] = [("Модель по умолчанию клиента", "__default__")]
        for m in tel.get("models") or []:
            tag = " [скрыта]" if m.get("hidden") else ""
            ctx = f" · {m['context_window'] // 1000}K" if m.get("context_window") else ""
            opts.append((f"{m['name']}{ctx}{tag} ({m['id']})", str(m["id"])))
        with model_select.prevent(Select.Changed):
            prev_val = str(model_select.value)
            model_select.set_options(opts)
            model_select.value = prev_val if any(v == prev_val for _, v in opts) else "__default__"

        lines: list[str] = []
        acc_str = str(tel.get("account_label") or "аккаунт не привязан")
        proxy_info = self._profile_proxy_map.get((self.provider, prof["name"]))
        proxy_str = (
            str(proxy_info["effective"].get("url", proxy_info["effective"]["mode"]))
            if proxy_info
            else "inherit"
        )
        extra_flags = f" · Автомод: {'вкл' if is_auto else 'выкл'}"
        if self.provider == "agy":
            h_info = self._agy_history_info or {}
            h_mode = next(
                (r["mode"] for r in (h_info.get("profiles") or []) if r["profile"] == prof["name"]),
                None,
            )
            h_labels = {
                "canonical": "каноническая",
                "shared": "общая",
                "partial": "частичная",
                "isolated": "изолированная",
            }
            if h_mode:
                extra_flags += f" · История: {h_labels.get(h_mode, h_mode)}"
        lines.append(
            f"[bold]{esc(self.provider)}/{esc(prof['name'])}[/bold] · {esc(acc_str)} · "
            f"[bold {COLOR_INFO}]{esc(tel.get('plan_detail') or '—')}[/] · "
            f"Прокси: {esc(proxy_str)}{extra_flags}"
        )

        win_rows = fmt_window_rows(tel)
        if win_rows:
            lines.append("[bold]Квоты по группам:[/bold]")
            lines.extend(win_rows)
        elif tel.get("in_cooldown"):
            lines.append(f"Квота: 0% · кулдаун до {_fmt_reset(tel.get('resets_at'))}")
        elif not tel.get("logged_in"):
            lines.append("Квота: нет входа (нажми «Войти»)")
        else:
            lines.append("Квота: нет кэша — нажми [u] или «Опросить» для точных процентов")

        if tel.get("reset_credits"):
            lines[-1] += f"  · Кредитов сброса: {int(tel['reset_credits'])}"

        models = tel.get("models") or []
        if models:
            grouped_summary = fmt_grouped_models_summary(models, tel.get("model_quotas"))
            lines.append(f"Модели ({len(models)}): {grouped_summary}")
        else:
            lines.append("Модели: нет данных для этого харнесса")

        locked_models = tel.get("locked_models") or []
        if locked_models:
            lines.append(
                "Недоступны на тарифе: "
                + ", ".join(esc(f"{m['id']} [{m['badge']}]") for m in locked_models)
            )

        toks = tel.get("tokens") or {}
        tok_parts = []
        if toks.get("total_tokens") is not None:
            scope_tag = " (общая история AGY)" if toks.get("shared_history") else ""
            tok_parts.append(f"всего {_fmt_tokens(toks['total_tokens'])}{scope_tag}")
        if toks.get("recent_7d_tokens") is not None:
            tok_parts.append(f"7д: {_fmt_tokens(toks['recent_7d_tokens'])}")
        if toks.get("peak_daily_tokens") is not None:
            tok_parts.append(f"пик/день: {_fmt_tokens(toks['peak_daily_tokens'])}")
        if toks.get("sessions_count") is not None:
            tok_parts.append(f"чатов: {int(toks['sessions_count'])}")
        by_model = toks.get("by_model") or {}
        if by_model:
            top_m = ", ".join(f"{esc(m)}: {_fmt_tokens(v)}" for m, v in list(by_model.items())[:4])
            tok_parts.append(f"топ: {top_m}")
        if tok_parts:
            lines.append("Токены: " + " · ".join(tok_parts))

        inspector.update("\n".join(lines))

    def refresh_data(self, keep_profile: str | None = None) -> None:
        if not self.is_running:
            return
        self.reload_catalog()
        self.refresh_provider_view(keep_profile=keep_profile)
        active_tab = self.query_one(TabbedContent).active
        if active_tab == "models-tab":
            self.refresh_models_tab()
        elif active_tab == "sessions-tab":
            self.refresh_sessions_tab()

    def refresh_models_tab(self) -> None:
        if not self.is_running:
            return
        prov_val = str(self.query_one("#registry-provider-filter", Select).value)
        fam_val = str(self.query_one("#registry-family-filter", Select).value)
        effort_val = str(self.query_one("#effort-filter", Select).value)
        self._last_models_filter = (prov_val, fam_val, effort_val)
        prov_filter = None if prov_val == "__all__" else prov_val
        fam_filter = None if fam_val == "__all__" else fam_val

        raw_rows = catalog_models(self.hub, provider=prov_filter, family=fam_filter)
        self.registry_rows = group_catalog_models(raw_rows, preferred_effort=effort_val)
        reg_table = self.query_one("#models-registry", DataTable)
        reg_table.clear()

        eff_short = {"low": "L", "medium": "M", "high": "H", "xhigh": "XH", "max": "MAX"}
        for item in self.registry_rows:
            best_rem = item["best_remaining_percent"]
            q_known = bool(item.get("quota_known", False)) and best_rem >= 0
            q_cell = fmt_quota_badge(best_rem if best_rem >= 0 else None, known=q_known)
            effs = item.get("efforts") or []
            if effs:
                sel_eff = item.get("selected_effort")
                lvl_text = Text()
                for i, e in enumerate(effs):
                    if i:
                        lvl_text.append("/", style=COLOR_MUTED)
                    tag = eff_short.get(e, e)
                    lvl_text.append(
                        tag, style=f"bold {COLOR_INFO}" if e == sel_eff else COLOR_MUTED
                    )
            else:
                lvl_text = Text("—", style=COLOR_MUTED)
            reg_table.add_row(
                ", ".join(item["providers"]),
                Text(str(item["display_name"])),
                lvl_text,
                f"{item['ready_profiles']}/{item['total_profiles']}",
                q_cell,
                _fmt_reset(item.get("best_resets_at")),
                _fmt_tokens(item.get("total_tokens") or None),
            )

        sel = self.query_one("#smart-model-select", Select)
        with sel.prevent(Select.Changed):
            prev_val = str(sel.value)
            if self.registry_rows:
                opts: list[tuple[str, str]] = []
                seen_opts: set[str] = set()
                for r in self.registry_rows:
                    mid = str(r["model"])
                    if mid in seen_opts:
                        continue
                    seen_opts.add(mid)
                    opts.append((f"[{','.join(r['providers'])}] {r['display_name']} ({mid})", mid))
                for raw in raw_rows:
                    mid = str(raw["model"])
                    if mid not in seen_opts:
                        seen_opts.add(mid)
                        opts.append(
                            (
                                f"[{','.join(raw['providers'])}] {raw['display_name']} ({mid})",
                                mid,
                            )
                        )
                sel.set_options(opts)
                sel.value = prev_val if any(v == prev_val for _, v in opts) else opts[0][1]
            else:
                sel.set_options([("Нет моделей для фильтра", "__none__")])
                sel.value = "__none__"

        self._refresh_candidates_table()
        self._models_dirty = False

    def _refresh_candidates_table(self) -> None:
        if not self.is_running:
            return
        prov_val = str(self.query_one("#registry-provider-filter", Select).value)
        prov_filter = None if prov_val == "__all__" else prov_val
        model = str(self.query_one("#smart-model-select", Select).value)
        self._last_smart_model = model
        heading = self.query_one("#candidates-heading", Static)
        table = self.query_one("#model-candidates", DataTable)
        table.clear()

        if not model or model == "__none__":
            self.candidate_rows = []
            heading.update("Рейтинг аккаунтов: выбери модель выше")
        else:
            self.candidate_rows = rank_candidates(self.hub, model, provider=prov_filter)
            heading.update(f"Рейтинг аккаунтов для {model} ({len(self.candidate_rows)})")

        for idx, row in enumerate(self.candidate_rows, 1):
            model_cell = (
                Text(row["resolved_model"])
                if row.get("exact_match", True)
                else Text(f"↪ {row['resolved_model']}", style=COLOR_WARN)
            )
            q_cell = fmt_quota_badge(
                row.get("remaining_percent"), known=bool(row.get("quota_known", False))
            )
            table.add_row(
                str(idx),
                f"{row['provider']}/{row['profile']}",
                str(row.get("plan_detail") or row["tier"]),
                model_cell,
                q_cell,
                _fmt_reset(row.get("resets_at")),
                fmt_status_badge(row),
            )
        has_candidates = bool(self.candidate_rows)
        self.query_one("#run-smart", Button).disabled = not has_candidates
        self.query_one("#run-candidate", Button).disabled = not has_candidates

    def refresh_sessions_tab(self) -> None:
        if not self.is_running:
            return
        import harnesshub.tui as _tui_pkg

        project_str = self.query_one("#project", Input).value.strip()
        cwd: Path | None = (
            None
            if self.all_projects_sessions or not project_str
            else Path(project_str).expanduser()
        )
        if cwd is not None and not cwd.is_dir():
            cwd = None

        scope_btn = self.query_one("#session-scope", Button)
        scope_btn.label = "Проект: все" if self.all_projects_sessions else "Проект: текущий"

        prov_val = str(self.query_one("#sessions-provider-filter", Select).value)
        prof_val = str(self.query_one("#sessions-profile-filter", Select).value)
        query_val = self.query_one("#sessions-search", Input).value.strip()

        prov_filter = None if prov_val == "__all__" else prov_val
        prof_filter = None if prof_val == "__all__" else prof_val
        if prof_filter and "/" in prof_filter:
            p_prov, p_name = prof_filter.split("/", 1)
            prov_filter = p_prov
            prof_filter = p_name

        table = self.query_one("#sessions-table", DataTable)
        table.clear()
        self.session_rows = _tui_pkg.list_sessions(
            self.hub,
            cwd=cwd,
            provider=prov_filter,
            profile=prof_filter,
            query=query_val,
            limit=50,
        )
        for row in self.session_rows:
            table.add_row(
                f"{row['provider']}/{row['profile']}",
                row["model"],
                _fmt_turns(row.get("turns")),
                _fmt_tokens(row.get("tokens_used")),
                _fmt_reset(row.get("updated_at")),
                row.get("project_name") or "—",
                Text(row["title"]),
            )
        has_sessions = bool(self.session_rows)
        self.query_one("#session-resume", Button).disabled = not has_sessions
        self.query_one("#session-resume-as", Button).disabled = not has_sessions
        self.query_one("#session-handoff", Button).disabled = not has_sessions
        self._update_session_preview()
        self._sessions_dirty = False

    def _update_session_preview(self) -> None:
        if not self.is_running:
            return
        import harnesshub.tui as _tui_pkg

        preview = self.query_one("#session-preview", Static)
        row = self._safe_row(self.session_rows, self.query_one("#sessions-table", DataTable))
        if row is None:
            preview.update("Диалоги по выбранным фильтрам не найдены.")
            return
        lines = [
            f"[bold]{esc(row['provider'])}/{esc(row['profile'])}[/bold] · {esc(row['model'])}",
            f"ID: {esc(row['session_id'])}",
            f"Проект: {esc(row.get('cwd') or '—')}",
            (
                f"Обновлён: {esc(row.get('updated_at_iso') or '—')} · "
                f"Токенов: {_fmt_tokens(row.get('tokens_used'))}"
            ),
            "",
        ]
        try:
            detail = _tui_pkg.read_session(
                self.hub,
                row["session_id"],
                provider=row["provider"],
                profile=row["profile"],
            )
            msgs = detail.get("messages") or []
            if not msgs:
                lines.append("(Сообщений в транскрипте не найдено)")
            for msg in msgs[-8:]:
                role = "Вы" if msg["role"] == "user" else "Агент"
                snippet = " ".join(str(msg["text"]).split())
                if len(snippet) > 360:
                    snippet = snippet[:357] + "…"
                lines.append(f"[bold]{role}:[/bold] {esc(snippet)}\n")
        except HubError as exc:
            lines.append(f"Не удалось прочитать диалог: {esc(exc)}")
        preview.update(Text.from_markup("\n".join(lines)))

    def _selected_settings_proxy_target(self) -> tuple[str, str] | None:
        val = str(self.query_one("#settings-proxy-profile", Select).value)
        if not val or val == "__none__" or "/" not in val:
            return None
        prov, name = val.split("/", 1)
        return prov, name

    def _selected_settings_agy_profile(self) -> str | None:
        val = str(self.query_one("#settings-agy-profile", Select).value)
        if not val or val == "__none__":
            return None
        return val

    def refresh_network(self) -> None:
        if not self.is_running:
            return
        info = self._global_proxy_info or self.hub.proxy_info()
        self.query_one("#global-proxy-summary", Static).update(
            Text(
                f"Общий: {info['effective'].get('url', info['effective']['mode'])} · "
                f"источник {info['source']}"
            )
        )
        target = self._selected_settings_proxy_target()
        self.query_one("#profile-proxy", Button).disabled = target is None
        if target is not None:
            prov, name = target
            pinfo = self._profile_proxy_map.get((prov, name)) or self.hub.proxy_info(prov, name)
            eff = pinfo["effective"].get("url", pinfo["effective"]["mode"])
            message = f"Профиль {prov}/{name}: {eff} · источник {pinfo['source']}"
        else:
            message = "Создай или импортируй профиль для индивидуального прокси"
        self.query_one("#profile-proxy-summary", Static).update(Text(message))

    def refresh_agy_workspace(self) -> None:
        if not self.is_running:
            return
        info = self._agy_history_info or agy_history_status(self.hub)
        agy_auto = bool(info.get("automode", self.hub.automode_enabled("agy")))
        shared_on = bool(info.get("enabled", False))
        auto_btn = self.query_one("#agy-automode-toggle", Button)
        auto_btn.label = f"Автомод AGY: {'ВКЛ' if agy_auto else 'ВЫКЛ'}"
        auto_btn.set_class(agy_auto, "-active-toggle")

        hist_btn = self.query_one("#agy-history-toggle", Button)
        hist_btn.label = f"Общая история: {'ВКЛ' if shared_on else 'ВЫКЛ'}"
        hist_btn.set_class(shared_on, "-active-toggle")

        prof_modes = [f"{r['profile']}={r['mode']}" for r in (info.get("profiles") or [])]
        summary_lines = [
            f"Автомод (--dangerously-skip-permissions): {'включён' if agy_auto else 'выключен'}",
            f"Общая история: {'включена' if shared_on else 'выключена'} · {info['shared_base']}",
        ]
        if prof_modes:
            summary_lines.append("Профили AGY: " + ", ".join(prof_modes))
        self.query_one("#agy-workspace-summary", Static).update(Text("\n".join(summary_lines)))

        agy_name = self._selected_settings_agy_profile()
        toggle_btn = self.query_one("#agy-toggle", Button)
        toggle_btn.disabled = agy_name is None
        if agy_name is not None:
            try:
                ah = bool(self.hub.profile("agy", agy_name).get("autoheal", False))
                toggle_btn.label = f"Автохил ({agy_name}): {'ВКЛ' if ah else 'ВЫКЛ'}"
                toggle_btn.set_class(ah, "-active-toggle")
            except HubError:
                toggle_btn.label = "Автохил: ВЫКЛ"
        else:
            toggle_btn.label = "Автохил: ВЫКЛ"

    def edit_proxy(self, per_profile: bool) -> None:
        if per_profile:
            target = self._selected_settings_proxy_target()
            if target is None:
                raise HubError("Сначала создай или импортируй профиль")
            t_provider, t_name = target
            provider: str | None = t_provider
            name: str | None = t_name
            policy = self.hub.profile(t_provider, t_name).get("proxy", {"mode": "inherit"})
        else:
            provider, name = None, None
            policy = self.hub.store.read()["network"]["global"]

        def save(value: dict[str, Any] | None) -> None:
            if value is not None:
                try:
                    self.hub.set_proxy(value, provider, name)
                    self._global_proxy_info = self.hub.proxy_info()
                    if provider and name:
                        self._profile_proxy_map[(provider, name)] = self.hub.proxy_info(
                            provider, name
                        )
                    else:
                        self._profile_proxy_map = {
                            (p["provider"], p["name"]): self.hub.proxy_info(
                                p["provider"], p["name"]
                            )
                            for p in self._all_profiles
                        }
                    self.refresh_network()
                    self.refresh_account_inspector()
                    self.activity("Настройка прокси сохранена")
                except (HubError, OSError) as exc:
                    self.handle_error(exc)

        self.push_screen(
            ProxyForm(f"Прокси · {provider}/{name}" if name else "Общий прокси", policy), save
        )

    def selected_profile(self) -> str:
        row = self._safe_row(self.rows, self.query_one("#profiles", DataTable))
        if row is None:
            raise HubError("Сначала создай или импортируй профиль")
        return str(row["name"])

    def activity(self, message: str) -> None:
        if not self.is_running:
            return
        try:
            self.query_one("#activity", Static).update(Text(message))
        except NoMatches:
            pass

    def handle_error(self, exc: Exception) -> None:
        if not self.is_running:
            return
        self.activity(str(exc))
        self.notify(str(exc), severity="error")

    def edit_profile(self, mode: str) -> None:
        provider = self.provider
        try:
            if not self.hub.harness(provider).profile_supported():
                raise HubError("Для этого harness пока доступен запуск родного клиента")
            current = self.selected_profile() if mode == "rename" else ""
        except HubError as exc:
            self.handle_error(exc)
            return

        def save(value: dict[str, str] | None) -> None:
            if value is None:
                return
            try:
                if mode == "new":
                    self.hub.create(provider, value["name"])
                elif mode == "import":
                    self.hub.import_profile(provider, value["name"], Path(value["path"]))
                else:
                    self.hub.rename(provider, current, value["name"])
                self.refresh_data(keep_profile=value["name"])
                self.activity(f"Профиль {provider}/{value['name']} сохранён")
            except (HubError, OSError) as exc:
                self.handle_error(exc)

        self.push_screen(ProfileForm(mode, provider, current), save)

    def action_new_profile(self) -> None:
        self.edit_profile("new")

    def _validated_project(self) -> str:
        project = self.query_one("#project", Input).value
        if not Path(project).expanduser().is_dir():
            raise HubError("Выбери существующий каталог рабочего проекта")
        return project

    def request_launch(self, *, login: bool = False, native: bool = False) -> None:
        try:
            name = None if native else self.selected_profile()
            project = self._validated_project()
            if not login and not native:
                chosen_model = str(self.query_one("#account-model-select", Select).value)
                if chosen_model and chosen_model != "__default__":
                    self._dispatch_request(
                        Request("run-model", self.provider, name, cwd=project, model=chosen_model)
                    )
                    return
            self._dispatch_request(
                Request("login" if login else "run", self.provider, name, native, project)
            )
        except HubError as exc:
            self.handle_error(exc)

    def request_candidate_launch(self, best: bool = False) -> None:
        try:
            if not self.candidate_rows:
                raise HubError("Нет подходящих профилей для выбранной модели")
            chosen = (
                self.candidate_rows[0]
                if best
                else self._safe_row(
                    self.candidate_rows, self.query_one("#model-candidates", DataTable)
                )
            )
            if chosen is None:
                raise HubError("Нет подходящих профилей для выбранной модели")
            project = self._validated_project()
            self._dispatch_request(
                Request(
                    "run-model",
                    chosen["provider"],
                    chosen["profile"],
                    cwd=project,
                    model=chosen["resolved_model"],
                )
            )
        except HubError as exc:
            self.handle_error(exc)

    def request_session_resume(self, target_profile: str | None = None) -> None:
        try:
            row = self._safe_row(self.session_rows, self.query_one("#sessions-table", DataTable))
            if row is None:
                raise HubError("Диалогов пока не найдено")
            project = self._validated_project()
            self._dispatch_request(
                Request(
                    "resume",
                    row["provider"],
                    target_profile or row["profile"],
                    cwd=project,
                    session_id=row["session_id"],
                )
            )
        except HubError as exc:
            self.handle_error(exc)

    def action_launch(self) -> None:
        if self.screen is self.screen_stack[0]:
            active_tab = self.query_one(TabbedContent).active
            if active_tab == "models-tab":
                self.request_candidate_launch(best=False)
            elif active_tab == "sessions-tab":
                self.request_session_resume()
            else:
                self.request_launch()

    def action_login(self) -> None:
        self.request_launch(login=True)

    def action_usage(self) -> None:
        active_tab = self.query_one(TabbedContent).active
        if active_tab == "models-tab":
            prov_val = str(self.query_one("#registry-provider-filter", Select).value)
            self._start_probe_many(None if prov_val == "__all__" else prov_val)
            return
        if self.provider in ("codex", "agy", "claude") and self.rows:
            self._start_probe_many(self.provider)
        else:
            self._start_probe_many(None)

    def action_refresh(self) -> None:
        self.refresh_data()
        self.activity("Харнессы, аккаунты, модели и диалоги обновлены")

    def _toggle_current_automode(self) -> None:
        keep_name = self.selected_profile() if self.rows else None
        prof = self.hub.profile(self.provider, keep_name) if keep_name else None
        if prof is not None and "automode" in prof:
            res = self.hub.set_automode(not bool(prof["automode"]), self.provider, prof["name"])
        else:
            cur = self.hub.automode_enabled(self.provider)
            res = self.hub.set_automode(not cur, self.provider)
        self.refresh_data(keep_profile=keep_name)
        self.activity(f"Автомод ({self.provider}): {'включён' if res['effective'] else 'выключен'}")

    def _start_probe_single(self, provider: str, name: str) -> None:
        self.activity(f"Опрашиваю квоты и модели для {provider}/{name}…")

        async def _work() -> None:
            try:
                tel = await asyncio.to_thread(probe_profile_telemetry, self.hub, provider, name)
                if not self.is_running:
                    return
                self.refresh_data(keep_profile=name)
                rem = _fmt_quota(
                    tel.get("remaining_percent"),
                    known=tel.get("quota_source") in ("live", "cache", "cooldown"),
                )
                self.activity(
                    f"{provider}/{name}: {tel.get('plan_detail') or '—'} · "
                    f"остаток {rem} · моделей {len(tel.get('models') or [])}"
                )
            except asyncio.CancelledError:
                return
            except (HubError, OSError, subprocess.SubprocessError) as exc:
                self.handle_error(exc)

        self.run_worker(_work(), exclusive=True, group="probe")

    def _start_probe_many(self, provider: str | None = None) -> None:
        scope = provider or "всех харнессов"
        self.activity(f"Живой опрос квот и моделей ({scope})…")

        async def _work() -> None:
            try:
                results = await asyncio.to_thread(probe_all_profiles, self.hub, provider)
                if not self.is_running:
                    return
                ok_count = sum(1 for r in results if not r.get("probe_error"))
                self.refresh_data()
                self.activity(
                    f"Опрос завершён ({scope}): обновлено {ok_count}/{len(results)} аккаунтов"
                )
            except asyncio.CancelledError:
                return
            except (HubError, OSError, subprocess.SubprocessError) as exc:
                self.handle_error(exc)

        self.run_worker(_work(), exclusive=True, group="probe")

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        key = event.button.id
        try:
            if key in ("global-proxy", "profile-proxy"):
                self.edit_proxy(key == "profile-proxy")
            elif key == "install-harness":
                provider = self.provider
                name = self.selected_profile() if self.rows else None

                def install(value: dict[str, str | None] | None) -> None:
                    if value is not None:
                        self._dispatch_request(Request("install", provider, value["profile"]))

                self.push_screen(InstallForm(self.hub, provider, name), install)
            elif key in ("new", "import", "rename"):
                self.edit_profile(key)
            elif key == "run":
                self.request_launch()
            elif key == "launch-ide":
                project = self._validated_project()
                result = launch_ide(
                    self.hub,
                    self.provider,
                    profile=self.selected_profile(),
                    cwd=Path(project),
                )
                self.activity(f"Открыта {result['name']} (PID {result.get('pid', '—')})")
            elif key == "login":
                self.request_launch(login=True)
            elif key == "native":
                self.request_launch(native=True)
            elif key == "probe-provider":
                self._start_probe_many(self.provider)
            elif key == "probe-all":
                self.action_usage()
            elif key == "default":
                name = self.selected_profile()
                self.hub.set_default(self.provider, name)
                self.refresh_data(keep_profile=name)
            elif key == "run-smart":
                self.request_candidate_launch(best=True)
            elif key == "run-candidate":
                self.request_candidate_launch(best=False)
            elif key == "probe-agy":
                prov_val = str(self.query_one("#registry-provider-filter", Select).value)
                self._start_probe_many(None if prov_val == "__all__" else prov_val)
            elif key == "consult-open":
                model = str(self.query_one("#smart-model-select", Select).value)
                project = self._validated_project()
                choices = _model_choices_from_hub(self.hub)
                self.set_focus(None)

                def run_consult(data: dict[str, Any] | None) -> None:
                    if data:
                        self._dispatch_request(
                            Request(
                                "consult",
                                self.provider,
                                cwd=project,
                                model=data["model"],
                                note=data["question"],
                                council=data["council"],
                            )
                        )

                self.push_screen(ConsultForm(model, choices=choices), run_consult)
            elif key == "session-resume":
                self.request_session_resume()
            elif key == "session-resume-as":
                row = self._safe_row(
                    self.session_rows, self.query_one("#sessions-table", DataTable)
                )
                if row is None:
                    raise HubError("Выбери диалог для продолжения")
                prov_profiles = [
                    p["name"] for p in self._all_profiles if p["provider"] == row["provider"]
                ]
                if not prov_profiles:
                    raise HubError(f"Нет профилей для {row['provider']}")

                def do_resume_as(chosen_profile: str | None) -> None:
                    if chosen_profile:
                        self.request_session_resume(target_profile=chosen_profile)

                self.push_screen(
                    ResumeProfileForm(row, prov_profiles, str(row["profile"])), do_resume_as
                )
            elif key == "session-handoff":
                row = self._safe_row(
                    self.session_rows, self.query_one("#sessions-table", DataTable)
                )
                if row is None:
                    raise HubError("Выбери диалог для переноса")
                project = self._validated_project()
                choices = _model_choices_from_hub(self.hub)

                def do_handoff(data: dict[str, str] | None) -> None:
                    if data:
                        self._dispatch_request(
                            Request(
                                "handoff",
                                self.provider,
                                cwd=project,
                                session_id=data["session_id"],
                                model=data["model"],
                                note=data["note"],
                            )
                        )

                self.push_screen(HandoffForm(row, choices=choices), do_handoff)
            elif key == "session-scope":
                self.all_projects_sessions = not self.all_projects_sessions
                self.refresh_sessions_tab()
                scope_label = "все проекты" if self.all_projects_sessions else "текущий проект"
                self.activity(f"Диалоги: {scope_label}")
            elif key == "sessions-refresh":
                self.refresh_sessions_tab()
                self.activity("Диалоги обновлены")
            elif key == "remove":
                provider, remove_name = self.provider, self.selected_profile()

                def remove(confirmed: bool | None) -> None:
                    if confirmed:
                        try:
                            self.hub.remove(provider, remove_name)
                            self.refresh_data()
                            self.activity("Профиль убран из списка. Файлы сохранены.")
                        except (HubError, OSError) as exc:
                            self.handle_error(exc)

                self.push_screen(ConfirmRemove(remove_name), remove)
            elif key == "discover":
                candidates = self.hub.existing_profiles()
                if not candidates:
                    self.activity(
                        "Новых профилей AGY / Codex / Claude в стандартных каталогах не найдено"
                    )
                    return

                def import_existing(candidate: dict[str, str] | None) -> None:
                    if candidate:
                        try:
                            self.hub.import_profile(
                                candidate["provider"], candidate["name"], Path(candidate["path"])
                            )
                            self.refresh_data()
                            self.activity(
                                f"Импортирован {candidate['provider']}/{candidate['name']}"
                            )
                        except (HubError, OSError) as exc:
                            self.handle_error(exc)

                self.push_screen(ExistingProfiles(candidates), import_existing)
            elif key == "agy-toggle":
                agy_name = self._selected_settings_agy_profile()
                if agy_name is None:
                    raise HubError("Нет профилей AGY для настройки автохила")
                current = bool(self.hub.profile("agy", agy_name).get("autoheal", False))
                AgyMaintenance(self.hub).autoheal(agy_name, not current)
                self.refresh_data(keep_profile=agy_name if self.provider == "agy" else None)
                self.activity(f"Автохил {agy_name}: {'выключен' if current else 'включён'}")
            elif key == "toggle-automode":
                self._toggle_current_automode()
            elif key == "agy-automode-toggle":
                cur = self.hub.automode_enabled("agy")
                res = AgyMaintenance(self.hub).automode(not cur)
                keep_name = (
                    self.selected_profile() if (self.provider == "agy" and self.rows) else None
                )
                self.refresh_data(keep_profile=keep_name)
                self.activity(
                    "Автомод AGY (--dangerously-skip-permissions): "
                    f"{'включён' if res['effective'] else 'выключен'}"
                )
            elif key == "agy-history-toggle":
                cur = bool((self._agy_history_info or agy_history_status(self.hub))["enabled"])
                res = set_agy_shared_history(self.hub, not cur)
                keep_name = (
                    self.selected_profile() if (self.provider == "agy" and self.rows) else None
                )
                self.refresh_data(keep_profile=keep_name)
                if res["enabled"]:
                    busy = res.get("deferred_busy") or []
                    tail = f" · отложено занятых: {', '.join(busy)}" if busy else ""
                    self.activity(
                        f"Общая история AGY включена ({len(res.get('linked', []))} связано){tail}"
                    )
                else:
                    self.activity(
                        f"Общая история AGY выключена ({len(res.get('unlinked', []))} отвязано)"
                    )
            elif key == "agy-sync":
                res = sync_agy_profiles(self.hub)
                keep_name = (
                    self.selected_profile() if (self.provider == "agy" and self.rows) else None
                )
                self.refresh_data(keep_profile=keep_name)
                busy = res.get("deferred_busy") or []
                tail = f" · заняты: {', '.join(busy)}" if busy else ""
                self.activity(f"AGY sync: обновлено {len(res.get('synced', []))} профилей{tail}")
            elif key in ("agy-status", "agy-heal", "agy-restore"):
                self._dispatch_request(Request(key, self.provider))
        except (HubError, OSError) as exc:
            self.handle_error(exc)
