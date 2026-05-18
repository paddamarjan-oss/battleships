#!/usr/bin/env python3
"""
Battleships Online — 20x20, directional ships, special abilities
Run: python server.py  ->  open http://localhost:8000
"""
import asyncio, json, random, string, os
import aiohttp
from aiohttp import web

GRID_SIZE = 20

# Ship definitions with counts
SHIP_DEFS = [
    {"name": "Carrier",    "size": 7, "count": 1},
    {"name": "Battleship", "size": 5, "count": 2},
    {"name": "Cruiser",    "size": 4, "count": 2},
    {"name": "Submarine",  "size": 3, "count": 1},
    {"name": "Destroyer",  "size": 2, "count": 3},
]

# Expanded list of individual ships with unique IDs
SHIP_LIST = []
for _s in SHIP_DEFS:
    for _i in range(_s["count"]):
        _suffix = f" {_i+1}" if _s["count"] > 1 else ""
        SHIP_LIST.append({
            "id":   f"{_s['name']}{_suffix}".strip(),
            "name": _s["name"],
            "size": _s["size"],
        })

SHIP_ID_TO_SIZE = {s["id"]: s["size"] for s in SHIP_LIST}
SHIP_ID_TO_TYPE = {s["id"]: s["name"] for s in SHIP_LIST}

ABILITIES = {
    "Carrier":    "recon",
    "Battleship": "barrage",
    "Cruiser":    "smoke",
    "Submarine":  "dive",
    "Destroyer":  "ram",
}

rooms   = {}
clients = {}

# ─── Room ──────────────────────────────────────────────────────────────────────
class Room:
    def __init__(self, code):
        self.code    = code
        self.players = [None, None]
        self.boards  = [None, None]       # 20x20 grid, cell = ship_id or None
        self.shots   = [set(), set()]     # coords fired by each player
        self.ready   = [False, False]
        self.turn    = 0
        self.over    = False
        # ship tracking
        self.ship_cells   = [{}, {}]      # ship_id -> [{r,c},...] front->rear
        self.ship_orient  = [{}, {}]      # ship_id -> 'H'|'V'
        self.ship_fwd_inc = [{}, {}]      # ship_id -> bool (True = right/down)
        # ability state
        self.ability_used  = [{}, {}]     # ship_id -> True
        self.smoke_active  = [False, False]
        self.dive_active   = [False, False]
        self.barrage_shots = [0, 0]       # shots remaining in active barrage

    def opp(self, i): return 1 - i

    def all_sunk(self, board, shots):
        for r, row in enumerate(board):
            for c, cell in enumerate(row):
                if cell and f"{r},{c}" not in shots:
                    return False
        return True

    def ship_alive(self, pidx, ship_id):
        cells = self.ship_cells[pidx].get(ship_id, [])
        hits  = self.shots[self.opp(pidx)]
        return any(f"{c['r']},{c['c']}" not in hits for c in cells)

    def board_for_owner(self, pidx):
        board     = self.boards[pidx]
        opp_shots = self.shots[self.opp(pidx)]
        grid = []
        for r, row in enumerate(board):
            rr = []
            for c, cell in enumerate(row):
                coord = f"{r},{c}"
                if coord in opp_shots:
                    rr.append("hit" if cell else "miss")
                else:
                    rr.append(cell if cell else "empty")
            grid.append(rr)
        # front cell map
        fronts = {}
        for sid, cells in self.ship_cells[pidx].items():
            if cells:
                fronts[f"{cells[0]['r']},{cells[0]['c']}"] = sid
        # available abilities
        avail = {}
        for sid in list(self.ship_cells[pidx].keys()):
            if sid not in self.ability_used[pidx] and self.ship_alive(pidx, sid):
                ship_type = SHIP_ID_TO_TYPE.get(sid)
                if ship_type and ship_type in ABILITIES:
                    avail[sid] = ABILITIES[ship_type]
        return {
            "grid":    grid,
            "fronts":  fronts,
            "orients": self.ship_orient[pidx],
            "fwd_inc": self.ship_fwd_inc[pidx],
            "abilities": avail,
            "smoke_active": self.smoke_active[pidx],
            "dive_active":  self.dive_active[pidx],
        }

    def board_for_enemy(self, pidx):
        opp      = self.opp(pidx)
        board    = self.boards[opp]
        my_shots = self.shots[pidx]
        result = []
        for r, row in enumerate(board):
            rr = []
            for c, cell in enumerate(row):
                coord = f"{r},{c}"
                if coord in my_shots:
                    rr.append("hit" if cell else "miss")
                else:
                    rr.append("unknown")
            result.append(rr)
        return result

    def sunk_ships(self, attacked, attacker_shots):
        board = self.boards[attacked]
        sunk, seen = [], set()
        for r, row in enumerate(board):
            for c, cell in enumerate(row):
                if cell and cell not in seen:
                    seen.add(cell)
                    cells = [f"{rr},{cc}" for rr, row2 in enumerate(board)
                             for cc, ce in enumerate(row2) if ce == cell]
                    if all(co in attacker_shots for co in cells):
                        sunk.append(cell)
        return sunk

