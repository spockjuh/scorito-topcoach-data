#!/usr/bin/env python3
"""
Scorito TopCoach data harvester.

Haalt uitsluitend PUBLIEKE, login-vrije endpoints op (geen sessie, geen cookies,
geen accountgegevens). Schrijft per competitie twee bestanden:
- data/{competitie}/latest.json  — status, punten, transfers, wedstrijden, fase
  (geen spelerslijst, om te voorkomen dat het bestand te groot wordt en bij het
  ophalen wordt afgekapt)
- data/{competitie}/players.json — spelerslijst, verrijkt met naam/club (niet
  alleen kale ID's)
Plus een append-only geschiedenisbestand met speler-punten voor trendanalyse.

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


def find_event_id(market_id: int):
    """
    Vindt het eventId (bv. 806 = Eredivisie 26/27) dat bij een marketId hoort —
    nodig om spelersnamen en clubnamen op te halen. Faalt stil (None) als het
    veldschema afwijkt; naam-verrijking wordt dan simpelweg overgeslagen, de rest
    van de run gaat gewoon door met alleen ID's.
    """
    payload = unwrap(get_json(
        f"https://platform.scorito.com/event/v1.0/eventlist/bymarket/{market_id}"))
    if not payload:
        return None
    entry = payload[0] if isinstance(payload, list) and payload else payload
    if isinstance(entry, dict):
        for key in ("Id", "EventId", "id"):
            if key in entry:
                return entry[key]
    return None


def build_name_lookups(event_id):
    """
    Geeft (player_bios, team_names) terug voor een event:
    - player_bios: {playerId: {"name": "Voornaam Achternaam", "nationality": ...}}
    - team_names:  {teamId: "Clubnaam"}
    Faalt stil (lege dicts) als het event niet gevonden wordt — de spelerslijst
    blijft dan werken met kale ID's, wat nog steeds bruikbaar is, alleen minder
    leesbaar.
    """
    player_bios, team_names = {}, {}
    if not event_id:
        return player_bios, team_names

    bios = unwrap(get_json(
        f"https://football.scorito.com/footballgeneric/v2.0/teamplayer/event/{event_id}"))
    if isinstance(bios, list):
        for p in bios:
            pid = p.get("PlayerId")
            if pid is not None:
                first = p.get("FirstName", "") or ""
                last = p.get("LastName", "") or ""
                player_bios[pid] = {
                    "name": f"{first} {last}".strip(),
                    "nationality": p.get("Nationality"),
                }

    teams = unwrap(get_json(
        f"https://football.scorito.com/footballGeneric/v2.0/teams/event/{event_id}"))
    if isinstance(teams, list):
        for t in teams:
            tid = t.get("Id")
            if tid is not None:
                team_names[tid] = t.get("Name") or t.get("NameShort")

    return player_bios, team_names


def enrich_players(players, player_bios, team_names):
    """Voegt naam/club/nationaliteit toe aan elke spelersregel, en laat overbodige
    tijdstempelvelden weg — dit is de belangrijkste stap om players.json compact
    en direct leesbaar te maken in plaats van kale ID's."""
    enriched = []
    for p in players or []:
        bio = player_bios.get(p.get("playerId"), {})
        enriched.append({
            "teamPlayerId": p.get("teamPlayerId"),
            "playerId": p.get("playerId"),
            "name": bio.get("name") or None,
            "nationality": bio.get("nationality"),
            "position": p.get("playerPosition"),
            "teamId": p.get("teamId"),
            "teamName": team_names.get(p.get("teamId")),
            "price": p.get("price"),
        })
    return enriched


def find_current_market_round(market_structure, game_phase_id):
    """
    Vindt de actuele marketRoundId uit marketstructure.

    Bevestigd echt schema (uitgelezen uit eigen HAR-capture, marketId 312):
        Content.MarketPhases[].MarketRounds[] met per ronde:
        Id, Order, Deadline, IsDeadlinePassed, IsStarted, Name, DefinitiveCalculationDate

    Rondes zitten dus GENEST onder MarketPhases, niet los op het top-niveau.
    We pakken de eerste ronde (op volgorde van Order) waarvan de deadline nog
    niet gepasseerd is; is die er niet (bv. seizoenseinde), dan de laatst
    bekende ronde als beste benadering.
    """
    if not isinstance(market_structure, dict):
        return None
    try:
        all_rounds = []
        for phase in market_structure.get("MarketPhases", []):
            all_rounds.extend(phase.get("MarketRounds", []))
        if not all_rounds:
            return None
        all_rounds.sort(key=lambda r: r.get("Order", 0))

        upcoming = [r for r in all_rounds if r.get("IsDeadlinePassed") is False]
        if upcoming:
            return upcoming[0].get("Id")
        return all_rounds[-1].get("Id")
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

    raw_players = unwrap(get_json(
        f"https://footballmanager-query.scorito.com/v1.0/teamplayerenriched/market/{market_id}"))

    if not raw_players:
        # marketstructure bestaat al (schema/deadlines gepland), maar de
        # spelersmarkt is nog niet gevuld — bv. TopCoach DE vóór lancering.
        result["status"] = "structure_only"
        result["playerCount"] = 0
    else:
        result["status"] = "active"
        event_id = find_event_id(market_id)
        player_bios, team_names = build_name_lookups(event_id)
        result["_enrichedPlayers"] = enrich_players(raw_players, player_bios, team_names)
        result["playerCount"] = len(result["_enrichedPlayers"])
        if not player_bios:
            print("  [waarschuwing] geen namen gevonden — players.json bevat dan alleen ID's")

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

        enriched_players = snapshot.pop("_enrichedPlayers", None)
        if enriched_players is not None:
            with open(comp_dir / "players.json", "w", encoding="utf-8") as f:
                json.dump({
                    "competition": key,
                    "marketId": market_id,
                    "fetchedAt": snapshot.get("fetchedAt"),
                    "playerCount": len(enriched_players),
                    "players": enriched_players,
                }, f, ensure_ascii=False, indent=2)

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
