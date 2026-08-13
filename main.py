from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import random
import time
import os
from typing import List

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# 目录配置：预设（只读） vs 自定义（可写）
PRESET_DIR = "data/presets"
CUSTOM_DIR = "data/custom"
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

# --- 游戏与房间管理逻辑 ---
rooms = {}
connections = {}
ROOM_TIMEOUT = 600

def cleanup_inactive_rooms():
    now = time.time()
    expired_rooms = [
        r_id for r_id, room in rooms.items() 
        if now - room.get("last_activity", now) > ROOM_TIMEOUT
    ]
    for r_id in expired_rooms:
        if r_id in rooms: del rooms[r_id]
        if r_id in connections: del connections[r_id]

def touch_room(room_id: str):
    if room_id in rooms:
        rooms[room_id]["last_activity"] = time.time()

def advance_turn(room):
    players = room["players"]
    if not players: return
    
    if all(p.get("finished", False) for p in players):
        room["started"] = False
        room["turn_index"] = 0
        room["current_question"] = None
        room["question_history"] = []
        for p in players:
            p["ready"] = False
            p["finished"] = False
            p["last_guess_wrong"] = False
            p["target"] = ""
        room["events"].append({"content": "🏆 游戏结束！所有玩家都已成功猜出自己的身份！房间已重置。"})
        return
    
    n = len(players)
    for i in range(1, n + 1):
        next_idx = (room["turn_index"] + i) % n
        if not players[next_idx].get("finished", False):
            room["turn_index"] = next_idx
            break

async def broadcast(room_id: str):
    if room_id not in connections: return
    room = rooms.get(room_id)
    if not room: return
    for conn in connections[room_id]:
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
                    "targetPerson": (p["target"] if not room["started"] or p["id"] != conn_id or p.get("finished", False) else "???"), 
                    "isMe": p["id"] == conn_id
                } 
                for p in room["players"]
            ],
            "started": room["started"],
            "turn_index": room.get("turn_index", 0),
            "current_question": room.get("current_question"),
            "question_history": room.get("question_history", []),
            "events": room["events"],
            "library_name": room.get("library_name", "默认")
        }
        try: 
            await conn["ws"].send_text(json.dumps(payload, ensure_ascii=False))
        except: 
            pass