# ─── Helpers ───────────────────────────────────────────────────────────────────
def make_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms: return code

def empty_board():
    return [[None] * GRID_SIZE for _ in range(GRID_SIZE)]

def validate_placement(ships_data):
    """Returns (ok, board_or_errstr, ship_cells, ship_orient, ship_fwd_inc)"""
    board  = empty_board()
    placed = set()
    sc, so, sf = {}, {}, {}

    for ship in ships_data:
        sid    = ship.get("id")
        cells  = ship.get("cells", [])
        facing = ship.get("facing", "right")

        if sid not in SHIP_ID_TO_SIZE:
            return False, f"Unknown ship id: {sid}", {}, {}, {}
        if sid in placed:
            return False, f"Duplicate ship: {sid}", {}, {}, {}
        expected_size = SHIP_ID_TO_SIZE[sid]
        if len(cells) != expected_size:
            return False, f"{sid}: expected {expected_size} cells, got {len(cells)}", {}, {}, {}

        rows = [c["r"] for c in cells]
        cols = [c["c"] for c in cells]
        horiz = len(set(rows)) == 1
        vert  = len(set(cols)) == 1
        if not (horiz or vert):
            return False, f"{sid} is not straight", {}, {}, {}

        rs, cs2 = sorted(rows), sorted(cols)
        if horiz:
            if cs2 != list(range(cs2[0], cs2[0] + len(cells))):
                return False, f"{sid} cells not contiguous", {}, {}, {}
            orient  = "H"
            fwd_inc = (facing == "right")
        else:
            if rs != list(range(rs[0], rs[0] + len(cells))):
                return False, f"{sid} cells not contiguous", {}, {}, {}
            orient  = "V"
            fwd_inc = (facing == "down")

        for cell in cells:
            r, c = cell["r"], cell["c"]
            if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
                return False, f"{sid} out of bounds at {r},{c}", {}, {}, {}
            if board[r][c]:
                return False, f"Ships overlap at {r},{c}", {}, {}, {}
            board[r][c] = sid

        placed.add(sid)
        sc[sid] = cells
        so[sid] = orient
        sf[sid] = fwd_inc

    if len(placed) != len(SHIP_LIST):
        missing = [s["id"] for s in SHIP_LIST if s["id"] not in placed]
        return False, f"Missing ships: {missing}", {}, {}, {}

    return True, board, sc, so, sf

