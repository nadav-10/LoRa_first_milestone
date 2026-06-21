import argparse
import json
import os
import sys
import time
import requests
import base64
import threading
import struct
import random
import queue
import threading
import glob
import hashlib

# Ensure lora_common is in path for pubkey
sys.path.append(os.path.join(os.path.dirname(__file__), 'lora_common'))
from pubkey import RSA1024
INPUT_COMMAND_DELAY = 2.5
DEFAULT_INTERVAL = 30
REMOTE_SYNC_INTERVAL = 10
SIM_LOOP_DELAY = 1
ROUTER_URL = "http://localhost:8200"
COORDINATOR_URL = "http://34.165.8.95:8080"  # Franji's Google cloud node 2025-04-16
# use http://34.165.8.95:8080/coordinator_stats to check stats

class UserState:
    def __init__(self, filename=None, nickname=None):
        self.lock = threading.RLock()
        self.delay_secs = 0
        self.key = None 
        self.key_id = None
        self.messages = []
        self.last_out_seq = {}
        self.messages_by_seq = {}
        self.last_in_seq = {}
        self.messages_to_send : queue = queue.Queue()
        
        if nickname:
            self.nickname = nickname
            self.fullname = nickname
            self.phone = ""
            self.password = ""
            self.filename = f"user_{nickname}.json"
            if os.path.exists(self.filename):
                self.load()
            else:
                self.generate()
        elif filename and os.path.exists(filename):
            self.filename = filename
            self.load()
        else:
            raise ValueError("Must provide either filename or nickname")

    def load(self):
        try:
            with open(self.filename, 'r') as f:
                data = json.load(f)
                self.nickname = data.get('nickname', 'unknown')
                self.fullname = data.get('fullname', self.nickname)
                self.password = data.get('password', '')
                self.phone = data.get('phone', '')
                pem = data.get('private_key')
                if pem:
                    self.key = RSA1024.from_private_text(pem)
                    self.key_id = int.from_bytes(self.key.key_id(), 'big')
                self.messages = data.get('messages', [])
                self.last_out_seq = data.get('last_seq', {})
        except Exception as e:
            print(f"[ERROR] Error loading state from {self.filename}: {e}")
            

    def generate(self):
        print(f"[DEBUG] Generating new key pair for {self.nickname}...", flush=True)
        self.key = RSA1024.generate()
        self.key_id = int.from_bytes(self.key.key_id(), 'big')
        self.save()

    def save(self):
        with self.lock:
            data = {
                'nickname': self.nickname,
                'fullname': self.fullname,
                'password': self.password,
                'phone': self.phone,
                'private_key': self.key.private_key_text() if self.key else None,
                'messages': self.messages,
                'last_seq': self.last_out_seq
            }
            try:
                with open(self.filename, 'w') as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                print(f"[ERROR] Error saving {self.filename}: {e}", flush=True)

    def add_message(self, msg):
        with self.lock:
            if msg not in self.messages:
                self.messages.append(msg)
                self.save()
                return True
        return False
    
def read_line(user : UserState, local_users, remote_users):
    while True:
        text = input("Enter command. Formats: '<target>#<message>', '<detail>$<new_value>', 'USERS'\n")
        if text.count('#') == 1 and text.count('$') == 0:
            splt_text = text.split('#')
            try:
                user.messages_to_send.put((int(splt_text[0]), splt_text[1]))
            except (ValueError, TypeError):
                print("[ERROR] Invalid target key_id. Must be an integer.")
        elif text.count('$') == 1 and text.count('#') == 0:
            splt_text = text.split('$')
            if splt_text[0] == 'phone':
                user.phone = splt_text[1]
            elif splt_text[0] == 'nick':
                user.nickname = splt_text[1]
            elif splt_text[0] == 'name':
                user.fullname = splt_text[1]
            else:
                print("[ERROR] Invalid detail field. Use 'phone', 'nick', or 'name'.")
            user.save()
        elif text.strip().upper() == "USERS":
            print("Known users:")
            print("\n".join(f"local - key id: {u.key_id}, nickname: {u.nickname}, full name: {u.fullname}, phone: {u.phone}" for u in local_users))
            print("\n".join(f"remote - key id: {u['key_id']}, nickname: {u['nick']}, full name: {u['name']}, phone: {u['phone']}, delay: {u['delay_secs']}" for u in remote_users.values()))
        else:
            print("[ERROR] Invalid command format. Please use '<target>#<message>' or '<detail>$<new_value>' or 'USERS'.")
        time.sleep(INPUT_COMMAND_DELAY)

