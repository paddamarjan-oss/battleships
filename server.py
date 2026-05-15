#!/usr/bin/env python3
"""
Battleships Online Server
Run with: python server.py
Then open http://localhost:8000 in your browser
"""

import asyncio
import json
import random
import string
import os
import aiohttp
from aiohttp import web

# ─── Game Constants ────────────────────────────────────────────────────────────
GRID_SIZE = 10
SHIPS = [
    {"name": "Carrier",    "size": 5},
    {"name": "Battleship", "size": 4},
    {"name": "Cruiser",    "size": 3},
    {"name": "Submarine",  "size": 3},
    {"name": "Destroyer",  "size": 2},
]

# ─── State ─────────────────────────────────────────────────────────────────────
rooms = {}      # room_code -> Room
clients = {}    # websocket -> {"room": code, "player": 0|1}

class Room:
    def __init__(self, code):
        self.code = code
        self.players = [None, None]   # websockets
        self.boards = [None, None]    # 10x10 grids of ship names or None
        self.shots  = [set(), set()]  # shots fired by each player (as "row,col")
        self.ready  = [False, False]  # placement confirmed
        self.turn   = 0               # whose turn it is (0 or 1)
        self.over   = False
        # ship cells per player: {ship_name: [{r, c}, ...]}
        self.ship_cells = [{}, {}]
        # ship orientations per player: {ship_name: 'H' or 'V'}
        self.ship_orientations = [{}, {}]

    def opponent(self, idx):
        return 1 - idx

    def all_ships_sunk(self, board, shots):
        """Return True if every ship cell on board has been shot."""
        for r, row in enumerate(board):
            for c, cell in enumerate(row):
                if cell is not None:
                    if f"{r},{c}" not in shots:
                        return False
        return True

    def serialize_board_for_owner(self, player_idx):
        """Full board (ships visible) for the owner."""
        board = self.boards[player_idx]
        shots = self.shots[self.opponent(player_idx)]  # shots the opponent fired at me
        result = []
        for r, row in enumerate(board):
            result_row = []
            for c, cell in enumerate(row):
                coord = f"{r},{c}"
                if coord in shots:
                    result_row.append("hit" if cell else "miss")
                else:
                    result_row.append(cell if cell else "empty")  # send ship name
            result.append(result_row)
        return result

    def serialize_enemy_board(self, player_idx):
        """Enemy board (ships hidden) for the attacker."""
        opp = self.opponent(player_idx)
        board = self.boards[opp]
        shots = self.shots[player_idx]  # shots I fired at opponent
        result = []
        for r, row in enumerate(board):
            result_row = []
            for c, cell in enumerate(row):
                coord = f"{r},{c}"
                if coord in shots:
                    result_row.append("hit" if cell else "miss")
                else:
                    result_row.append("unknown")
            result.append(result_row)
        return result

    def sunk_ships(self, attacked_player_idx, attacker_shots):
        """Return list of ship names that are fully sunk."""
        board = self.boards[attacked_player_idx]
        sunk = []
        for ship in SHIPS:
            name = ship["name"]
            # Collect all cells of this ship
            cells = []
            for r, row in enumerate(board):
                for c, cell in enumerate(row):
                    if cell == name:
                        cells.append(f"{r},{c}")
            if cells and all(coord in attacker_shots for coord in cells):
                sunk.append(name)
        return sunk

# ─── Helpers ───────────────────────────────────────────────────────────────────
def make_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code

def empty_board():
    return [[None]*GRID_SIZE for _ in range(GRID_SIZE)]

def validate_placement(ships_data):
    """
    ships_data: list of {name, size, cells: [{r,c}, ...]}
    Returns (bool, error_message)
    """
    board = empty_board()
    placed_names = set()

    expected = {s["name"]: s["size"] for s in SHIPS}

    for ship in ships_data:
        name = ship.get("name")
        cells = ship.get("cells", [])

        if name not in expected:
            return False, f"Unknown ship: {name}"
        if name in placed_names:
            return False, f"Duplicate ship: {name}"
        if len(cells) != expected[name]:
            return False, f"{name} needs {expected[name]} cells, got {len(cells)}"

        # Check contiguous & straight
        rows = [c["r"] for c in cells]
        cols = [c["c"] for c in cells]
        if not (len(set(rows)) == 1 or len(set(cols)) == 1):
            return False, f"{name} must be straight"

        rows_s, cols_s = sorted(rows), sorted(cols)
        if len(set(rows)) == 1:
            if cols_s != list(range(cols_s[0], cols_s[0]+len(cells))):
                return False, f"{name} cells must be contiguous"
        else:
            if rows_s != list(range(rows_s[0], rows_s[0]+len(cells))):
                return False, f"{name} cells must be contiguous"

        for cell in cells:
            r, c = cell["r"], cell["c"]
            if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
                return False, f"{name} out of bounds"
            if board[r][c] is not None:
                return False, f"Ships overlap at {r},{c}"
            board[r][c] = name

        placed_names.add(name)

    if len(placed_names) != len(SHIPS):
        return False, "Not all ships placed"

    return True, board

