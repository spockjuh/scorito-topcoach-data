#!/usr/bin/env python3
"""
Scorito TopCoach data harvester.

Haalt uitsluitend PUBLIEKE, login-vrije endpoints op (geen sessie, geen cookies,
geen accountgegevens). Schrijft per competitie een schone JSON-samenvatting naar
data/{competitie}/latest.json, plus een append-only geschiedenisbestand met
speler-punten voor trendanalyse.

Bronvermelding van de endpoints: scorito-topcoach-api-catalogus-v1.md
(project "The Whatsubleague").
"""

import json
import time
import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

COMPETITIONS = {
    "nl":  {"marketId": 312, "name": "TopCoach NL (Eredivisie)"},
    "kkd": {"marketId": 313, "name": "TopCoach KKD (Keuken Kampioen Divisie)"},
    "uk":  {"marketId": 314, "name": "TopCoach UK (Premier League)"},
    "be":  {"marketId": 315, "name": "TopCoach BE (Pro League)"},
    "de":  {"marketId": 328, "name": "TopCoach DE (Bundesliga) — mogelijk nog niet live"},
}

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "scorito-topcoach-data-bot/1.0 (persoonlijk project, "
                  "https://github.com/spockjuh/scorito-topcoach-data)",
}

REQUEST_DELAY_SECONDS = 1.0  # nette pauze tussen calls, geen burst-gedrag
REQUEST_TIMEOUT = 20
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def get_json(url: str):
    """Haalt een endpoint op en geeft de geparste JSON terug, of None bij falen."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        time.sleep(REQUEST_DELAY_SECONDS)
        if resp.status_code == 200 and resp.text.strip():
            return resp.json()
        print(f"  [skip] {url} -> status {resp.status_code}")
        return None
    except Exception as exc:  # noqa: BLE001 - bewust breed: elke competitie moet onafhankelijk falen
        print(f"  [error] {url} -> {exc}")
        return None


def unwrap(payload):
    """Sommige endpoints wikkelen data in {ResultCode, ErrorMessage, Content}."""
    if isinstance(payload, dict) and "Content" in payload and "ResultCode" in payload:
        return payload["Content"]
    return payload


def find_current_market_round(market_structure, game_phase_id):
    """
    Best-effort poging om de actuele marketRoundId te vinden uit marketstructure.
    Faalt stil (geeft None) als het veldschema afwijkt van wat verwacht wordt —
    de rest van de run gaat dan gewoon door zonder ronde-specifieke data.
    """
    if not market_structure:
        return None
    try:
        rounds = None
        for key in ("MarketRounds", "Rounds", "marketRounds"):
            if isinstance(market_structure, dict) and key in market_structure:
                rounds = market_structure[key]
                break
        if not rounds:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        best = None
        for r in rounds:
            deadline_str = r.get("DeadlineDateTime") or r.get("Deadline")
            if not deadline_str:
                continue
            deadline = datetime.datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            if deadline >= now:
                best = r
                break
        if best:
            return best.get("Id") or best.get("MarketRoundId")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] kon marketRoundId niet bepalen: {exc}")
    return None


# ---------------------------------------------------------------------------
# Per-competitie ophaal-logica
# ---------------------------------------------------------------------------

def fetch_competition(key: str, market_id: int, name: str):
    print(f"== {name} (marketId={market_id}) ==")
    result = {
        "competition": key,
        "marketId": market_id,
        "name": name,
        "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "unknown",
    }

    market_structure = unwrap(get_json(
        f"https://platform.scorito.com/market/v2.0/marketstructure/{market_id}"))
    if market_structure is None:
        # Markt bestaat (nog) niet of is niet actief (bv. TopCoach DE vóór lancering)
        result["status"] = "unavailable"
        return result

    result["status"] = "active"
    result["marketStructure"] = market_structure

    game_phase = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/gamePhase/{market_id}"))
    result["gamePhase"] = game_phase

    current_phase = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/gamePhase/currentbeforedeadline/{market_id}"))
    result["currentPhase"] = current_phase
    game_phase_id = None
    if isinstance(current_phase, dict):
        game_phase_id = current_phase.get("Id") or current_phase.get("GamePhaseId")

    result["players"] = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/teamplayerenriched/market/{market_id}"))

    result["playerPoints"] = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/marketplayerpoints/{market_id}"))

    result["transferFeed"] = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/playermutation/{market_id}"))

    market_round_id = find_current_market_round(market_structure, game_phase_id)
    result["currentMarketRoundId"] = market_round_id
    if market_round_id:
        result["marketRoundMatches"] = unwrap(get_json(
            f"https://footballmanager-query.scorito.com/v1.0/marketroundmatch/{market_round_id}"))
        result["marketRoundEnriched"] = unwrap(get_json(
            f"https://footballmanager-query.scorito.com/v1.0/marketroundenriched/{market_round_id}"))
    else:
        print("  [info] geen actuele marketRoundId gevonden — ronde-specifieke data overgeslagen")

    return result


def fetch_help_content_once(key: str, market_id: int):
    """
    Spelregels/puntentabel veranderen vrijwel nooit — alleen ophalen als we 'm nog
    niet hebben, om het helpcentrum niet onnodig te belasten.
    """
    target = DATA_DIR / key / "spelregels.json"
    if target.exists():
        return
    content = unwrap(get_json(
        f"https://platform.scorito.com/help/v1.0/helpcontent/market/{market_id}"))
    if content is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    print(f"  [ok] spelregels.json opgeslagen voor {key}")


def append_history(key: str, snapshot: dict):
    """Voegt een compacte punten-snapshot toe aan de geschiedenis (voor trends)."""
    hist_dir = DATA_DIR / key / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    hist_file = hist_dir / f"{date_str}.json"
    if hist_file.exists():
        return  # al een snapshot van vandaag, niet overschrijven
    compact = {
        "date": date_str,
        "marketId": snapshot.get("marketId"),
        "playerPoints": snapshot.get("playerPoints"),
    }
    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = []

    for key, cfg in COMPETITIONS.items():
        market_id = cfg["marketId"]
        name = cfg["name"]
        try:
            snapshot = fetch_competition(key, market_id, name)
        except Exception as exc:  # noqa: BLE001 - één competitie mag de rest niet meeslepen
            print(f"  [FOUT] {name} volledig mislukt: {exc}")
            snapshot = {
                "competition": key,
                "marketId": market_id,
                "name": name,
                "status": "error",
                "error": str(exc),
                "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        comp_dir = DATA_DIR / key
        comp_dir.mkdir(parents=True, exist_ok=True)
        with open(comp_dir / "latest.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        if snapshot.get("status") == "active":
            fetch_help_content_once(key, market_id)
            append_history(key, snapshot)

        summary.append({"competition": key, "status": snapshot.get("status")})

    with open(DATA_DIR / "index.json", "w", encoding="utf-8") as f:
        json.dump({
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "competitions": summary,
        }, f, ensure_ascii=False, indent=2)

    print("\nSamenvatting:")
    for s in summary:
        print(f"  {s['competition']}: {s['status']}")


if __name__ == "__main__":
    main()