class Simulator:
    def __init__(self, url, coordinator_url, interval, verbose=False, plain_text=False):
        self.lock = threading.RLock()
        self.url = url
        self.coordinator_url = coordinator_url
        self.interval = interval
        self.verbose = verbose
        self.plain_text = plain_text
        self.local_users = []
        self.remote_users = {} # key_id -> dict
        self.users_registery = {}
        self.running = True
        self.stats = {'sent': 0, 'received': 0, 'replied': 0, 'cleared': 0}
        self.message_counter = 0
        # self.session_prefix = int(time.time() % 100000)
        self.router_connected = False
        self.coordinator_connected = False

    def load_users(self):
        files = glob.glob("user_*.json")
        for f in files:
            try:
                u = UserState(filename=f)
                if u.key_id:
                    self.local_users.append(u)
                    self.log(f"[DEBUG] Loaded local user: {u.nickname} ({u.key_id})")
            except Exception as e:
                self.log(f"[DEBUG] Failed to load user file {f}: {e}")

    def create_user(self, nickname):
        filename = f"user_{nickname}.json"
        if os.path.exists(filename):
            print(f"[ERROR]: User '{nickname}' already exists. Not recreating.", flush=True)
            return
        u = UserState(nickname=nickname)
        self.log(f"[DEBUG] Created user: {u.nickname} ({u.key_id})")

    def log(self, msg):
        if self.verbose:
           print(f"[VERBOSE] {msg}", flush=True)
    def report_to_coordinator(self, event_type, sender, receiver, message_id, **kwargs):
        return None
        report = {
            'type': event_type,
            'sender': sender,
            'receiver': receiver,
            'message_id': message_id,
            'timestamp': time.time()
        }
        report.update(kwargs)
        try:
            r = requests.post(f"{self.coordinator_url}/report", json=report, timeout=2)
            if r.status_code == 200:
                if not self.coordinator_connected:
                    print(f"[DEBUG] Connected to coordinator: {self.coordinator_url}", flush=True)
                    self.coordinator_connected = True
            else:
                self.log(f"[DEBUG] Coordinator report returned {r.status_code}: {r.text}")
        except Exception as e:
            self.log(f"[ERROR] Coordinator report failed: {e}")

    def sync_remote_users(self):
        try:
            r = requests.get(f"{self.url}/users", timeout=5)
            if r.status_code == 200:
                if not self.router_connected:
                    print(f"[DEBUG] Connected to router: {self.url}", flush=True)
                    self.router_connected = True
                users = r.json().get('users', [])

                with self.lock:
                    received_key_ids = {u['key_id'] for u in users}
                    simulated_local_ids = {lu.key_id for lu in self.local_users}
                    
                    for lu in self.local_users:
                        if lu.key_id not in received_key_ids:
                            print(f"\n[DEBUG] Warning: Local user {lu.nickname} ({lu.key_id}) is NOT reported by router /users API.", flush=True)
                    has_remote = any(uid not in simulated_local_ids for uid in received_key_ids)
                    if users and not has_remote:
                        if self.verbose:
                            print("\n[DEBUG] Only local users found in /users. No remote routers/users discovered yet.", flush=True)
                    for u in users:
                        kid = u['key_id']
                        self.remote_users[kid] = u  # שומרת מילונים בנפרד
                        
                        if kid not in simulated_local_ids:
                            remote_file = f"remote_{kid}.json"
                            if not os.path.exists(remote_file):
                                try:
                                    with open(remote_file, 'w') as f:
                                        json.dump(u, f, indent=2)
                                except Exception as e:
                                    self.log(f"[ERROR] Failed to save {remote_file}: {e}")
        except Exception as e:
            self.log(f"[ERROR] Failed to sync users: {e}")

    def decrypt_message(self, local_user, msg):
        if 'text' in msg: return None, None, msg['text']
        
        is_plain = 'plain2_b64' in msg
        if not is_plain and 'crypt2_b64' not in msg:
            return None, None, None
            
        sender_id = msg['sender']
        sender_info = self.remote_users.get(sender_id)
        if not sender_info and not is_plain:
            return None, None, None
            
        try:
            if is_plain:
                decrypted_payload = base64.b64decode(msg['plain2_b64'])
            else:
                sender_pub_key = RSA1024.from_public_bytes(base64.b64decode(sender_info['key_b64']))
                crypt2 = base64.b64decode(msg['crypt2_b64'])
                encrypted = sender_pub_key.encrypt(crypt2)
                decrypted_payload = local_user.key.decrypt(encrypted)
            
            if len(decrypted_payload) < 3: return None
            seq, ack, text_len = struct.unpack("BBB", decrypted_payload[:3])
            return seq, ack, text_len, decrypted_payload[3:3+text_len].decode('utf-8')
        except Exception as e:
            return None, None, None

    def send_text(self, sender_user : UserState, to_key_id, text, report=False, mid=None):
        try:
            target_info = self.remote_users.get(to_key_id)
            if not target_info and not self.plain_text:
                return False
            
            seq = (sender_user.last_out_seq.get(to_key_id, 0) + 1) % 256
            if seq == 0: seq = 1
            sender_user.last_out_seq[to_key_id] = seq
            sender_user.save()
            if to_key_id not in sender_user.messages_by_seq:
                sender_user.messages_by_seq[to_key_id] = {}
            sender_user.messages_by_seq[to_key_id][seq] = text
            text_bytes = text.encode('utf-8')
            payload = struct.pack("BBB", seq, sender_user.last_in_seq.get(to_key_id, 0), len(text_bytes)) + text_bytes
         #   print(f"[DEBUG] len is {len(payload)} payload is {payload}")
            data = {
                'sender': sender_user.key_id,
                'to': to_key_id,
                'utime': int(time.time())
            }

            if self.plain_text:
                plain2_b64 = base64.b64encode(payload).decode('ascii')
                data['plain2_b64'] = plain2_b64
                payload_hash = hashlib.sha256(payload).hexdigest()
            else:
                target_pub_key = RSA1024.from_public_bytes(base64.b64decode(target_info['key_b64']))
                encrypted = target_pub_key.encrypt(payload)
           #     print(f"[DEBUG] len is {len(encrypted)} encrypted is {encrypted}")
                signed_encrypted = sender_user.key.decrypt(encrypted)
                crypt2_b64 = base64.b64encode(signed_encrypted).decode('ascii')
                data['crypt2_b64'] = crypt2_b64
                payload_hash = hashlib.sha256(signed_encrypted).hexdigest()

            if text != "":
                print(f"[DEBUG] from {sender_user.key_id} to {to_key_id} sent {text}")
            r = requests.post(f"{self.url}/text", json=data, timeout=5)
            if r.status_code == 200:
                if report and mid:
                    self.stats['sent'] += 1
               #     self.report_to_coordinator('SENT', sender_user.key_id, to_key_id, mid, payload_hash=payload_hash)
                return True
            return False
        except Exception as e:
            print(f"[ERROR]: there was an error in send_text {e}")
            return False

    def poll_user(self, user : UserState):
        try:
            key_b64 = base64.b64encode(user.key.public_bytes()).decode('ascii')
            details_str = f"{user.nickname}:{user.fullname}:{user.phone}"
            details_bytes = details_str.encode('utf-8')
            details = bytes([len(details_bytes)]) + details_bytes
            details_signed = user.key.decrypt(details)
            details_b64 = base64.b64encode(details_signed).decode('ascii')

            data = {
                'key_b64': key_b64,
                'details_b64': details_b64
            }
           # print(f"sending /alive: {data}")
            r = requests.post(f"{self.url}/alive", json=data, timeout=5)
            if r.status_code == 200:
              #  print(f"response from /alive: {r.json()}")

                resp = r.json()
                m : dict
                for m in resp.get('messages', []):
                    if m not in user.messages:
                        seq, ack, text_len, text = self.decrypt_message(user, m)
                        sender_id = m['sender']
                        k = {}
                        for a,b in m.items():
                            k[a] = b
                        if text and len(text) > 0:
                            k['text'] = text
                        user.add_message(k)
                        if text_len > 0:
                            crypt2 = base64.b64decode(m.get('crypt2_b64', m.get('plain2_b64', '')))
                            payload_hash = hashlib.sha256(crypt2).hexdigest()
                            
                            self.log(f"[{user.nickname}] Received: {text} from {sender_id}")
                            print(f"[DEBUG] from {sender_id} got {text}")
                            # mid = text
                            self.stats['received'] += 1
                          #  self.report_to_coordinator('RECEIVED', sender_id, user.key_id, text, payload_hash=payload_hash)
                            user.last_in_seq[sender_id] = seq
                            self.send_text(user, sender_id, '')
                            print(f"[DEBUG] sent ACK on message {seq} to {sender_id}")
                            self.stats['replied'] += 1
                          #  self.report_to_coordinator('REPLIED', sender_id, user.key_id, text, payload_hash=payload_hash)

                        if ack != 0 and ack in user.messages_by_seq.get(sender_id, {}):
                            # mid = text
                            self.stats['cleared'] += 1
                           # self.report_to_coordinator('CLEARED', user.key_id, sender_id, text, payload_hash=payload_hash)
                            print(f"[DEBUG] got ACK on message {ack} from {sender_id} - {user.messages_by_seq.get(sender_id, {}).pop(ack)}")
                            #TODO why not to save ack instead of payload_hash?
                        else:
                            self.log(f"[{user.nickname}] Delaying message from {m.get('sender')}: missing key or decryption failed.")
        except Exception as e:
            print(f"[DEBUG] poll_user error for {user.nickname}: {e}")
            self.log(f"[DEBUG] poll_user error for {user.nickname}: {e}")

    def get_router_stats(self):
        try:
            r = requests.get(f"{self.url}/stats", timeout=2)
            if r.status_code == 200:
                data = r.json()
                data.pop('status', None) # remove status OK so it's less noisy
                return data
        except:
            pass
        return None

    def get_coordinator_stats(self):
        return {}
        try:
            r = requests.get(f"{self.coordinator_url}/coordinator_stats", timeout=2)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None

    def run(self):
        last_sync = 0
        # last_send = {}

        print(f"[DEBUG] Starting simulator with {len(self.local_users)} local users...", flush=True)
        
        while self.running:
            now = time.time()
            
            if now - last_sync > REMOTE_SYNC_INTERVAL:
                self.sync_remote_users()
                last_sync = now

            for u in self.local_users:
                self.poll_user(u)

            
            nicks = ",".join([u.nickname for u in self.local_users])
            stats_msg = f"Stats[{nicks}]: Sent:{self.stats['sent']} Received:{self.stats['received']} Acked:{self.stats['replied']} Cleared:{self.stats['cleared']}   "
            if not self.verbose:
                # print(f"\r{stats_msg}", end="", flush=True)
                pass
            
            # Print global and router stats every 10 seconds
            if int(now) % 10 == 0 and getattr(self, "_last_stats_print", 0) != int(now):
                self._last_stats_print = int(now)
                if self.verbose:
                    #print(f"\n[MONITOR] {stats_msg}", flush=True)
                    pass
                
                # Global (Coordinator) Stats
                c_stats = self.get_coordinator_stats()
                if c_stats:
                    diag = c_stats.get('diagnostics', {})
                    env_drop = c_stats.get('environmental_drops', 0)
                    global_msg = (f"[GLOBAL] Total:{c_stats.get('total_sent')} "
                                 f"Cleared:{c_stats.get('total_cleared')} "
                                 f"Drops(Env:{env_drop} Snd:{diag.get('lost_at_sender_router')} "
                                 f"Med:{diag.get('lost_in_medium')} Rcv:{diag.get('lost_at_receiver_router')})")
                    # if not self.verbose: print("") # New line before global stats
                    # print(global_msg, flush=True)

                # Router Stats
                r_stats = self.get_router_stats()
                if r_stats:
                    r_msg = f"[ROUTER] {self.url}: " + " ".join([f"{k}:{v}" for k, v in r_stats.items()])
                    #print(r_msg, flush=True)
            
            time.sleep(SIM_LOOP_DELAY)


