from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import random
import time
import os
import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import uuid4
from typing import List

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(maintain_rooms())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

# 目录配置：预设（只读） vs 自定义（可写）
PRESET_DIR = "data/presets"
CUSTOM_DIR = os.environ.get("WHOAMI_CUSTOM_DIR", "data/custom")
os.makedirs(PRESET_DIR, exist_ok=True)
os.makedirs(CUSTOM_DIR, exist_ok=True)

class LibrarySchema(BaseModel):
    name: str
    characters: List[str]

# --- 辅助函数：检查是否属于官方预设库 ---
def is_preset(name: str) -> bool:
    return os.path.exists(os.path.join(PRESET_DIR, f"{name}.json"))

@app.get("/api/libraries")
async def get_libraries():
    """获取所有可用角色库列表（合并预设与自定义目录）"""
    libs = set()
    # 读取官方预设
    for f in os.listdir(PRESET_DIR):
        if f.endswith(".json"): libs.add(f[:-5])
    # 读取用户自定义
    for f in os.listdir(CUSTOM_DIR):
        if f.endswith(".json"): libs.add(f[:-5])
    return {"libraries": list(libs)}

@app.get("/api/libraries/{name}")
async def get_library(name: str):
    """获取指定角色库详情（优先读取预设库）"""
    safe_name = os.path.basename(name)
    
    # 1. 先查预设库
    preset_path = os.path.join(PRESET_DIR, f"{safe_name}.json")
    if os.path.exists(preset_path):
        with open(preset_path, "r", encoding="utf-8") as f:
            return {"name": safe_name, "characters": json.load(f), "protected": True}
    
    # 2. 后查自定义库
    custom_path = os.path.join(CUSTOM_DIR, f"{safe_name}.json")
    if os.path.exists(custom_path):
        with open(custom_path, "r", encoding="utf-8") as f:
            return {"name": safe_name, "characters": json.load(f), "protected": False}
            
    raise HTTPException(status_code=404, detail="角色库不存在")

@app.post("/api/libraries")
async def save_library(lib: LibrarySchema):
    """新建或保存自定义角色库（严禁覆盖预设库）"""
    safe_name = os.path.basename(lib.name)
    
    # 1. 拦截对官方预设库的修改企图
    if is_preset(safe_name):
        raise HTTPException(status_code=403, detail="官方预设角色库不可修改，如需调整请另存为新名称")
    
    if not lib.characters:
        raise HTTPException(status_code=400, detail="角色库内容不能为空")
    
    # 2. 统一写入自定义目录
    path = os.path.join(CUSTOM_DIR, f"{safe_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([c.strip() for c in lib.characters if c.strip()], f, ensure_ascii=False)
        
    return {"status": "success"}

@app.get("/")
async def get_index():
    return FileResponse("static/index.html")

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "release": os.environ.get("WHOAMI_RELEASE", "development")}

# --- 游戏与房间管理逻辑 ---
rooms = {}
connections = {}
ROOM_TIMEOUT = 600
RECONNECT_GRACE_SECONDS = 60
GUESS_TIMEOUT_SECONDS = 30
DEFAULT_MAX_PLAYERS = 8

@app.get("/api/rooms")
async def get_rooms():
    """Expose only public lobby information, never characters or round state."""
    cleanup_inactive_rooms()
    now = time.time()
    summaries = []
    for room_id, room in rooms.items():
        eligible_ids = eligible_player_ids(room_id, now)
        if not eligible_ids:
            continue
        online_ids = connected_player_ids(room_id)
        host = next((p for p in room["players"] if p["id"] == room.get("host_id")), None)
        summaries.append({
            "id": room_id,
            "name": room.get("name", f"房间 {room_id}"),
            "host_name": host["name"] if host else "等待房主",
            "library_name": room["library_name"],
            "player_count": len(eligible_ids),
            "online_count": len(online_ids),
            "max_players": room.get("max_players", DEFAULT_MAX_PLAYERS),
            "started": room["started"],
            "joinable": not room["started"] and len(eligible_ids) < room.get("max_players", DEFAULT_MAX_PLAYERS),
        })
    summaries.sort(key=lambda room: (not room["joinable"], room["started"], room["id"]))
    return {"rooms": summaries}

def update_host(room_id: str, now=None):
    room = rooms[room_id]
    eligible_ids = eligible_player_ids(room_id, now)
    if room.get("host_id") not in eligible_ids:
        online_ids = connected_player_ids(room_id)
        candidates = [p for p in room["players"] if p["id"] in online_ids or p["id"] in eligible_ids]
        candidates.sort(key=lambda p: p["id"] not in online_ids)
        room["host_id"] = candidates[0]["id"] if candidates else None

