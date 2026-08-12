from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import json
import random
import time

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_index():
    return FileResponse("static/index.html")

# --- 游戏与房间管理逻辑 ---
rooms = {}
connections = {}
HISTORICAL_FIGURES = historical_figures = [
    "孔子", "老子", "庄子", "孟子", "荀子", "墨子", "韩非", "孙武", "屈原", "商鞅", 
    "扁鹊", "吕不韦", "邹衍", "管仲", "晏子", "秦始皇", "刘邦", "项羽", "张良", "韩信", 
    "萧何", "汉武帝", "卫青", "霍去病", "张骞", "司马迁", "班超", "蔡伦", "张衡", "华佗", 
    "董仲舒", "王昭君", "曹操", "诸葛亮", "关羽", "刘备", "孙权", "周瑜", "司马懿", "陶渊明", 
    "王羲之", "祖逖", "祖冲之", "花木兰", "北魏孝文帝", "郦道元", "贾思勰", "谢安", "隋文帝", "唐太宗", 
    "武则天", "李白", "杜甫", "王维", "白居易", "韩愈", "柳宗元", "玄奘", "鉴真", "狄仁杰", 
    "魏征", "郭子仪", "颜真卿", "李商隐", "温庭筠", "文成公主", "僧一行", "宋太祖", "王安石", "苏轼", 
    "辛弃疾", "李清照", "岳飞", "文天祥", "司马光", "沈括", "毕昇", "朱熹", "成吉思汗", "忽必烈", 
    "关汉卿", "郭守敬", "耶律阿保机", "欧阳修", "陆游", "朱元璋", "朱棣", "张居正", "王阳明", "郑和", 
    "海瑞", "李时珍", "戚继光", "顾炎武", "康熙帝", "雍正帝", "乾隆帝", "曹雪芹", "纪晓岚", "纳兰性德"
]
ROOM_TIMEOUT = 600  # 房间无操作超时时间：10分钟 (600秒)

def cleanup_inactive_rooms():
    """自动清理超时未活动的僵尸房间"""
    now = time.time()
    expired_rooms = [
        r_id for r_id, room in rooms.items() 
        if now - room.get("last_activity", now) > ROOM_TIMEOUT
    ]
    for r_id in expired_rooms:
        if r_id in rooms:
            del rooms[r_id]
        if r_id in connections:
            del connections[r_id]

def touch_room(room_id: str):
    """刷新房间的最后活跃时间"""
    if room_id in rooms:
        rooms[room_id]["last_activity"] = time.time()

def advance_turn(room):
    players = room["players"]
    if not players: return
    
    # 所有玩家通关：重置房间回准备阶段，允许新玩家加入
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
        room["events"].append({"content": "🏆 游戏结束！所有玩家都已成功猜出自己的身份！房间已重置，等待新玩家加入或重新准备。"})
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
            "events": room["events"]
        }
        try: 
            await conn["ws"].send_text(json.dumps(payload, ensure_ascii=False))
        except: 
            pass

@app.websocket("/ws/{room_id}/{name}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, name: str):
    await websocket.accept()
    
    # 每次有新连接时，顺便扫描并清理过期房间
    cleanup_inactive_rooms()
    
    if room_id not in rooms:
        rooms[room_id] = {
            "started": False, 
            "players": [], 
            "turn_index": 0, 
            "current_question": None, 
            "question_history": [], 
            "events": [],
            "last_activity": time.time()
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
        room["events"].append({"content": f"系统: {name} 加入房间"})
    
    await broadcast(room_id)

    try:
        while True:
            msg = await websocket.receive_text()
            touch_room(room_id)  # 收到任何消息都刷新活跃时间
            
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
                    chars = random.sample(HISTORICAL_FIGURES, min(len(room["players"]), len(HISTORICAL_FIGURES)))
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
            
            # 若未开始且连接全断，直接清理
            if not connections[room_id] and not room["started"]:
                del connections[room_id]
                del rooms[room_id]
            else:
                await broadcast(room_id)