import os
import json
from flask import Flask, render_template, request, redirect, url_for
from flask import session

app = Flask(__name__)
app.secret_key = 'sa7f87saaviuoduiofrqu0s0fs'
global_sim = None
# Directory where we save user files (same as your project dir)
USER_DIR = "."

@app.route('/', methods=['GET', 'POST'])
def index():
    message = None
    
    if request.method == 'POST':
        fullname = request.form.get('fullname').strip()
        password = request.form.get('password')
        action = request.form.get('action')
        
        filename = f"user_{fullname}.json"
        
        if action == "signup":
            if os.path.exists(filename):
                message = f"User '{fullname}' already exists! Try logging in."
            else:
                try:
                    # 1. יצירת המשתמש (מייצר מפתחות ושומר אוטומטית)
                    new_user = UserState(nickname=fullname)
                    
                    # 2. עדכון הסיסמה והשם בתוך האובייקט
                    new_user.password = password
                    new_user.fullname = fullname
                    
                    # 3. שמירה מחדש עם הסיסמה המעודכנת
                    new_user.save()
                    if global_sim:
                        # בודקים שהמשתמש לא קיים כבר ברשימה הריצה
                        if not any(u.nickname == fullname for u in global_sim.local_users):
                            global_sim.local_users.append(new_user)
                    
                    message = f"User '{fullname}' created successfully! You can now log in."
                except Exception as e:
                    message = f"[ERROR] Failed to create user: {e}"
                
        elif action == "login":
            if not os.path.exists(filename):
                message = "User not found. Please Sign Up first."
            else:
                try:
                    # 1. טעינת המשתמש דרך המחלקה (טוענת אוטומטית גם את הסיסמה)
                    user = UserState(filename=filename)
                    
                    # 2. בדיקה פשוטה בין מה שהוקלד למה ששמור באובייקט
                    if user.password == password:
                        if global_sim:
                            if not any(u.nickname == fullname for u in global_sim.local_users):
                                global_sim.local_users.append(user)
                        
                            # --- ההזרקה למילון המאוחד כאן: ---
                            global_sim.users_registery[user.key_id] = user

                        session['key_id'] = user.key_id
                        session['user_key_id'] = user.key_id  # בשביל ה-HTML של השותף
                        return redirect(url_for('home_page'))
                    else:
                        message = "Incorrect password. Please try again."
                except Exception as e:
                    message = f"[ERROR] Failed to log in: {e}"

    return render_template('index.html', message=message)