# 通过 Query Parameter 接收传入的角色库名
@app.websocket("/ws/{room_id}/{name}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, name: str, library: str = "默认历史人物"):
    await websocket.accept()
    cleanup_inactive_rooms()
    
    if room_id not in rooms:
        # 首个进入房间的玩家决定该房间的角色库
        safe_name = os.path.basename(library)
        
        # 1. 先在自定义目录 (CUSTOM_DIR) 中查找
        lib_path = os.path.join(CUSTOM_DIR, f"{safe_name}.json")
        
        # 2. 如果自定义目录没有，再到预设目录 (PRESET_DIR) 中查找
        if not os.path.exists(lib_path):
            lib_path = os.path.join(PRESET_DIR, f"{safe_name}.json")
            
        # 3. 如果两边都找不到，直接抛出异常退出，拒绝默默降级
        if not os.path.exists(lib_path):
            raise HTTPException(status_code=404, detail=f"角色库 '{safe_name}' 不存在")

        # 读取角色库内容
        with open(lib_path, "r", encoding="utf-8") as f:
            characters = json.load(f)

        rooms[room_id] = {
            "started": False, 
            "players": [], 
            "turn_index": 0, 
            "current_question": None, 
            "question_history": [], 
            "events": [],
            "last_activity": time.time(),
            "library_name": library,
            "characters": characters
        }
        connections[room_id] = []
    
    room = rooms[room_id]
    touch_room(room_id)
    
    existing_player = next((p for p in room["players"] if p["name"] == name), None)
    if room["started"] and not existing_player:
        await websocket.send_text(json.dumps({"type": "ERROR", "message": "🚫 该房间游戏已在进行中，不允许中途加入！"}, ensure_ascii=False))
        await websocket.close()
        return

    if existing_player:
        pid = existing_player["id"]
        connections[room_id] = [c for c in connections[room_id] if c["name"] != name]
        connections[room_id].append({"ws": websocket, "id": pid, "name": name})
        room["events"].append({"content": f"系统: {name} 重新连接"})
    else:
        pid = "p_" + str(random.randint(1000, 9999))
        room["players"].append({"id": pid, "name": name, "target": "", "ready": False, "finished": False, "last_guess_wrong": False})
        connections[room_id].append({"ws": websocket, "id": pid, "name": name})
        room["events"].append({"content": f"系统: {name} 加入房间 (角色库: {room['library_name']})"})
    
    await broadcast(room_id)

    try:
        while True:
            msg = await websocket.receive_text()
            touch_room(room_id)
            
            data = json.loads(msg)
            action = data.get("action")
            
            if action == "TOGGLE_READY" and not room["started"]:
                for p in room["players"]:
                    if p["id"] == pid:
                        p["ready"] = not p["ready"]
                        status = "已准备 🟢" if p["ready"] else "取消准备 ⚪"
                        room["events"].append({"content": f"系统: {name} {status}"})
            
            elif action == "START_GAME" and not room["started"]:
                if not room["players"] or not all(p["ready"] for p in room["players"]):
                    room["events"].append({"content": "系统: 必须所有玩家都点击准备后才能开始游戏！"})
                else:
                    room["started"] = True
                    room["turn_index"] = 0
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
                if pid == current_player["id"] and room["current_question"] is None and not current_player["finished"]:
                    text = data.get("text", "").strip()
                    if text:
                        current_player["last_guess_wrong"] = False
                        room["current_question"] = {
                            "asker_id": pid,
                            "asker_name": current_player["name"],
                            "text": text,
                            "answers": {},
                            "stage": "answering"
                        }
                        room["events"].append({"content": f"❓ 【{current_player['name']}】 提问: {text}"})
            
            elif action == "ANSWER_QUESTION" and room["started"]:
                q = room["current_question"]
                if q and q["stage"] == "answering" and pid != q["asker_id"]:
                    ans = data.get("answer")
                    if ans in ["是", "否"]:
                        q["answers"][pid] = ans
                        responder_name = next((p["name"] for p in room["players"] if p["id"] == pid), "某玩家")
                        room["events"].append({"content": f"💬 【{responder_name}】 回答 【{q['asker_name']}】: 【{ans}】"})
                        
                        other_players = [p for p in room["players"] if p["id"] != q["asker_id"]]
                        if all(p["id"] in q["answers"] for p in other_players):
                            q["stage"] = "guessing"
                            room["events"].append({"content": f"📢 【{q['asker_name']}】的提问已收到所有回答，请进行猜测或跳过！"})
            
            elif action == "MAKE_GUESS" and room["started"]:
                q = room["current_question"]
                if q and q["stage"] == "guessing" and pid == q["asker_id"]:
                    current_player = room["players"][room["turn_index"]]
                    guess_text = data.get("guess", "").strip()
                    if guess_text:
                        target = current_player["target"]
                        if guess_text == target:
                            current_player["finished"] = True
                            current_player["last_guess_wrong"] = False
                            room["events"].append({"content": f"🎉 恭喜 【{current_player['name']}】 成功猜出自己是【{target}】！通关成功！ 🎉"})
                        else:
                            current_player["last_guess_wrong"] = True
                            room["events"].append({"content": f"❌ 【{current_player['name']}】 猜测【{guess_text}】错误！"})
                    else:
                        current_player["last_guess_wrong"] = False
                        room["events"].append({"content": f"⏭️ 【{current_player['name']}】 放弃了本轮猜测。"})
                    
                    room["question_history"].append(q)
                    room["current_question"] = None
                    advance_turn(room)
            
            await broadcast(room_id)
            
    except WebSocketDisconnect:
        connections[room_id] = [c for c in connections[room_id] if c["ws"] != websocket]
        if room_id in rooms:
            touch_room(room_id)
            disconnected_player = next((p for p in rooms[room_id]["players"] if p["id"] == pid), None)
            
            if not room["started"]:
                rooms[room_id]["players"] = [p for p in rooms[room_id]["players"] if p["id"] != pid]
            
            if disconnected_player:
                rooms[room_id]["events"].append({"content": f"系统: {name} 离线"})
            
            if not connections[room_id] and not room["started"]:
                del connections[room_id]
                del rooms[room_id]
            else:
                await broadcast(room_id)