async def send(ws, msg):
    try:
        await ws.send(json.dumps(msg))
    except Exception:
        pass

async def broadcast_state(room: Room):
    """Send updated game state to both players."""
    for idx, ws in enumerate(room.players):
        if ws is None:
            continue
        opp = room.opponent(idx)
        sunk_by_me = room.sunk_ships(opp, room.shots[idx]) if room.boards[opp] else []
        sunk_by_opp = room.sunk_ships(idx, room.shots[opp]) if room.boards[idx] else []

        payload = {
            "type": "state",
            "my_board":    room.serialize_board_for_owner(idx) if room.boards[idx] else None,
            "enemy_board": room.serialize_enemy_board(idx) if room.boards[opp] else None,
            "my_turn":     room.turn == idx,
            "ready":       room.ready[:],
            "over":        room.over,
            "winner":      idx if room.over and room.turn == opp else (opp if room.over else None),
            "sunk_by_me":  sunk_by_me,
            "sunk_by_opp": sunk_by_opp,
        }
        await send(ws, payload)

# ─── Message Handler ────────────────────────────────────────────────────────────
async def handle(ws):
    clients[ws] = {"room": None, "player": None}
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # ── Create Room ──────────────────────────────────────────────────
            if mtype == "create":
                code = make_code()
                room = Room(code)
                room.players[0] = ws
                rooms[code] = room
                clients[ws] = {"room": code, "player": 0}
                await send(ws, {"type": "joined", "player": 0, "code": code})

            # ── Join Room ────────────────────────────────────────────────────
            elif mtype == "join":
                code = msg.get("code", "").upper().strip()
                if code not in rooms:
                    await send(ws, {"type": "error", "msg": "Room not found"})
                    continue
                room = rooms[code]
                if room.players[1] is not None:
                    await send(ws, {"type": "error", "msg": "Room is full"})
                    continue
                room.players[1] = ws
                clients[ws] = {"room": code, "player": 1}
                await send(ws, {"type": "joined", "player": 1, "code": code})
                # Notify player 0 that opponent joined
                if room.players[0]:
                    await send(room.players[0], {"type": "opponent_joined"})

            # ── Place Ships ──────────────────────────────────────────────────
            elif mtype == "place":
                info = clients[ws]
                if info["room"] is None:
                    continue
                room = rooms[info["room"]]
                pidx = info["player"]
                if room.ready[pidx]:
                    await send(ws, {"type": "error", "msg": "Already placed"})
                    continue

                ok, result = validate_placement(msg.get("ships", []))
                if not ok:
                    await send(ws, {"type": "error", "msg": result})
                    continue

                room.boards[pidx] = result
                room.ready[pidx] = True

                # Store ship cell positions and orientations for move logic
                for ship in msg.get("ships", []):
                    name = ship.get("name")
                    cells = ship.get("cells", [])
                    room.ship_cells[pidx][name] = cells
                    rows = [c["r"] for c in cells]
                    room.ship_orientations[pidx][name] = 'H' if len(set(rows)) == 1 else 'V'

                await send(ws, {"type": "placed"})

                if all(room.ready):
                    # Both placed — game starts, player 0 goes first
                    for i, p in enumerate(room.players):
                        if p:
                            await send(p, {"type": "start"})
                    await broadcast_state(room)
                else:
                    opp = room.opponent(pidx)
                    if room.players[opp]:
                        await send(room.players[opp], {"type": "opponent_placed"})

            # ── Fire Shot ────────────────────────────────────────────────────
            elif mtype == "fire":
                info = clients[ws]
                if info["room"] is None:
                    continue
                room = rooms[info["room"]]
                pidx = info["player"]

                if room.over:
                    continue
                if room.turn != pidx:
                    await send(ws, {"type": "error", "msg": "Not your turn"})
                    continue
                if not all(room.ready):
                    continue

                r, c = msg.get("r"), msg.get("c")
                if not (isinstance(r, int) and isinstance(c, int)):
                    continue
                if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
                    continue

                coord = f"{r},{c}"
                opp = room.opponent(pidx)

                if coord in room.shots[pidx]:
                    await send(ws, {"type": "error", "msg": "Already fired there"})
                    continue

                room.shots[pidx].add(coord)
                hit = room.boards[opp][r][c] is not None

                # Check win
                if room.all_ships_sunk(room.boards[opp], room.shots[pidx]):
                    room.over = True
                    # turn stays as pidx so we know who won
                else:
                    room.turn = opp  # switch turns

                await broadcast_state(room)

            # ── Move Ship ─────────────────────────────────────────────────────
            elif mtype == "move":
                info = clients[ws]
                if info["room"] is None:
                    continue
                room = rooms[info["room"]]
                pidx = info["player"]

                if room.over:
                    continue
                if room.turn != pidx:
                    await send(ws, {"type": "error", "msg": "Not your turn"})
                    continue
                if not all(room.ready):
                    continue

                ship_name = msg.get("ship")
                direction = msg.get("direction")  # 'forward' or 'backward'

                if ship_name not in room.ship_cells[pidx]:
                    await send(ws, {"type": "error", "msg": "Unknown ship"})
                    continue

                orientation = room.ship_orientations[pidx].get(ship_name, 'H')
                cells = room.ship_cells[pidx][ship_name]

                # Determine delta based on orientation and direction
                # 'forward' = increasing index (right for H, down for V)
                # 'backward' = decreasing index
                if orientation == 'H':
                    dr, dc = (0, 1) if direction == 'forward' else (0, -1)
                else:
                    dr, dc = (1, 0) if direction == 'forward' else (-1, 0)

                new_cells = [{"r": cell["r"] + dr, "c": cell["c"] + dc} for cell in cells]

                # Validate new positions
                board = room.boards[pidx]
                valid = True
                for cell in new_cells:
                    nr, nc = cell["r"], cell["c"]
                    if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
                        valid = False
                        break
                    # Check for collision with other ships (ignore this ship's own cells)
                    existing = board[nr][nc]
                    if existing is not None and existing != ship_name:
                        valid = False
                        break

                if not valid:
                    await send(ws, {"type": "error", "msg": "Can't move there"})
                    continue

                # Clear old cells
                for cell in cells:
                    board[cell["r"]][cell["c"]] = None

                # Place in new cells
                for cell in new_cells:
                    board[cell["r"]][cell["c"]] = ship_name

                room.ship_cells[pidx][ship_name] = new_cells

                # Switch turn
                opp = room.opponent(pidx)
                room.turn = opp
                await broadcast_state(room)
            elif mtype == "rematch":
                info = clients[ws]
                if info["room"] is None:
                    continue
                room = rooms[info["room"]]
                # Reset room
                room.boards = [None, None]
                room.shots  = [set(), set()]
                room.ready  = [False, False]
                room.turn   = 0
                room.over   = False
                room.ship_cells = [{}, {}]
                room.ship_orientations = [{}, {}]
                for p in room.players:
                    if p:
                        await send(p, {"type": "rematch"})

    except Exception:
        pass
    finally:
        info = clients.pop(ws, {})
        code = info.get("room")
        if code and code in rooms:
            room = rooms[code]
            pidx = info.get("player")
            if pidx is not None:
                room.players[pidx] = None
            opp_idx = 1 - (pidx or 0)
            opp_ws = room.players[opp_idx] if pidx is not None else None
            if opp_ws:
                await send(opp_ws, {"type": "opponent_left"})
            # Clean up empty rooms
            if all(p is None for p in room.players):
                rooms.pop(code, None)