# נניח שמשתנה ה-sim הגלובלי שלך נגיש (נגדיר אותו מחוץ ל-main או נשמור התייחסות אליו)
# לצורך הפשטות, ודא שמשתנה ה-sim שנוצר ב-main נשמר כמשתנה גלובלי בקובץ, למשל: global_sim = None

@app.route('/messages/<int:target_id>', methods=['GET', 'POST'])
def chat(target_id):
    status_message = None
    
    current_id = session.get('key_id')
    
    # 1. שליפה מוקדמת של המשתמש המקומי - מחוץ לכל התנאים כדי שיהיה נגיש תמיד!
    sender_user = None
    if global_sim and global_sim.local_users and current_id:
        sender_user = next((u for u in global_sim.local_users if u.key_id == current_id), None)

    if request.method == 'POST':
        text = request.form.get('message_text')
        

        if global_sim and global_sim.local_users and current_id:
            if sender_user:
            # 2. שימוש בפונקציה המקורית מהקוד שלך לשליחת ההודעה ברשת
                success = global_sim.send_text(sender_user, target_id, text, report=True, mid=text)
            
                if success:
                    status_message = "Message sent successfully!"
                    sender_user.add_message({'sender': current_id, 'to': target_id, 'text': text})
                else:
                    status_message = "Failed to send message. Target user might be offline or unknown."
        else:
            status_message = "Error: No local logged-in user found."

   # שליפת נתוני היעד (האדם השני)
    target_user_obj = None
    if global_sim and target_id in global_sim.users_registery:
        target_user_obj = global_sim.users_registery[target_id]
    
    if not target_user_obj:
        class TemporaryUser: pass
        target_user_obj = TemporaryUser()
        target_user_obj.key_id = target_id
        target_user_obj.nickname = "Unknown"
        target_user_obj.fullname = "Unknown User"
        target_user_obj.phone = "N/A"

    # --- שליפת היסטוריית ההודעות המלאה שלי ---
    chat_history = sender_user.messages if sender_user else []

    return render_template('chat.html', 
                           target_user=target_user_obj, 
                           status_message=status_message,
                           chat_history=chat_history,  # מועבר ל-HTML
                           my_id=current_id)           # מועבר ל-HTML לצורך זיהוי השולח