def apply_move(room, pidx, ship_id, steps=1):
    """Move ship forward `steps` squares. Returns (ok, error_str)."""
    orient  = room.ship_orient[pidx][ship_id]
    fwd_inc = room.ship_fwd_inc[pidx][ship_id]
    cells   = room.ship_cells[pidx][ship_id]
    board   = room.boards[pidx]
    hits    = room.shots[room.opp(pidx)]

    if orient == "H": dr, dc = 0, (1 if fwd_inc else -1)
    else:             dr, dc = (1 if fwd_inc else -1), 0

    # Sever cells behind rearmost hit
    hit_idx   = [i for i, c in enumerate(cells) if f"{c['r']},{c['c']}" in hits]
    surviving = list(cells)
    if hit_idx:
        rearmost  = max(hit_idx)
        severed   = surviving[rearmost + 1:]
        surviving = surviving[:rearmost + 1]
        for c in severed:
            board[c["r"]][c["c"]] = None

    movable = [c for c in surviving if f"{c['r']},{c['c']}" not in hits]
    if not movable:
        return False, "No sections left to move"

    # Validate all steps
    for step in range(1, steps + 1):
        lead = movable[0]
        nr, nc = lead["r"] + dr * step, lead["c"] + dc * step
        if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
            return False, "Blocked by edge"
        ex = board[nr][nc]
        if ex and ex != ship_id:
            return False, "Blocked by another ship"

    # Apply movement
    for _ in range(steps):
        for c in movable: board[c["r"]][c["c"]] = None
        new_movable = []
        for c in movable:
            nr, nc = c["r"] + dr, c["c"] + dc
            board[nr][nc] = ship_id
            new_movable.append({"r": nr, "c": nc})
        movable = new_movable

    # Rebuild ordered cell list
    new_surviving = []
    mi = iter(movable)
    for c in surviving:
        if f"{c['r']},{c['c']}" in hits:
            new_surviving.append(c)
        else:
            new_surviving.append(next(mi))
    room.ship_cells[pidx][ship_id] = new_surviving

    still_alive = [c for c in new_surviving if f"{c['r']},{c['c']}" not in hits]
    if not still_alive:
        del room.ship_cells[pidx][ship_id]

    return True, None

# ─── Network ───────────────────────────────────────────────────────────────────
async def ws_send(ws, msg):
    try: await ws.send_str(json.dumps(msg))
    except: pass

async def broadcast(room):
    for idx, ws in enumerate(room.players):
        if ws is None: continue
        opp = room.opp(idx)
        payload = {
            "type":         "state",
            "my_board":     room.board_for_owner(idx) if room.boards[idx] else None,
            "enemy_board":  room.board_for_enemy(idx) if room.boards[opp] else None,
            "my_turn":      room.turn == idx,
            "ready":        room.ready[:],
            "over":         room.over,
            "winner":       idx if room.over and room.turn == opp else (opp if room.over else None),
            "sunk_by_me":   room.sunk_ships(opp, room.shots[idx]) if room.boards[opp] else [],
            "sunk_by_opp":  room.sunk_ships(idx, room.shots[opp]) if room.boards[idx] else [],
            "barrage_left": room.barrage_shots[idx],
            "in_barrage":   room.barrage_shots[idx] > 0,
        }
        await ws_send(ws, payload)