def eligible_player_ids(room_id: str, now=None):
    """Keep temporarily disconnected players in the current round."""
    now = time.time() if now is None else now
    online_ids = connected_player_ids(room_id)
    return {
        p["id"] for p in rooms[room_id]["players"]
        if not p.get("left") and (
            p["id"] in online_ids
            or now - p.get("disconnected_at", 0) < RECONNECT_GRACE_SECONDS
        )
    }

def connected_player_ids(room_id: str):
    """Return the player IDs that currently have an active WebSocket."""
    return {conn["id"] for conn in connections.get(room_id, [])}

def cleanup_inactive_rooms():
    now = time.time()
    expired_rooms = [
        r_id for r_id, room in rooms.items() 
        if not connections.get(r_id)
        and now - room.get("last_activity", now) > ROOM_TIMEOUT
    ]
    for r_id in expired_rooms:
        if r_id in rooms: del rooms[r_id]
        if r_id in connections: del connections[r_id]

def touch_room(room_id: str):
    if room_id in rooms:
        rooms[room_id]["last_activity"] = time.time()

def advance_turn(room, connected_ids=None):
    players = room["players"]
    if not players: return
    
    if all(p.get("finished", False) or p.get("left", False) for p in players):
        room["started"] = False
        room["turn_index"] = 0
        room["turn_id"] = uuid4().hex
        room["current_question"] = None
        room["question_history"] = []
        for p in players:
            p["ready"] = False
            p["finished"] = False
            p["last_guess_wrong"] = False
            p["target"] = ""
        room["events"].append({"content": "🏆 游戏结束！所有仍在参与的玩家都已成功猜出身份！房间已重置。"})
        return True
    
    n = len(players)
    for i in range(1, n + 1):
        next_idx = (room["turn_index"] + i) % n
        if (
            not players[next_idx].get("finished", False)
            and (connected_ids is None or players[next_idx]["id"] in connected_ids)
        ):
            room["turn_index"] = next_idx
            room["turn_id"] = uuid4().hex
            return True
    return False

def move_question_to_guessing_if_ready(room_id: str, now=None):
    """Only exclude disconnected responders after their reconnect grace expires."""
    now = time.time() if now is None else now
    room = rooms.get(room_id)
    if not room:
        return

    question = room.get("current_question")
    if not question or question["stage"] != "answering":
        return

    eligible_ids = eligible_player_ids(room_id, now)
    responders = [
        player["id"]
        for player in room["players"]
        if player["id"] != question["asker_id"]
        and player["id"] in eligible_ids
    ]
    if all(player_id in question["answers"] for player_id in responders):
        question["stage"] = "guessing"
        question["guess_deadline"] = now + GUESS_TIMEOUT_SECONDS
        room["events"].append({"content": f"📢 【{question['asker_name']}】的提问已收到所有可参与玩家的回答，请在 {GUESS_TIMEOUT_SECONDS} 秒内猜测或跳过！"})
        return True
    return False

def finish_question(room_id: str, guess="", *, timed_out=False, disconnected=False, now=None):
    room = rooms[room_id]
    q = room["current_question"]
    player = room["players"][room["turn_index"]]
    player["last_guess_wrong"] = False
    if disconnected:
        q["outcome"] = "disconnected"
        message = (f"系统: 【{player['name']}】已离开房间，结束其当前提问。" if player.get("left")
                   else f"系统: 【{player['name']}】超过 {RECONNECT_GRACE_SECONDS} 秒未重连，已结束其当前提问。")
    elif timed_out:
        q["outcome"] = "timeout"
        message = f"⏭️ 【{player['name']}】{GUESS_TIMEOUT_SECONDS} 秒内未猜测，已自动跳过。"
    elif guess:
        if guess == player["target"]:
            q["outcome"] = "correct"
            player["finished"] = True
            message = f"🎉 恭喜 【{player['name']}】 成功猜出自己是【{player['target']}】！通关成功！ 🎉"
        else:
            q["outcome"] = "wrong"
            player["last_guess_wrong"] = True
            message = f"❌ 【{player['name']}】 猜测【{guess}】错误！"
    else:
        q["outcome"] = "skipped"
        message = f"⏭️ 【{player['name']}】 放弃了本轮猜测。"
    room["events"].append({"content": message})
    room["question_history"].append(q)
    room["current_question"] = None
    advance_turn(room, eligible_player_ids(room_id, now))
    touch_room(room_id)