def _get_delay_color(delay_secs):
    if delay_secs <= 5:
        return "green"
    elif delay_secs <= 30:
        return "orange"
    else:
        return "red"
    
@app.route('/home')
def home_page():
    """
    Renders the placeholder home page (localhost:5000/home).
    """
    current_id = session.get('key_id')
    if not current_id or not global_sim:
        return "<h1>Access Denied: Error 401</h1><p>You must log in first to view this page!</p>", 401

    with global_sim.lock:
        # 1. שליפת המשתמש המקומי המחובר כעת (תמיד אובייקט UserState)    
        current_user = next((u for u in global_sim.local_users if u.key_id == current_id), None)
        if not current_user:
            return "<h1>Access Denied: Error 401</h1><p>Session user not found. Please log in again.</p>", 401

        # יצירת סט של ה-IDs המקומיים שלנו כדי למנוע כפילויות
        local_ids = {u.key_id for u in global_sim.local_users}

        users_list = []

        # א) הוספת משתמשים מקומיים אחרים (אם קיימים) - הם כבר אובייקטים
        for lu in global_sim.local_users:
            if lu.key_id != current_id:
                users_list.append(lu)

        # ב) הוספת משתמשים חיצוניים מהרשת (שהם כרגע מילונים ב-remote_users)
        if global_sim.remote_users:
            for kid, data in global_sim.remote_users.items():
                if kid not in local_ids:  # מתעלמים ממי שהוא המשתמש המקומי שלנו
                    class TemporaryUser:
                        pass
                    u = TemporaryUser()
                    u.key_id = data.get('key_id', kid)
                    u.nickname = data.get('nick', 'Unknown')
                    u.fullname = data.get('name', 'External User')
                    u.phone = data.get('phone', 'N/A')
                    u.delay_secs = data.get('delay_secs', 0)
                    
                    # עכשיו u הוא אובייקט לכל דבר, וה-HTML של עמית יוכל לקרוא u.key_id בלי לקרוס!
                    users_list.append(u)

    # מעבירים את users_list (שמכילה רק אובייקטים) ואת current_user ל-HTML
    return render_template('home.html', users=users_list, current_user=current_user, get_color=_get_delay_color)

