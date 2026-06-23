"""Generate a stakeholder prototype report without changing production output.

This script reads the latest cleaned CSV exports in ``reports/`` and produces:

* an enhanced mobile-first executive report prototype; and
* a side-by-side comparison page with the current HTML report.

It intentionally does not call ``analyze_calls()`` so it avoids database writes
and avoids regenerating the production report artifacts.
"""

from __future__ import annotations

import json
import re
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    import anthropic
except ImportError:  # Anthropic SDK is optional; MiniMax call will be skipped if missing.
    anthropic = None

from call_log_analyzer import (
    analyze_out_of_hours,
    clean_phone_for_match,
    generate_plots,
)


ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
TEMPLATES_DIR = ROOT / "templates"

RETAIL_ABANDONMENT_TARGET = 10.0
TRADE_ABANDONMENT_TARGET = 5.0
OOH_WATCH_THRESHOLD = 100
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"


def _post_json_without_env_proxy(url: str, headers: dict, payload: dict, timeout: int) -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    return session.post(url, headers=headers, json=payload, timeout=timeout)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _latest_current_report() -> Path | None:
    reports = sorted(
        REPORTS_DIR.glob("call_report_*.html"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for report in reports:
        if "_test_" not in report.name and "comparison" not in report.name:
            return report
    return None


def _fmt_date(value: Any) -> str:
    if pd.isna(value):
        return "N/A"
    return pd.to_datetime(value).strftime("%d/%m/%Y")


def _fmt_datetime(value: Any) -> str:
    if pd.isna(value):
        return "N/A"
    return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M")


def _parse_wait_seconds(value: Any) -> int:
    if pd.isna(value):
        return 0
    parts = str(value).split(":")
    if len(parts) != 3:
        return 0
    try:
        hours, minutes, seconds = [int(float(part)) for part in parts]
    except ValueError:
        return 0
    return hours * 3600 + minutes * 60 + seconds


def _format_duration(seconds: float | int) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def _change(current: float, previous: float, higher_is_good: bool = True, unit: str = "") -> dict:
    delta = current - previous
    pct = (delta / previous * 100) if previous else None
    if abs(delta) < 0.05:
        status = "neutral"
    elif (delta > 0 and higher_is_good) or (delta < 0 and not higher_is_good):
        status = "good"
    else:
        status = "bad"

    if unit == "pp":
        label = f"{delta:+.1f} pp"
    elif isinstance(current, float) or isinstance(previous, float):
        label = f"{delta:+.1f}{unit}"
    else:
        label = f"{int(delta):+,}"

    pct_label = f"{pct:+.1f}%" if pct is not None else "new"
    return {"delta": delta, "pct": pct, "label": label, "pct_label": pct_label, "status": status}


def _metric_summary(main_df: pd.DataFrame, abandoned_df: pd.DataFrame) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    main = main_df.copy()
    abandoned = abandoned_df.copy()

    main["call_start"] = pd.to_datetime(main["call_start"], errors="coerce")
    main = main.dropna(subset=["call_start"])
    if not abandoned.empty:
        abandoned["Call Time"] = pd.to_datetime(abandoned["Call Time"], errors="coerce")
        abandoned = abandoned.dropna(subset=["Call Time"])

    max_date = main["call_start"].max()
    max_date_norm = max_date.normalize()
    this_week_end = max_date_norm + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    this_week_start = max_date_norm - pd.Timedelta(days=6)
    last_week_end = this_week_start - pd.Timedelta(seconds=1)
    last_week_start = this_week_start - pd.Timedelta(days=7)

    main["week"] = 3
    main.loc[(main["call_start"] >= this_week_start) & (main["call_start"] <= this_week_end), "week"] = 1
    main.loc[(main["call_start"] >= last_week_start) & (main["call_start"] <= last_week_end), "week"] = 2

    if not abandoned.empty:
        abandoned["week"] = 3
        abandoned.loc[
            (abandoned["Call Time"] >= this_week_start) & (abandoned["Call Time"] <= this_week_end),
            "week",
        ] = 1
        abandoned.loc[
            (abandoned["Call Time"] >= last_week_start) & (abandoned["Call Time"] <= last_week_end),
            "week",
        ] = 2

    main_12 = main[main["week"].isin([1, 2])].copy()
    abandoned_12 = abandoned[abandoned["week"].isin([1, 2])].copy() if not abandoned.empty else pd.DataFrame()

    def count_main(week: int, customer_type: str | None = None) -> int:
        frame = main_12[main_12["week"] == week]
        if customer_type:
            frame = frame[frame["customer_type"] == customer_type]
        return int(len(frame))

    def count_abandoned(week: int, customer_type: str | None = None) -> int:
        if abandoned_12.empty:
            return 0
        frame = abandoned_12[abandoned_12["week"] == week]
        if customer_type:
            frame = frame[frame["customer_type"] == customer_type]
        return int(len(frame))

    def rate(abandoned_count: int, main_count: int) -> float:
        total = abandoned_count + main_count
        return round(abandoned_count / total * 100, 1) if total else 0.0

    ooh = analyze_out_of_hours(main_12, abandoned_12)

    metrics = {
        "max_date": _fmt_date(max_date),
        "this_week_start": this_week_start.strftime("%Y-%m-%d"),
        "this_week_end": max_date_norm.strftime("%Y-%m-%d"),
        "last_week_start": last_week_start.strftime("%Y-%m-%d"),
        "last_week_end": (this_week_start - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        "week1_main_calls": count_main(1),
        "week2_main_calls": count_main(2),
        "week1_retail_calls": count_main(1, "retail"),
        "week2_retail_calls": count_main(2, "retail"),
        "week1_trade_calls": count_main(1, "trade"),
        "week2_trade_calls": count_main(2, "trade"),
        "week1_retail_abandoned": count_abandoned(1, "retail"),
        "week2_retail_abandoned": count_abandoned(2, "retail"),
        "week1_trade_abandoned": count_abandoned(1, "trade"),
        "week2_trade_abandoned": count_abandoned(2, "trade"),
    }

    metrics["week1_abandoned_total"] = metrics["week1_retail_abandoned"] + metrics["week1_trade_abandoned"]
    metrics["week2_abandoned_total"] = metrics["week2_retail_abandoned"] + metrics["week2_trade_abandoned"]
    metrics["week1_calls"] = metrics["week1_main_calls"] + metrics["week1_abandoned_total"]
    metrics["week2_calls"] = metrics["week2_main_calls"] + metrics["week2_abandoned_total"]
    metrics["week1_retail_abandonment_rate"] = rate(
        metrics["week1_retail_abandoned"], metrics["week1_retail_calls"]
    )
    metrics["week2_retail_abandonment_rate"] = rate(
        metrics["week2_retail_abandoned"], metrics["week2_retail_calls"]
    )
    metrics["week1_trade_abandonment_rate"] = rate(
        metrics["week1_trade_abandoned"], metrics["week1_trade_calls"]
    )
    metrics["week2_trade_abandonment_rate"] = rate(
        metrics["week2_trade_abandoned"], metrics["week2_trade_calls"]
    )
    metrics.update(ooh)

    return metrics, main_12, abandoned_12


def _build_movements(metrics: dict) -> list[dict]:
    rows = [
        ("Total calls", metrics["week1_calls"], metrics["week2_calls"], "", True),
        ("Retail calls", metrics["week1_retail_calls"], metrics["week2_retail_calls"], "", True),
        ("Trade calls", metrics["week1_trade_calls"], metrics["week2_trade_calls"], "", True),
        ("Abandoned calls", metrics["week1_abandoned_total"], metrics["week2_abandoned_total"], "", False),
        (
            "Retail abandonment",
            metrics["week1_retail_abandonment_rate"],
            metrics["week2_retail_abandonment_rate"],
            "pp",
            False,
        ),
        (
            "Trade abandonment",
            metrics["week1_trade_abandonment_rate"],
            metrics["week2_trade_abandonment_rate"],
            "pp",
            False,
        ),
    ]
    movements = []
    for label, current, previous, unit, higher_is_good in rows:
        value = f"{current:.1f}%" if unit == "pp" else f"{int(current):,}"
        previous_value = f"{previous:.1f}%" if unit == "pp" else f"{int(previous):,}"
        movements.append(
            {
                "label": label,
                "value": value,
                "previous": previous_value,
                "change": _change(current, previous, higher_is_good, unit),
            }
        )
    return movements


def _build_attention_flags(metrics: dict, abandoned_12: pd.DataFrame) -> list[dict]:
    flags = []
    retail_rate = metrics["week1_retail_abandonment_rate"]
    trade_rate = metrics["week1_trade_abandonment_rate"]

    if retail_rate > RETAIL_ABANDONMENT_TARGET:
        flags.append({
            "level": "risk",
            "title": "Retail abandonment above target",
            "detail": f"{retail_rate:.1f}% vs target below {RETAIL_ABANDONMENT_TARGET:.0f}%",
        })
    if trade_rate > TRADE_ABANDONMENT_TARGET:
        flags.append({
            "level": "risk",
            "title": "Trade abandonment above target",
            "detail": f"{trade_rate:.1f}% vs target below {TRADE_ABANDONMENT_TARGET:.0f}%",
        })
    if metrics["ooh_total"] > OOH_WATCH_THRESHOLD:
        flags.append({
            "level": "watch",
            "title": "Out-of-hours demand is material",
            "detail": f"{metrics['ooh_total']:,} calls outside operating hours",
        })

    if not abandoned_12.empty:
        week1_trade = abandoned_12[
            (abandoned_12["week"] == 1) & (abandoned_12["customer_type"] == "trade")
        ].copy()
        if not week1_trade.empty:
            phones = week1_trade["Caller ID"].apply(clean_phone_for_match)
            repeats = sum(1 for count in Counter(phones).values() if count > 1)
            if repeats:
                flags.append({
                    "level": "risk",
                    "title": "Repeat abandoned trade customers",
                    "detail": f"{repeats} phone number(s) abandoned more than once this week",
                })

            day_counts = week1_trade["Call Time"].dt.day_name().value_counts()
            if not day_counts.empty:
                flags.append({
                    "level": "watch",
                    "title": "Peak trade abandonment day",
                    "detail": f"{day_counts.index[0]} had {int(day_counts.iloc[0])} abandoned trade call(s)",
                })

    if not flags:
        flags.append({
            "level": "good",
            "title": "No threshold exceptions detected",
            "detail": "Current prototype thresholds did not flag a priority issue.",
        })
    return flags


def _build_kpi_cards(metrics: dict) -> list[dict]:
    return [
        {
            "label": "Total Calls",
            "value": f"{metrics['week1_calls']:,}",
            "subtext": "Including abandoned calls",
            "change": _change(metrics["week1_calls"], metrics["week2_calls"], True),
        },
        {
            "label": "Retail Calls",
            "value": f"{metrics['week1_retail_calls']:,}",
            "subtext": "Main call log volume",
            "change": _change(metrics["week1_retail_calls"], metrics["week2_retail_calls"], True),
        },
        {
            "label": "Trade Calls",
            "value": f"{metrics['week1_trade_calls']:,}",
            "subtext": "Main call log volume",
            "change": _change(metrics["week1_trade_calls"], metrics["week2_trade_calls"], True),
        },
        {
            "label": "Abandoned Calls",
            "value": f"{metrics['week1_abandoned_total']:,}",
            "subtext": "Retail and trade combined",
            "change": _change(metrics["week1_abandoned_total"], metrics["week2_abandoned_total"], False),
        },
        {
            "label": "Retail Abandonment",
            "value": f"{metrics['week1_retail_abandonment_rate']:.1f}%",
            "subtext": f"Target below {RETAIL_ABANDONMENT_TARGET:.0f}%",
            "change": _change(
                metrics["week1_retail_abandonment_rate"],
                metrics["week2_retail_abandonment_rate"],
                False,
                "pp",
            ),
        },
        {
            "label": "Trade Abandonment",
            "value": f"{metrics['week1_trade_abandonment_rate']:.1f}%",
            "subtext": f"Target below {TRADE_ABANDONMENT_TARGET:.0f}%",
            "change": _change(
                metrics["week1_trade_abandonment_rate"],
                metrics["week2_trade_abandonment_rate"],
                False,
                "pp",
            ),
        },
    ]


def _deterministic_summary(metrics: dict, flags: list[dict]) -> tuple[list[str], list[str]]:
    movements = _build_movements(metrics)
    movement_by_label = {row["label"]: row for row in movements}
    summary = [
        (
            f"Total call demand was {metrics['week1_calls']:,}, "
            f"{movement_by_label['Total calls']['change']['label']} "
            f"({movement_by_label['Total calls']['change']['pct_label']}) versus last week."
        ),
        (
            f"Retail abandonment is {metrics['week1_retail_abandonment_rate']:.1f}% "
            f"and trade abandonment is {metrics['week1_trade_abandonment_rate']:.1f}%."
        ),
        (
            f"Out-of-hours demand reached {metrics['ooh_total']:,} calls, "
            f"with {metrics['ooh_after_closing']:,} after closing."
        ),
    ]

    flagged = [flag for flag in flags if flag["level"] in {"risk", "watch"}]
    if flagged:
        summary.append(f"Top exception: {flagged[0]['title'].lower()} ({flagged[0]['detail']}).")

    recommendations = []
    if metrics["week1_retail_abandonment_rate"] > RETAIL_ABANDONMENT_TARGET:
        recommendations.append("Review retail queue coverage on the highest-abandonment days.")
    if metrics["week1_trade_abandonment_rate"] > TRADE_ABANDONMENT_TARGET:
        recommendations.append("Prioritise callback follow-up for named trade customers in the list below.")
    if metrics["ooh_total"] > OOH_WATCH_THRESHOLD:
        recommendations.append("Track out-of-hours demand for service-hours or callback-process decisions.")
    if not recommendations:
        recommendations.append("Keep monitoring abandonment and out-of-hours demand against agreed thresholds.")
    return summary, recommendations


def _summary_messages(metrics: dict, flags: list[dict]) -> tuple[str, list[dict]]:
    ai_payload = {
        "this_week": {
            "date_range": f"{metrics['this_week_start']} to {metrics['this_week_end']}",
            "total_calls": metrics["week1_calls"],
            "retail_calls": metrics["week1_retail_calls"],
            "trade_calls": metrics["week1_trade_calls"],
            "abandoned_calls": metrics["week1_abandoned_total"],
            "retail_abandonment_rate": metrics["week1_retail_abandonment_rate"],
            "trade_abandonment_rate": metrics["week1_trade_abandonment_rate"],
        },
        "last_week": {
            "date_range": f"{metrics['last_week_start']} to {metrics['last_week_end']}",
            "total_calls": metrics["week2_calls"],
            "retail_calls": metrics["week2_retail_calls"],
            "trade_calls": metrics["week2_trade_calls"],
            "abandoned_calls": metrics["week2_abandoned_total"],
            "retail_abandonment_rate": metrics["week2_retail_abandonment_rate"],
            "trade_abandonment_rate": metrics["week2_trade_abandonment_rate"],
        },
        "out_of_hours": {
            "total_calls": metrics["ooh_total"],
            "before_opening": metrics["ooh_before_opening"],
            "after_closing": metrics["ooh_after_closing"],
        },
        "attention_flags": flags,
    }
    system = (
        "You write concise executive summaries for weekly call-centre reports. "
        "This week is the current reporting period and last week is the comparison period. "
        "Calculate movement as this_week minus last_week. Use only the provided metrics. Return JSON with keys "
        "executive_summary and recommendations, each an array of 3-5 short strings. "
        "Do not invent causes, names, or actions not supported by the data."
    )
    user = json.dumps(ai_payload, default=str)
    return system, [{"role": "user", "content": user}]


def _summary_messages_openai(metrics: dict, flags: list[dict]) -> list[dict]:
    """OpenAI-style messages list with system role included."""
    system, messages = _summary_messages(metrics, flags)
    return [{"role": "system", "content": system}, *messages]


def _parse_summary_json(text: str) -> tuple[list[str], list[str]]:
    """Parse a model's JSON reply, tolerating markdown fences or leading prose."""
    if not text or not text.strip():
        raise ValueError("Model returned empty text.")
    candidate = text.strip()

    # Strip markdown code fences the model sometimes wraps JSON in.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)

    # If there's prose around the JSON, grab the outermost {...} block.
    if not candidate.lstrip().startswith("{"):
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match:
            candidate = match.group(0)

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        preview = candidate[:200].replace("\n", " ")
        raise ValueError(f"Model response was not valid JSON: {exc}. Preview: {preview!r}") from exc

    summary = [str(x) for x in parsed.get("executive_summary", []) if str(x).strip()]
    recommendations = [str(x) for x in parsed.get("recommendations", []) if str(x).strip()]
    if not summary or not recommendations:
        raise ValueError("Model response did not include both executive_summary and recommendations.")
    return summary[:5], recommendations[:5]


def _error_text(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:240] + ("..." if len(text) > 240 else "")


def _model_result(
    provider: str,
    model: str,
    summary: list[str],
    recommendations: list[str],
    status: str = "ok",
    error: str = "",
) -> dict:
    return {
        "provider": provider,
        "model": model,
        "summary": summary,
        "recommendations": recommendations,
        "status": status,
        "error": error,
    }


def _call_openai_for_summary(metrics: dict, flags: list[dict]) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL") or os.getenv("REPORT_AI_MODEL") or DEFAULT_OPENAI_MODEL
    if not api_key:
        return _model_result("OpenAI", model, [], [], "skipped", "OPENAI_API_KEY not set.")

    payload = {
        "model": model,
        "input": _summary_messages_openai(metrics, flags),
        "text": {"format": {"type": "json_object"}},
    }
    try:
        response = _post_json_without_env_proxy(
            "https://api.openai.com/v1/responses",
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload,
            30,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("output_text")
        if not text:
            parts = []
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        parts.append(content.get("text", ""))
            text = "".join(parts)
        summary, recommendations = _parse_summary_json(text)
        return _model_result("OpenAI", model, summary, recommendations)
    except Exception as exc:
        print(f"OpenAI summary unavailable: {exc}")
        return _model_result("OpenAI", model, [], [], "error", _error_text(exc))


def _call_minimax_for_summary(metrics: dict, flags: list[dict]) -> dict:
    """Call MiniMax via its Anthropic-compatible endpoint (works with Max plan / Token Plan keys)."""
    api_key = os.getenv("MINIMAX_API_KEY")
    model = os.getenv("MINIMAX_MODEL") or DEFAULT_MINIMAX_MODEL
    base_url = os.getenv("MINIMAX_API_BASE") or "https://api.minimax.io/anthropic"

    if not api_key:
        return _model_result("MiniMax", model, [], [], "skipped", "MINIMAX_API_KEY not set.")
    if anthropic is None:
        return _model_result(
            "MiniMax",
            model,
            [],
            [],
            "skipped",
            "anthropic SDK not installed. Run: pip install anthropic",
        )

    system, messages = _summary_messages(metrics, flags)
    try:
        client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=messages,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        text = "".join(text_parts).strip()
        if not text:
            block_types = [getattr(b, "type", None) for b in response.content]
            stop_reason = getattr(response, "stop_reason", None)
            raise ValueError(
                f"MiniMax response had no text blocks (content types: {block_types}, stop_reason: {stop_reason})."
            )
        summary, recommendations = _parse_summary_json(text)
        return _model_result("MiniMax", model, summary, recommendations)
    except Exception as exc:
        print(f"MiniMax summary unavailable: {exc}")
        return _model_result("MiniMax", model, [], [], "error", _error_text(exc))


def _build_model_summaries(metrics: dict, flags: list[dict]) -> list[dict]:
    deterministic_summary, deterministic_recommendations = _deterministic_summary(metrics, flags)
    return [
        _call_openai_for_summary(metrics, flags),
        _call_minimax_for_summary(metrics, flags),
        _model_result(
            "Rules",
            "Deterministic fallback",
            deterministic_summary,
            deterministic_recommendations,
        ),
    ]


def _build_chart_takeaways(main_12: pd.DataFrame, abandoned_12: pd.DataFrame) -> list[str]:
    takeaways = []
    if not main_12.empty:
        main = main_12.copy()
        main["day_name"] = main["call_start"].dt.day_name()
        week1 = main[main["week"] == 1]
        if not week1.empty and "ringing_total_sec" in week1.columns:
            wait_by_day = week1.groupby("day_name")["ringing_total_sec"].mean().sort_values(ascending=False)
            takeaways.append(
                f"Average waiting time was highest on {wait_by_day.index[0]} at "
                f"{_format_duration(wait_by_day.iloc[0])}."
            )
        if not week1.empty and "talking_total_sec" in week1.columns:
            avg_talk = week1["talking_total_sec"].mean()
            takeaways.append(f"Average talk time this week was {_format_duration(avg_talk)}.")

    if not abandoned_12.empty:
        abandoned = abandoned_12[abandoned_12["week"] == 1].copy()
        if not abandoned.empty:
            abandoned["day_name"] = abandoned["Call Time"].dt.day_name()
            day_counts = abandoned["day_name"].value_counts()
            takeaways.append(
                f"Abandoned calls were highest on {day_counts.index[0]} "
                f"with {int(day_counts.iloc[0])} abandoned call(s)."
            )
            abandoned["wait_sec"] = abandoned["Waiting Time"].apply(_parse_wait_seconds)
            takeaways.append(
                f"Average wait before abandonment was {_format_duration(abandoned['wait_sec'].mean())}."
            )

    return takeaways[:4] or ["Chart takeaways will appear once there is enough call and abandonment data."]


def _load_trade_name_map() -> dict[str, str]:
    path = ROOT / "data" / "trade_customer_numbers.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    if not {"phone_number", "customer_name"}.issubset(df.columns):
        return {}
    return {
        clean_phone_for_match(row["phone_number"]): str(row["customer_name"]).upper()
        for _, row in df.iterrows()
        if str(row.get("customer_name", "")).strip().upper() not in {"", "UNKNOWN"}
    }


def _build_followups(abandoned_12: pd.DataFrame) -> list[dict]:
    if abandoned_12.empty:
        return []
    trade_names = _load_trade_name_map()
    trade = abandoned_12[abandoned_12["customer_type"] == "trade"].copy()
    if trade.empty:
        return []

    trade["clean_phone"] = trade["Caller ID"].apply(clean_phone_for_match)
    trade["customer_name"] = trade["clean_phone"].map(trade_names)
    trade = trade.dropna(subset=["customer_name"])

    followups = []
    week1 = trade[trade["week"] == 1]
    for phone, group in week1.groupby("clean_phone"):
        previous_count = int(len(trade[(trade["week"] == 2) & (trade["clean_phone"] == phone)]))
        count = int(len(group))
        most_recent = group["Call Time"].max()
        action = "Recurring issue" if previous_count or count > 1 else "Call back"
        followups.append(
            {
                "customer_name": str(group["customer_name"].iloc[0]),
                "phone": str(group["Caller ID"].iloc[0]),
                "count": count,
                "most_recent": _fmt_datetime(most_recent),
                "previous_count": previous_count,
                "action": action,
            }
        )

    followups.sort(key=lambda item: (item["action"] != "Recurring issue", -item["count"], item["customer_name"]))
    return followups[:15]


def _data_confidence(main_df: pd.DataFrame, abandoned_df: pd.DataFrame) -> list[dict]:
    unique_call_ids = int(main_df["Call ID"].nunique()) if "Call ID" in main_df.columns else len(main_df)
    duplicates_removed = max(int(len(main_df) - unique_call_ids), 0)
    return [
        {"label": "Cleaned call rows", "value": f"{len(main_df):,}"},
        {"label": "Unique call IDs", "value": f"{unique_call_ids:,}"},
        {"label": "Duplicate call IDs in export", "value": f"{duplicates_removed:,}"},
        {"label": "Cleaned abandoned rows", "value": f"{len(abandoned_df):,}"},
        {"label": "Report generated", "value": datetime.now().strftime("%d/%m/%Y %H:%M")},
    ]


def _render_comparison(current_report: Path | None, enhanced_report: Path) -> Path:
    current_name = current_report.name if current_report else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Call Report Comparison</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; color: #1f2933; background: #eef2f5; }}
    header {{ padding: 18px; background: #ffffff; border-bottom: 1px solid #cfd8df; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    p {{ margin: 0; color: #52616f; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; height: calc(100vh - 86px); box-sizing: border-box; }}
    .panel {{ min-width: 0; background: #fff; border: 1px solid #cfd8df; }}
    .panel-title {{ padding: 10px 12px; font-weight: 700; border-bottom: 1px solid #cfd8df; }}
    iframe {{ width: 100%; height: calc(100% - 42px); border: 0; background: #fff; }}
    a {{ color: #116466; }}
    @media (max-width: 850px) {{
      .grid {{ display: block; height: auto; padding: 10px; }}
      .panel {{ height: 82vh; margin-bottom: 12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Current vs Proposed Weekly Call Report</h1>
    <p>Open directly: <a href="{current_name}">current report</a> | <a href="{enhanced_report.name}">proposed report</a></p>
  </header>
  <main class="grid">
    <section class="panel">
      <div class="panel-title">Current report</div>
      <iframe src="{current_name}" title="Current report"></iframe>
    </section>
    <section class="panel">
      <div class="panel-title">Proposed stakeholder report</div>
      <iframe src="{enhanced_report.name}" title="Proposed stakeholder report"></iframe>
    </section>
  </main>
</body>
</html>
"""
    output = REPORTS_DIR / "stakeholder_report_comparison.html"
    output.write_text(html, encoding="utf-8")
    return output


def generate_test_report() -> tuple[Path, Path]:
    load_dotenv(ROOT / ".env")
    REPORTS_DIR.mkdir(exist_ok=True)

    main_df = _read_csv(REPORTS_DIR / "call_logs_cleaned.csv")
    abandoned_df = _read_csv(REPORTS_DIR / "abandoned_logs_cleaned.csv")
    if main_df.empty:
        raise RuntimeError("reports/call_logs_cleaned.csv is missing or empty. Run the normal report first.")

    metrics, main_12, abandoned_12 = _metric_summary(main_df, abandoned_df)
    movements = _build_movements(metrics)
    flags = _build_attention_flags(metrics, abandoned_12)
    model_summaries = _build_model_summaries(metrics, flags)

    plots = generate_plots(main_12, abandoned_12)
    template = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    ).get_template("call_report_stakeholder_test.html.j2")
    html = template.render(
        metrics=metrics,
        movements=movements,
        attention_flags=flags,
        kpi_cards=_build_kpi_cards(metrics),
        model_summaries=model_summaries,
        chart_takeaways=_build_chart_takeaways(main_12, abandoned_12),
        followups=_build_followups(abandoned_12),
        data_confidence=_data_confidence(main_df, abandoned_df),
        plots=plots,
        raw_data=main_12.head(10),
        abandoned_logs=abandoned_12.head(10) if not abandoned_12.empty else pd.DataFrame(),
        generated_at=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )

    date_token = pd.to_datetime(metrics["this_week_end"]).strftime("%d_%m_%Y")
    enhanced_path = REPORTS_DIR / f"call_report_{date_token}_test_stakeholder.html"
    enhanced_path.write_text(html, encoding="utf-8")
    comparison_path = _render_comparison(_latest_current_report(), enhanced_path)
    return enhanced_path, comparison_path


if __name__ == "__main__":
    enhanced, comparison = generate_test_report()
    print(f"Enhanced test report: {enhanced}")
    print(f"Comparison report:    {comparison}")