def reconcile_room(room_id: str, now=None):
    """Apply deadlines once, whether triggered by the clock or a new message."""
    room = rooms[room_id]
    now = time.time() if now is None else now
    if not room["started"]:
        eligible_ids = eligible_player_ids(room_id, now)
        players = [p for p in room["players"] if p["id"] in eligible_ids]
        changed = len(players) != len(room["players"])
        room["players"] = players
        previous_host = room.get("host_id")
        update_host(room_id, now)
        changed = changed or previous_host != room.get("host_id")
        return changed

    changed = False
    online_ids = connected_player_ids(room_id)
    for player in room["players"]:
        if (player["id"] not in online_ids and not player.get("disconnect_expired")
                and now - player.get("disconnected_at", 0) >= RECONNECT_GRACE_SECONDS):
            player["disconnect_expired"] = True
            changed = True
    previous_host = room.get("host_id")
    update_host(room_id, now)
    changed = changed or previous_host != room.get("host_id")
    q = room["current_question"]
    if q and q["stage"] == "guessing" and now >= q["guess_deadline"]:
        finish_question(room_id, timed_out=True, now=now)
        changed = True

    current_player = room["players"][room["turn_index"]]
    eligible_ids = eligible_player_ids(room_id, now)
    if current_player["id"] not in eligible_ids:
        if room["current_question"]:
            finish_question(room_id, disconnected=True, now=now)
            changed = True
        elif advance_turn(room, eligible_ids):
            room["events"].append({"content": f"系统: 【{current_player['name']}】离线，已跳到下一位可参与玩家。"})
            changed = True
    return move_question_to_guessing_if_ready(room_id, now) or changed

async def maintain_rooms():
    while True:
        await asyncio.sleep(0.5)
        changed_rooms = [room_id for room_id in list(rooms) if reconcile_room(room_id)]
        await asyncio.gather(*(broadcast(room_id) for room_id in changed_rooms))
        cleanup_inactive_rooms()