@app.route('/update-profile', methods=['POST'])
def update_profile():
    if not session.get('key_id') in sim.users_registery:
        return "<h1>Access Denied</h1>", 401
        
    current_id = session['key_id']
    current_user = sim.users_registery[current_id]
    current_user.fullname = request.form.get('fullname')
    current_user.nickname = request.form.get('nickname')
    current_user.phone = request.form.get('phone')
    current_user.is_encrypted = 'is_encrypted' in request.form
    return redirect(url_for('home_page'))

if __name__ == "__main__":
    print("[DEBUG] Initializing Simulator background thread...")

    # 2. יוצרים את אובייקט הסימולטור עם ערכי ברירת המחדל
    sim = Simulator(url=ROUTER_URL, coordinator_url=COORDINATOR_URL, interval=DEFAULT_INTERVAL)
    
    # 3. שומרים אותו בתוך המשתנה הגלובלי - עכשיו הפונקציה chat() תכיר אותו!
    global_sim = sim
    
    # 4. מפעילים את ה-Simulator ברקע כדי שלא יחסום את האתר
    simulator_thread = threading.Thread(target=sim.run, daemon=True)
    simulator_thread.start()
    print("[DEBUG] Simulator is now running in the background.")

    # 5. מפעילים את שרת ה-Flask (האתר)
    print("[DEBUG] Starting the English Auth Flask Server...")
    app.run(debug=True, port=5000, use_reloader=False)