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
import http.server
import threading
import os
import websockets
from websockets.server import serve

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

    def opponent(self, idx):
        return 1 - idx

    def all_ships_sunk(self, board, shots):
        """Return True if every ship cell on board has been shot."""
        for row in board:
            for cell in row:
                if cell is not None:
                    coord = f"{board.index(row)},{row.index(cell)}"
                    if coord not in shots:
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
                    result_row.append("ship" if cell else "empty")
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

            # ── Rematch ──────────────────────────────────────────────────────
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
                for p in room.players:
                    if p:
                        await send(p, {"type": "rematch"})

    except websockets.exceptions.ConnectionClosed:
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

# ─── HTTP Server (serves index.html) ───────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(__file__), **kwargs)

    def log_message(self, format, *args):
        pass  # silence logs

def run_http():
    server = http.server.HTTPServer(("", 8000), Handler)
    server.serve_forever()

# ─── Entry Point ────────────────────────────────────────────────────────────────
async def main():
    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()
    print("┌─────────────────────────────────────────┐")
    print("│   🚢  Battleships Online Server          │")
    print("│                                         │")
    print("│   Web UI  →  http://localhost:8000      │")
    print("│   WebSocket →  ws://localhost:8765      │")
    print("│                                         │")
    print("│   Share the URL with a friend!          │")
    print("└─────────────────────────────────────────┘")

    async with serve(handle, "0.0.0.0", 8765):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