# ─── Combined HTTP + WebSocket Server ─────────────────────────────────────────

async def http_handler(request):
    """Serve index.html for all non-WebSocket GET requests."""
    here = os.path.dirname(os.path.abspath(__file__))
    index = os.path.join(here, "index.html")
    return web.FileResponse(index)

async def websocket_handler(request):
    """Handle WebSocket upgrade and pass to game logic."""
    ws_response = web.WebSocketResponse()
    await ws_response.prepare(request)

    # Wrap aiohttp WS in a shim compatible with our handle() logic
    class WSShim:
        def __init__(self, ws):
            self._ws = ws
            self._closed = False

        async def send(self, data):
            if not self._closed:
                await self._ws.send_str(data)

        async def __aiter__(self):
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    yield msg.data
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        async def close(self):
            self._closed = True

    shim = WSShim(ws_response)
    await handle(shim)
    return ws_response

async def main():
    port = int(os.environ.get("PORT", 8000))

    app = web.Application()
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/{tail:.*}", http_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"┌─────────────────────────────────────────┐")
    print(f"│   🚢  Battleships Online Server          │")
    print(f"│                                         │")
    print(f"│   Open →  http://localhost:{port}         │")
    print(f"│                                         │")
    print(f"│   Share the URL with a friend!          │")
    print(f"└─────────────────────────────────────────┘")

    await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