async def broadcast(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    async with room["broadcast_lock"]:
        if rooms.get(room_id) is room:
            await send_room_state(room_id, room)

async def send_room_state(room_id, room):
    # Build every recipient's snapshot before the first await; broadcasts are ordered.
    room["revision"] += 1
    online_ids = connected_player_ids(room_id)
    now = time.time()
    messages = []
    for conn in list(connections.get(room_id, [])):
        conn_id = conn["id"]
        payload = {
            "type": "ROOM_STATE",
            "players": [
                {
                    "id": p["id"], 
                    "name": p["name"], 
                    "ready": p["ready"],
                    "finished": p.get("finished", False),
                    "last_guess_wrong": p.get("last_guess_wrong", False),
                    "connected": p["id"] in online_ids,
                    "reconnecting": p["id"] not in online_ids and now - p.get("disconnected_at", 0) < RECONNECT_GRACE_SECONDS,
                    "targetPerson": (p["target"] if not room["started"] or p["id"] != conn_id or p.get("finished", False) else "???"), 
                    "isMe": p["id"] == conn_id
                } 
                for p in room["players"]
            ],
            "started": room["started"],
            "turn_index": room.get("turn_index", 0),
            "turn_id": room["turn_id"],
            "room_instance_id": room["instance_id"],
            "revision": room["revision"],
            "server_time": now,
            "current_question": room.get("current_question"),
            "question_history": room.get("question_history", []),
            "events": room["events"],
            "chat_messages": room.get("chat_messages", []),
            "library_name": room.get("library_name", "默认"),
            "room_name": room.get("name", f"房间 {room_id}"),
            "host_id": room.get("host_id"),
            "max_players": room.get("max_players", DEFAULT_MAX_PLAYERS)
        }
        messages.append((conn["ws"], json.dumps(payload, ensure_ascii=False)))
    await asyncio.gather(*(send_snapshot(ws, message) for ws, message in messages))

async def send_snapshot(websocket, message):
    try:
        await asyncio.wait_for(websocket.send_text(message), timeout=5)
    except (OSError, RuntimeError, WebSocketDisconnect, asyncio.TimeoutError):
        pass

# Lobby clients explicitly create or join; auto preserves older clients.
@app.websocket("/ws/{room_id}/{name}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, name: str, library: str = "默认历史人物",
                             mode: str = "auto", title: str = "", capacity: int = DEFAULT_MAX_PLAYERS):
    await websocket.accept()
    cleanup_inactive_rooms()

    async def reject(message):
        await websocket.send_text(json.dumps({"type": "ERROR", "message": message}, ensure_ascii=False))
        await websocket.close(code=4000)

    name = name.strip()
    if not name or len(name) > 20 or not room_id.strip() or len(room_id) > 40:
        await reject("昵称需要 1–20 个字，房间号需要 1–40 个字。")
        return
    if mode not in {"auto", "create", "join"}:
        await reject("无效的房间操作，请返回大厅重试。")
        return
    if mode == "create" and room_id in rooms:
        await reject("房间号已被占用，请重新创建房间。")
        return
    if mode == "join" and room_id not in rooms:
        await reject("这个房间已解散，请选择其他房间。")
        return
    
    if room_id not in rooms:
        if not 2 <= capacity <= 12 or len(title.strip()) > 30:
            await reject("房间人数应为 2–12 人，房间名称最多 30 个字。")
            return
        # 首个进入房间的玩家决定该房间的角色库
        safe_name = os.path.basename(library)
        
        # 1. 先在自定义目录 (CUSTOM_DIR) 中查找
        lib_path = os.path.join(CUSTOM_DIR, f"{safe_name}.json")
        
        # 2. 如果自定义目录没有，再到预设目录 (PRESET_DIR) 中查找
        if not os.path.exists(lib_path):
            lib_path = os.path.join(PRESET_DIR, f"{safe_name}.json")
            
        # 3. 如果两边都找不到，明确告知玩家，不默默降级
        if not os.path.exists(lib_path):
            await reject(f"角色库 '{safe_name}' 不存在，请重新选择。")
            return

        # 读取角色库内容
        with open(lib_path, "r", encoding="utf-8") as f:
            characters = json.load(f)
        if not characters:
            await reject("角色库为空，请选择其他角色库。")
            return

        rooms[room_id] = {
            "name": title.strip() or f"{name}的房间",
            "max_players": capacity,
            "host_id": None,
            "started": False, 
            "players": [], 
            "turn_index": 0, 
            "turn_id": uuid4().hex,
            "instance_id": uuid4().hex,
            "revision": 0,
            "broadcast_lock": asyncio.Lock(),
            "current_question": None, 
            "question_history": [], 
            "events": [],
            "chat_messages": [],
            "last_activity": time.time(),
            "library_name": safe_name,
            "characters": characters
        }
        connections[room_id] = []
    
    room = rooms[room_id]
    reconcile_room(room_id)
    touch_room(room_id)
    
    existing_player = next((p for p in room["players"] if p["name"] == name), None)
    if room["started"] and (not existing_player or existing_player.get("left")):
        await reject("该房间正在游戏中，请等待本局结束或选择其他房间。")
        return
    if not existing_player and len(room["players"]) >= room.get("max_players", DEFAULT_MAX_PLAYERS):
        await reject("房间已满，请选择其他房间或自己创建。")
        return

    replaced_connections = []
    if existing_player:
        pid = existing_player["id"]
        replaced_connections = [c for c in connections[room_id] if c["id"] == pid]
        connections[room_id] = [c for c in connections[room_id] if c["id"] != pid]
        connections[room_id].append({"ws": websocket, "id": pid, "name": name})
        existing_player.pop("disconnected_at", None)
        existing_player.pop("disconnect_expired", None)
        room["events"].append({"content": f"系统: {name} 重新连接"})
    else:
        pid = "p_" + uuid4().hex
        room["players"].append({"id": pid, "name": name, "target": "", "ready": False, "finished": False, "last_guess_wrong": False})
        connections[room_id].append({"ws": websocket, "id": pid, "name": name})
        room["events"].append({"content": f"系统: {name} 加入房间 (角色库: {room['library_name']})"})
    update_host(room_id)
    
    try:
        # Register the replacement first so the old socket's finally cannot mark it offline.
        for conn in replaced_connections:
            with suppress(OSError, RuntimeError, WebSocketDisconnect):
                await conn["ws"].close(code=4001)
        reconcile_room(room_id)
        await broadcast(room_id)
        while True:
            msg = await websocket.receive_text()
            if rooms.get(room_id) is not room or not any(
                c["ws"] is websocket for c in connections.get(room_id, [])
            ):
                break
            touch_room(room_id)
            state_changed = reconcile_room(room_id)

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            action = data.get("action")

            if action == "PING":
                await websocket.send_text(json.dumps({"type": "PONG"}))
                if state_changed:
                    await broadcast(room_id)
                continue
            
            if action == "LEAVE_ROOM":
                connections[room_id] = [c for c in connections[room_id] if c["ws"] is not websocket]
                player = next((p for p in room["players"] if p["id"] == pid), None)
                if room["started"] and player:
                    player["disconnected_at"] = 0
                    player["disconnect_expired"] = True
                    player["left"] = True
                else:
                    room["players"] = [p for p in room["players"] if p["id"] != pid]
                room["events"].append({"content": f"系统: {name} 离开了房间。"})
                if not eligible_player_ids(room_id):
                    del rooms[room_id]
                    del connections[room_id]
                else:
                    reconcile_room(room_id)
                    update_host(room_id)
                    await broadcast(room_id)
                await websocket.close()
                break

            elif action == "SEND_CHAT":
                text = data.get("text", "")
                if isinstance(text, str):
                    text = text.strip()
                    if text:
                        room["chat_messages"].append({
                            "name": name,
                            "text": text[:500],
                            "time": time.strftime("%H:%M")
                        })
                        room["chat_messages"] = room["chat_messages"][-100:]

            elif action == "TOGGLE_READY" and not room["started"]:
                for p in room["players"]:
                    if p["id"] == pid:
                        p["ready"] = not p["ready"]
                        status = "已准备 🟢" if p["ready"] else "取消准备 ⚪"
                        room["events"].append({"content": f"系统: {name} {status}"})
            
            elif action == "START_GAME" and not room["started"]:
                if pid != room.get("host_id"):
                    continue
                if len(room["players"]) < 2 or not all(
                    p["ready"] and p["id"] in connected_player_ids(room_id)
                    for p in room["players"]
                ):
                    room["events"].append({"content": "系统: 至少需要两位玩家，且所有玩家都在线并准备后才能开始游戏！"})
                else:
                    room["started"] = True
                    room["turn_index"] = 0
                    room["turn_id"] = uuid4().hex
                    room["current_question"] = None
                    room["question_history"] = []
                    
                    # 从绑定的角色库中随机抽取人物
                    chars_pool = room["characters"]
                    chars = random.sample(chars_pool, min(len(room["players"]), len(chars_pool)))
                    
                    for i, p in enumerate(room["players"]): 
                        p["target"] = chars[i % len(chars)]
                        p["finished"] = False
                        p["last_guess_wrong"] = False
                    room["events"].append({"content": "系统: 游戏开始，角色已分配！轮到【" + room["players"][0]["name"] + "】提问。"})
            
            elif action == "ASK_QUESTION" and room["started"]:
                current_player = room["players"][room["turn_index"]]
                if (pid == current_player["id"] and room["current_question"] is None
                        and not current_player["finished"] and data.get("turn_id") == room["turn_id"]):
                    text = data.get("text", "").strip()
                    if text:
                        current_player["last_guess_wrong"] = False
                        room["current_question"] = {
                            "id": uuid4().hex,
                            "asker_id": pid,
                            "asker_name": current_player["name"],
                            "text": text,
                            "answers": {},
                            "stage": "answering"
                        }
                        room["events"].append({"content": f"❓ 【{current_player['name']}】 提问: {text}"})
                        move_question_to_guessing_if_ready(room_id)
            
            elif action == "ANSWER_QUESTION" and room["started"]:
                q = room["current_question"]
                if (q and q["stage"] == "answering" and pid != q["asker_id"]
                        and data.get("question_id") == q["id"] and pid not in q["answers"]):
                    ans = data.get("answer")
                    if ans in ["是", "否", "是也不是"]:
                        q["answers"][pid] = ans
                        responder_name = next((p["name"] for p in room["players"] if p["id"] == pid), "某玩家")
                        room["events"].append({"content": f"💬 【{responder_name}】 回答 【{q['asker_name']}】: 【{ans}】"})
                        
                        move_question_to_guessing_if_ready(room_id)
            
            elif action == "MAKE_GUESS" and room["started"]:
                q = room["current_question"]
                if (q and q["stage"] == "guessing" and pid == q["asker_id"]
                        and data.get("question_id") == q["id"]):
                    guess_text = data.get("guess", "").strip()
                    finish_question(room_id, guess_text)
            
            await broadcast(room_id)
            
    except WebSocketDisconnect:
        pass
    finally:
        # A superseded socket must neither process actions nor disconnect its replacement.
        if rooms.get(room_id) is room and any(
            c["ws"] is websocket for c in connections.get(room_id, [])
        ):
            connections[room_id] = [c for c in connections[room_id] if c["ws"] is not websocket]
            touch_room(room_id)
            player = next((p for p in room["players"] if p["id"] == pid), None)
            if player:
                player["disconnected_at"] = time.time()
                room["events"].append({"content": f"系统: {name} 连接中断，保留 {RECONNECT_GRACE_SECONDS} 秒等待重连。"})
            await broadcast(room_id)
