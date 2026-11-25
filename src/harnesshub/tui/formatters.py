from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.markup import escape
from rich.text import Text

from harnesshub.models import model_base_and_effort

# Graphite & Steel semantic palette
COLOR_OK = "#3fb950"
COLOR_WARN = "#d29922"
COLOR_CRIT = "#f85149"
COLOR_INFO = "#58a6ff"
COLOR_MUTED = "#768390"
COLOR_TEXT = "#e6edf3"


def esc(value: object) -> str:
    return escape(str(value))


def quota_color(rem: float | None, *, known: bool = True) -> str:
    if rem is None or not known:
        return COLOR_MUTED
    if rem <= 15.0:
        return COLOR_CRIT
    if rem <= 50.0:
        return COLOR_WARN
    return COLOR_OK


def _fmt_tokens(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _fmt_turns(value: int | None) -> str:
    return "—" if value is None else str(value)


def _fmt_reset(ts: float | int | None) -> str:
    if not ts or ts <= 0:
        return "—"
    dt = datetime.fromtimestamp(float(ts))
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")


def _fmt_quota(rem: float | None, *, known: bool = True) -> str:
    if rem is None or not known:
        return "—"
    if abs(rem - round(rem)) < 0.05:
        return f"{int(round(rem))}%"
    return f"{rem:.1f}%"


def fmt_quota_badge(rem: float | None, *, known: bool = True) -> Text:
    label = _fmt_quota(rem, known=known)
    return Text(label, style=quota_color(rem, known=known))


def _quota_bar(rem: float | None, width: int = 12) -> str:
    if rem is None:
        return "░" * width
    clamped = max(0.0, min(100.0, float(rem)))
    filled = round(clamped / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


def fmt_status_badge(tel: dict[str, Any]) -> Text:
    if tel.get("locked"):
        return Text("● занят", style=COLOR_CRIT)
    if not tel.get("logged_in", True):
        return Text("○ нет входа", style=COLOR_MUTED)
    if tel.get("in_cooldown"):
        return Text("● кулдаун", style=COLOR_CRIT)
    if tel.get("plan_expired"):
        return Text("● истёк", style=COLOR_WARN)
    if tel.get("quota_ready") is False:
        return Text("● лимит 0%", style=COLOR_CRIT)
    if tel.get("active"):
        return Text("● активен", style=COLOR_INFO)
    return Text("● готов", style=COLOR_OK)


def _find_window(
    windows: list[dict[str, Any]],
    *window_ids: str,
    label_substr: str | None = None,
) -> dict[str, Any] | None:
    for wid in window_ids:
        for w in windows:
            if str(w.get("window") or "").lower() == wid:
                return w
    if label_substr:
        low = label_substr.lower()
        for w in windows:
            if low in str(w.get("label") or "").lower():
                return w
    return None


def fmt_agy_group_quotas(tel: dict[str, Any]) -> tuple[Text, Text]:
    """Return separate (Gemini, Claude/GPT) quota cells for AGY accounts."""
    windows = tel.get("windows") or []
    if not windows and tel.get("in_cooldown"):
        r = _fmt_reset(tel.get("resets_at"))
        msg = f"0% ({r})" if r != "—" else "0%"
        return Text(msg, style=COLOR_CRIT), Text(msg, style=COLOR_CRIT)

    gem_w = _find_window(windows, "gemini", label_substr="gemini")
    if gem_w and gem_w.get("remaining_percent") is not None:
        g_rem = float(gem_w["remaining_percent"])
        g_reset = _fmt_reset(gem_w.get("resets_at"))
        gem_cell = Text(_fmt_quota(g_rem), style=quota_color(g_rem))
        if g_reset != "—":
            gem_cell.append(f" {g_reset}", style=COLOR_MUTED)
    else:
        gem_cell = Text("—", style=COLOR_MUTED)

    w5 = _find_window(windows, "3p-5h", label_substr="(5h)")
    w7 = _find_window(windows, "3p-weekly", label_substr="(weekly)")
    third_cell = Text()
    parts = 0
    if w5 and w5.get("remaining_percent") is not None:
        r5 = float(w5["remaining_percent"])
        third_cell.append("5ч:", style=COLOR_MUTED)
        third_cell.append(_fmt_quota(r5), style=quota_color(r5))
        parts += 1
    if w7 and w7.get("remaining_percent") is not None:
        r7 = float(w7["remaining_percent"])
        if parts:
            third_cell.append("  ", style=COLOR_MUTED)
        third_cell.append("7д:", style=COLOR_MUTED)
        third_cell.append(_fmt_quota(r7), style=quota_color(r7))
        if r7 <= 15.0:
            rst = _fmt_reset(w7.get("resets_at"))
            if rst != "—":
                third_cell.append(f" ({rst})", style=COLOR_MUTED)
        parts += 1
    if not parts:
        other = [w for w in windows if w is not gem_w and w.get("remaining_percent") is not None]
        if other:
            rem0 = float(other[0]["remaining_percent"])
            third_cell.append(_fmt_quota(rem0), style=quota_color(rem0))
        else:
            third_cell.append("—", style=COLOR_MUTED)
    return gem_cell, third_cell


def fmt_codex_group_quotas(tel: dict[str, Any]) -> tuple[Text, Text]:
    """Return separate (5h / primary, 7d / secondary) quota cells for Codex accounts."""
    windows = tel.get("windows") or []
    w_prim = _find_window(windows, "primary")
    w_sec = _find_window(windows, "secondary")
    if w_prim is None and windows:
        w_prim = windows[0]
    if w_sec is None and len(windows) > 1:
        w_sec = windows[1]

    def _cell(w: dict[str, Any] | None) -> Text:
        if not w or w.get("remaining_percent") is None:
            return Text("—", style=COLOR_MUTED)
        rem = float(w["remaining_percent"])
        lbl = str(w.get("label") or "")
        rst = _fmt_reset(w.get("resets_at"))
        cell = Text()
        if lbl and lbl not in ("5ч", "7д"):
            cell.append(f"{lbl}:", style=COLOR_MUTED)
        cell.append(_fmt_quota(rem), style=quota_color(rem))
        if rst != "—":
            cell.append(f" {rst}", style=COLOR_MUTED)
        return cell

    return _cell(w_prim), _cell(w_sec)


def fmt_cli_quota_summary(tel: dict[str, Any]) -> str:
    """Compact multi-group plain string for CLI `accounts` table."""
    prov = tel.get("provider")
    windows = tel.get("windows") or []
    if prov == "agy" and windows:
        gem_cell, third_cell = fmt_agy_group_quotas(tel)
        parts = []
        if gem_cell.plain != "—":
            parts.append(f"Gem {gem_cell.plain}")
        if third_cell.plain != "—":
            parts.append(f"3P {third_cell.plain}")
        if parts:
            return " · ".join(parts)
    if prov == "codex" and windows:
        c1, c2 = fmt_codex_group_quotas(tel)
        parts = []
        if c1.plain != "—":
            parts.append(c1.plain if ":" in c1.plain.split(" ", 1)[0] else f"5ч {c1.plain}")
        if c2.plain != "—":
            parts.append(c2.plain if ":" in c2.plain.split(" ", 1)[0] else f"7д {c2.plain}")
        if parts:
            return " · ".join(parts)
    q_known = tel.get("quota_source") in ("live", "cache", "cooldown")
    return _fmt_quota(tel.get("remaining_percent"), known=q_known)


def fmt_window_rows(tel: dict[str, Any]) -> list[str]:
    """Render each quota group on its own aligned line with a colored bar."""
    windows = tel.get("windows") or []
    if not windows:
        return []
    label_map = {
        "Claude and GPT models (weekly)": "Claude / GPT (7д)",
        "Claude and GPT models (5h)": "Claude / GPT (5ч)",
        "Gemini пул": "Gemini пул",
    }
    rows: list[str] = []
    for w in windows:
        rem = w.get("remaining_percent")
        rem_f = float(rem) if rem is not None else None
        color = quota_color(rem_f, known=rem_f is not None)
        raw_lbl = str(w.get("label") or w.get("window") or "квота")
        lbl = label_map.get(raw_lbl, raw_lbl)
        bar = _quota_bar(rem_f, 12)
        pct = _fmt_quota(rem_f, known=rem_f is not None)
        rst = _fmt_reset(w.get("resets_at"))
        rst_part = f"  [dim]сброс {esc(rst)}[/dim]" if rst != "—" else ""
        rows.append(
            f"  {esc(lbl):<18} [{color}]{bar}[/{color}] [bold {color}]{pct:>5}[/]{rst_part}"
        )
    return rows


def fmt_grouped_models_summary(
    models: list[dict[str, Any]], model_quotas: dict[str, Any] | None = None
) -> str:
    """Group effort variants so the account inspector stays concise and readable."""
    mq_map = model_quotas or {}
    groups: dict[str, list[str | None]] = {}
    group_quotas: dict[str, float | None] = {}
    order: list[str] = []
    for m in models:
        mid = str(m["id"])
        base_id, eff = model_base_and_effort(mid)
        if base_id not in groups:
            groups[base_id] = []
            group_quotas[base_id] = None
            order.append(base_id)
        if eff not in groups[base_id]:
            groups[base_id].append(eff)
        mq = mq_map.get(mid)
        if isinstance(mq, dict) and mq.get("remaining_percent") is not None:
            group_quotas[base_id] = float(mq["remaining_percent"])

    eff_short = {"low": "L", "medium": "M", "high": "H", "xhigh": "XH", "max": "MAX"}
    parts: list[str] = []
    for base_id in order:
        effs = [e for e in groups[base_id] if e is not None]
        q = group_quotas.get(base_id)
        if len(effs) >= 2:
            eff_tag = "/".join(eff_short.get(e, e) for e in effs)
            label = f"{esc(base_id)} [{eff_tag}]"
        elif len(effs) == 1:
            label = esc(f"{base_id}-{effs[0]}")
        else:
            label = esc(base_id)
        if q is not None:
            color = quota_color(q)
            label += f" [{color}]({_fmt_quota(q)})[/{color}]"
        parts.append(label)
    return ", ".join(parts)