# ─── WebSocket handler ─────────────────────────────────────────────────────────
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients[ws] = {"room": None, "player": None}
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try: data = json.loads(msg.data)
                except: continue
                await handle(ws, data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    finally:
        info = clients.pop(ws, {})
        code = info.get("room")
        if code and code in rooms:
            room = rooms[code]
            pidx = info.get("player")
            if pidx is not None: room.players[pidx] = None
            opp_ws = room.players[room.opp(pidx or 0)] if pidx is not None else None
            if opp_ws: await ws_send(opp_ws, {"type": "opponent_left"})
            if all(p is None for p in room.players): rooms.pop(code, None)
    return ws

async def handle(ws, msg):
    mtype = msg.get("type")

    # ── Create ────────────────────────────────────────────────────────────────
    if mtype == "create":
        code = make_code()
        room = Room(code)
        room.players[0] = ws
        rooms[code] = room
        clients[ws] = {"room": code, "player": 0}
        await ws_send(ws, {"type": "joined", "player": 0, "code": code})

    # ── Join ──────────────────────────────────────────────────────────────────
    elif mtype == "join":
        code = msg.get("code", "").upper().strip()
        if code not in rooms:
            await ws_send(ws, {"type": "error", "msg": "Room not found"}); return
        room = rooms[code]
        if room.players[1] is not None:
            await ws_send(ws, {"type": "error", "msg": "Room is full"}); return
        room.players[1] = ws
        clients[ws] = {"room": code, "player": 1}
        await ws_send(ws, {"type": "joined", "player": 1, "code": code})
        if room.players[0]: await ws_send(room.players[0], {"type": "opponent_joined"})

    # ── Place ─────────────────────────────────────────────────────────────────
    elif mtype == "place":
        info = clients[ws]
        if not info["room"]: return
        room = rooms[info["room"]]; pidx = info["player"]
        if room.ready[pidx]:
            await ws_send(ws, {"type": "error", "msg": "Already placed"}); return
        ok, result, sc, so, sf = validate_placement(msg.get("ships", []))
        if not ok:
            await ws_send(ws, {"type": "error", "msg": result}); return
        room.boards[pidx]      = result
        room.ship_cells[pidx]  = sc
        room.ship_orient[pidx] = so
        room.ship_fwd_inc[pidx]= sf
        room.ready[pidx] = True
        await ws_send(ws, {"type": "placed"})
        if all(room.ready):
            for p in room.players:
                if p: await ws_send(p, {"type": "start"})
            await broadcast(room)
        else:
            opp = room.opp(pidx)
            if room.players[opp]: await ws_send(room.players[opp], {"type": "opponent_placed"})

    # ── Fire ──────────────────────────────────────────────────────────────────
    elif mtype == "fire":
        info = clients[ws]
        if not info["room"]: return
        room = rooms[info["room"]]; pidx = info["player"]
        opp  = room.opp(pidx)
        if room.over or room.turn != pidx or not all(room.ready): return
        r, c = msg.get("r"), msg.get("c")
        if not (isinstance(r, int) and isinstance(c, int)): return
        if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE): return
        coord = f"{r},{c}"
        if coord in room.shots[pidx]:
            await ws_send(ws, {"type": "error", "msg": "Already fired there"}); return

        in_barrage = room.barrage_shots[pidx] > 0

        # Dive check — if this shot hits the sub's location and dive is active
        if room.dive_active[opp]:
            hit_cell = room.boards[opp][r][c]
            if hit_cell and SHIP_ID_TO_TYPE.get(hit_cell) == "Submarine":
                # Absorbed — counts as miss, dive expires
                room.shots[pidx].add(coord)
                room.boards[opp][r][c] = None   # hide sub cell (appears as miss)
                room.dive_active[opp] = False
                if in_barrage:
                    room.barrage_shots[pidx] -= 1
                    if room.barrage_shots[pidx] <= 0: room.turn = opp
                else:
                    room.turn = opp
                await ws_send(ws, {"type": "ability_event", "event": "dive_blocked", "r": r, "c": c})
                await broadcast(room); return

        # Smoke check
        if room.smoke_active[opp]:
            room.smoke_active[opp] = False
            room.shots[pidx].add(coord)
            if in_barrage:
                room.barrage_shots[pidx] -= 1
                if room.barrage_shots[pidx] <= 0: room.turn = opp
            else:
                room.turn = opp
            await ws_send(ws, {"type": "ability_event", "event": "smoke_blocked", "r": r, "c": c})
            await broadcast(room); return

        # Normal shot
        room.shots[pidx].add(coord)
        if room.all_sunk(room.boards[opp], room.shots[pidx]):
            room.over = True
        else:
            if in_barrage:
                room.barrage_shots[pidx] -= 1
                if room.barrage_shots[pidx] <= 0: room.turn = opp
            else:
                room.turn = opp
        await broadcast(room)

    # ── Move ──────────────────────────────────────────────────────────────────
    elif mtype == "move":
        info = clients[ws]
        if not info["room"]: return
        room = rooms[info["room"]]; pidx = info["player"]
        if room.over or room.turn != pidx or not all(room.ready): return
        ship_id = msg.get("ship")
        if ship_id not in room.ship_cells[pidx]:
            await ws_send(ws, {"type": "error", "msg": "Unknown ship"}); return
        ok, err = apply_move(room, pidx, ship_id, 1)
        if not ok:
            await ws_send(ws, {"type": "error", "msg": err}); return
        room.turn = room.opp(pidx)
        await broadcast(room)

    # ── Ability ───────────────────────────────────────────────────────────────
    elif mtype == "ability":
        info = clients[ws]
        if not info["room"]: return
        room = rooms[info["room"]]; pidx = info["player"]
        opp  = room.opp(pidx)
        if room.over or room.turn != pidx or not all(room.ready): return
        ship_id = msg.get("ship")
        if not ship_id or ship_id not in SHIP_ID_TO_TYPE:
            await ws_send(ws, {"type": "error", "msg": "Unknown ship"}); return
        if ship_id in room.ability_used[pidx]:
            await ws_send(ws, {"type": "error", "msg": "Ability already used"}); return
        if not room.ship_alive(pidx, ship_id):
            await ws_send(ws, {"type": "error", "msg": "Ship is destroyed"}); return

        ship_type = SHIP_ID_TO_TYPE[ship_id]
        ability   = ABILITIES.get(ship_type)
        room.ability_used[pidx][ship_id] = True

        if ability == "recon":
            tr, tc = msg.get("r", 0), msg.get("c", 0)
            revealed = {}
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    nr, nc = tr + dr, tc + dc
                    if 0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE:
                        val = room.boards[opp][nr][nc]
                        revealed[f"{nr},{nc}"] = "ship" if val else "empty"
            await ws_send(ws, {"type": "recon_result", "cells": revealed})
            # Recon does NOT end turn — broadcast updated abilities list
            await broadcast(room)

        elif ability == "barrage":
            room.barrage_shots[pidx] = 3
            await ws_send(ws, {"type": "ability_event", "event": "barrage_start"})
            await broadcast(room)

        elif ability == "smoke":
            room.smoke_active[pidx] = True
            room.turn = opp
            await ws_send(ws, {"type": "ability_event", "event": "smoke_up"})
            await broadcast(room)

        elif ability == "dive":
            room.dive_active[pidx] = True
            room.turn = opp
            await ws_send(ws, {"type": "ability_event", "event": "dive_start"})
            await broadcast(room)

        elif ability == "ram":
            ok, err = apply_move(room, pidx, ship_id, 2)
            if not ok:
                room.ability_used[pidx].pop(ship_id, None)  # refund
                await ws_send(ws, {"type": "error", "msg": err}); return
            room.turn = opp
            await broadcast(room)

    # ── Rematch ───────────────────────────────────────────────────────────────
    elif mtype == "rematch":
        info = clients[ws]
        if not info["room"]: return
        room = rooms[info["room"]]
        room.boards        = [None, None]
        room.shots         = [set(), set()]
        room.ready         = [False, False]
        room.turn          = 0
        room.over          = False
        room.ship_cells    = [{}, {}]
        room.ship_orient   = [{}, {}]
        room.ship_fwd_inc  = [{}, {}]
        room.ability_used  = [{}, {}]
        room.smoke_active  = [False, False]
        room.dive_active   = [False, False]
        room.barrage_shots = [0, 0]
        for p in room.players:
            if p: await ws_send(p, {"type": "rematch"})

# ─── HTTP ──────────────────────────────────────────────────────────────────────
async def http_handler(request):
    here = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(here, "index.html"))

async def main():
    port = int(os.environ.get("PORT", 8000))
    app  = web.Application()
    app.router.add_get("/ws",        websocket_handler)
    app.router.add_get("/{tail:.*}", http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Battleships running on http://localhost:{port}")
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
