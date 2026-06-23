import argparse
import json
import os
import sys
import time
import requests
import base64
import threading
import struct
import queue
import threading
import glob
import hashlib
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from pubkey import RSA1024

# Ensure lora_common is in path for pubkey
sys.path.append(os.path.join(os.path.dirname(__file__), 'lora_common'))
from pubkey import RSA1024
REMOTE_SYNC_INTERVAL = 10
SIM_LOOP_DELAY = 1
ROUTER_URL = "http://127.0.0.1:"
POLL_INTERVAL = 5

app = Flask(__name__)
app.secret_key = 'sa7f87saaviuoduiofrqu0s0fs'
sim = None
# Directory where we save user files (same as your project dir)
USER_DIR = "."

class UserState:
    def __init__(self, filename=None, nickname=None):
        self.lock = threading.RLock()
        self.delay_secs = 0
        self.key = None 
        self.key_id = None
        self.messages = [] #all messages, saved as a dict of the REST API protocol
        self.text_messages = []
        self.last_out_seq = {} #key_id -> seq
        self.messages_by_seq = {} #key_id -> (seq -> text)
        self.last_in_seq = {} #key_id -> seq
        self.messages_to_send : queue = queue.Queue() # (text, key_id (target))
        self.is_encrypted = False
        
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
    

class Simulator:
    def __init__(self, url, verbose=False):
        self.lock = threading.RLock()
        self.url = url
        self.verbose = verbose
        self.local_users = {} #key: key_id (int) value: user (UserState)
        self.remote_users = {} # key_id -> dict
        self.users_registery = {} #key_id -> dict
        self.load_users()
        self.running = True
        self.router_connected = False

    def load_users(self):
        files = glob.glob("user_*.json")
        for f in files:
            try:
                u = UserState(filename=f)
                if u.key_id:
                    self.local_users[u.key_id] = u
                    self.users_registery[u.key_id] = {
                                "phone" : u.phone,
                                "nick" : u.nickname,
                                "name" : u.fullname,
                                "key_id" : u.key_id,
                                "key_b64" : base64.b64encode(u.key.public_bytes()).decode('ascii'),
                                "delay_secs" : 0
                            }
                    self.log(f"[DEBUG] Loaded local user: {u.nickname} ({u.key_id})")
            except Exception as e:
                self.log(f"[ERROR] Failed to load user file {f}: {e}")

    def log(self, msg):
        if self.verbose:
           print(f"[VERBOSE] {msg}", flush=True)

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
                    simulated_local_ids = {lu.key_id for lu in self.local_users.values()}
                    
                    for lu in self.local_users.values():
                        if lu.key_id not in received_key_ids:
                            print(f"\n[DEBUG] Warning: Local user {lu.nickname} ({lu.key_id}) is NOT reported by router /users API.", flush=True)
                    has_remote = any(uid not in simulated_local_ids for uid in received_key_ids)
                    if users and not has_remote:
                        if self.verbose:
                            print("\n[DEBUG] Only local users found in /users. No remote routers/users discovered yet.", flush=True)
                    for u in users:
                        kid = u['key_id']
                        if kid not in simulated_local_ids:
                            self.users_registery[kid] = u
                            self.remote_users[kid] = u
                            remote_file = f"remote_{kid}.json"
                            if not os.path.exists(remote_file):
                                try:
                                    with open(remote_file, 'w') as f:
                                        json.dump(u, f, indent=2)
                                except Exception as e:
                                    self.log(f"[ERROR] Failed to save {remote_file}: {e}")
        except Exception as e:
            print(f"[ERRORT] failed to sync users: {e}")
            self.log(f"[ERROR] Failed to sync users: {e}")

    def decrypt_message(self, local_user, msg):
        is_plain = 'plain2_b64' in msg
        if not is_plain and 'crypt2_b64' not in msg:
            return None, None, 0, None
            
        sender_id = msg['sender']
        sender_info = self.users_registery.get(sender_id)
        if not sender_info and not is_plain:
            return None, None, 0, None
            
        try:
            if is_plain:
                decrypted_payload = base64.b64decode(msg['plain2_b64'])
            else:
                sender_pub_key = RSA1024.from_public_bytes(base64.b64decode(sender_info['key_b64']))
                crypt2 = base64.b64decode(msg['crypt2_b64'])
                encrypted = sender_pub_key.encrypt(crypt2)
                decrypted_payload = local_user.key.decrypt(encrypted)
            
            if len(decrypted_payload) < 3: return None, None, None, None
            seq, ack, text_len = struct.unpack("BBB", decrypted_payload[:3])
            return seq, ack, text_len, decrypted_payload[3:3+text_len].decode('utf-8')
        except Exception as e:
            print(f"[ERROR] an unknown exception happened while decrypting {e}")
            return None, None, None, None

    def send_text(self, sender_user : UserState, to_key_id, text):
        try:
            target_info = self.users_registery.get(to_key_id)
            if not target_info and sender_user.is_encrypted:
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

            if not sender_user.is_encrypted:
                plain2_b64 = base64.b64encode(payload).decode('ascii')
                data['plain2_b64'] = plain2_b64
            else:
                target_pub_key = RSA1024.from_public_bytes(base64.b64decode(target_info['key_b64']))
                encrypted = target_pub_key.encrypt(payload)
           #     print(f"[DEBUG] len is {len(encrypted)} encrypted is {encrypted}")
                signed_encrypted = sender_user.key.decrypt(encrypted)
                crypt2_b64 = base64.b64encode(signed_encrypted).decode('ascii')
                data['crypt2_b64'] = crypt2_b64

            if text != "":
                print(f"[DEBUG] from {sender_user.key_id} to {to_key_id} sent {text}")
                sender_user.text_messages.append({
                    "sender" : sender_user.key_id,
                    "to" : to_key_id,
                    "utime" : int(time.time()),
                    "text" : text
                })
            r = requests.post(f"{self.url}/text", json=data, timeout=5)
            if r.status_code == 200:
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
            r = requests.post(f"{self.url}/alive", json=data, timeout=5)
            if r.status_code == 200:
                resp = r.json()
                m : dict
                for m in resp.get('messages', []):
                    if m not in user.messages:
                        seq, ack, text_len, text = self.decrypt_message(user, m)
                        sender_id = m['sender']
                        user.add_message(m)
                        decrypted_msg = {
                            "utime" : m['utime'],
                            "sender" : m["sender"],
                            "to" : m["to"]
                        }
                        if text and len(text) > 0:
                            decrypted_msg['text'] = text
                            with user.lock:
                                user.text_messages.append(decrypted_msg)
                        if text_len > 0:
                            self.log(f"[{user.nickname}] Received: {text} from {sender_id}")
                            print(f"[DEBUG] from {sender_id} got {text}")
                            user.last_in_seq[sender_id] = seq
                            self.send_text(user, sender_id, '')
                            print(f"[DEBUG] sent ACK on message {seq} to {sender_id}")

                        if ack != 0 and ack in user.messages_by_seq.get(sender_id, {}):
                            print(f"[DEBUG] got ACK on message {ack} from {sender_id} - {user.messages_by_seq.get(sender_id, {}).pop(ack)}")
                            #TODO use it later to confirm a message was acknowledged
                        else:
                            self.log(f"[{user.nickname}] Delaying message from {m.get('sender')}: missing key or decryption failed.")
        except Exception as e:
            print(f"[DEBUG] poll_user error for {user.nickname}: {e}")
            self.log(f"[DEBUG] poll_user error for {user.nickname}: {e}")


    def run(self):
        last_sync = 0
        last_poll = 0
        # last_send = {}

        print(f"[DEBUG] Starting simulator with {len(self.local_users)} local users...", flush=True)
        
        while self.running:
            now = time.time()
            
            if now - last_sync > REMOTE_SYNC_INTERVAL:
                self.sync_remote_users()
                last_sync = now
            if now - last_poll > POLL_INTERVAL:
                for u in self.local_users.values():
                    self.poll_user(u)

            u : UserState
            for u in self.local_users.values():
                if u.messages_to_send.empty(): continue
                text, target_id = u.messages_to_send.get()
                print(f"[DEBUG] {text}")
                self.send_text(u, target_id, text)
            time.sleep(SIM_LOOP_DELAY)

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
                    new_user = UserState(nickname=fullname)
                    new_user.password = password
                    
                    new_user.save()

                    # בודקים שהמשתמש לא קיים כבר ברשימה הריצה
                    sim.local_users[new_user.key_id] = new_user
                    
                    message = f"User '{fullname}' created successfully! You can now log in."
                except Exception as e:
                    message = f"[ERROR] Failed to create user: {e}"
        #TODO: if preformed sign up, why not sign in?
        elif action == "login":
            if not os.path.exists(filename):
                message = "User not found. Please Sign Up first."
            else:
                try:
                    # 1. טעינת המשתמש דרך המחלקה (טוענת אוטומטית גם את הסיסמה)
                    user = UserState(filename=filename)
                    
                    if user.password == password:
                        sim.users_registery[user.key_id] = {
                            "phone" : user.phone,
                            "nick" : user.nickname,
                            "name" : user.fullname,
                            "key_id" : user.key_id,
                            "key_b64" : base64.b64encode(user.key.public_bytes()).decode('ascii'),
                            "delay_secs" : 0
                        }

                        session['key_id'] = user.key_id
                        return redirect(url_for('home_page'))
                    else:
                        message = "Incorrect password. Please try again."
                except Exception as e:
                    message = f"[ERROR] Failed to log in: {e}"

    return render_template('index.html', message=message)


# נניח שמשתנה ה-sim הגלובלי שלך נגיש (נגדיר אותו מחוץ ל-main או נשמור התייחסות אליו)
# לצורך הפשטות, ודא שמשתנה ה-sim שנוצר ב-main נשמר כמשתנה גלובלי בקובץ, למשל: sim = None

@app.route('/messages/<int:target_id>', methods=['GET', 'POST'])
def chat(target_id):
    status_message = None
    current_id = session.get('key_id')
    if not current_id in sim.local_users:
        return "<h1>Access Denied: Error 401</h1><p>You must log in first to view this page!</p>", 401
    
    sender_user = sim.local_users.get(current_id)
    if request.method == 'POST':
        text = request.form.get('message_text')

        if sender_user:
            sender_user.messages_to_send.put((text, target_id))
        else:
            status_message = "Error: No local logged-in user found."

    if target_id in sim.users_registery:
        target_user_obj = sim.users_registery[target_id]
    else:
        return "<h1>Access Denied: Error 401</h1><p>Target user not found!</p>", 401

    # --- שליפת היסטוריית ההודעות המלאה שלי ---
    chat_history = sender_user.text_messages

    return render_template('chat.html', 
                           target_user=target_user_obj, 
                           status_message=status_message,
                           chat_history=chat_history, 
                           my_id=current_id)


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
    if not current_id in sim.local_users:
        return "<h1>Access Denied: Error 401</h1><p>You must log in first to view this page!</p>", 401

    with sim.lock: 
        current_user = sim.local_users.get(current_id)
        if not current_user:
            return "<h1>Access Denied: Error 401</h1><p>Session user not found. Please log in again.</p>", 401
        users_list = [user for user in sim.users_registery.values() if user['key_id'] != session['key_id']]
    print(sim.users_registery)
    # מעבירים את users_list (שמכילה רק אובייקטים) ואת current_user ל-HTML
    return render_template('home.html', users=users_list, current_user=current_user, get_color=_get_delay_color)

@app.route('/update-profile', methods=['POST'])
def update_profile():
    if not session.get('key_id') in sim.local_users:
        return "<h1>Access Denied</h1>", 401
        
    current_id = session['key_id']
    current_user = sim.local_users[current_id]
    print(request.form)
    current_user.nickname = request.form.get('nickname')
    current_user.phone = request.form.get('phone')
    current_user.is_encrypted = 'is_encrypted' in request.form
    return redirect(url_for('home_page'))

if __name__ == "__main__":
    lock_file_path = "router_client.lock"
    try:
        # Keep the file object alive in the local scope of main()
        # so the lock isn't released until the script exits.
        lock_file = open(lock_file_path, 'w')
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        print(f"Error: Another instance of router_client.py is already running in this directory (lock file: {lock_file_path}).")
        sys.exit(1)

    print("[DEBUG] Initializing Simulator background thread...")
    parser = argparse.ArgumentParser(description="LoRa Router implementing the REST API and LoRa text protocol")
    parser.add_argument("--router_port", type=int, default=8200, 
                    help="Port for the router API (default: 8200)")
    parser.add_argument("--port", type=int, default=5000, 
                    help="Port for the Flask API (default: 5000)")
    parser.add_argument("--verbose", type=bool, default=False, 
                    help="Show tons of debug messages")
    # parser.add_argument("--port", type=int, default=5000, 
    #                 help="Port for the Flask API (default: 5000)")
    args = parser.parse_args()
    
    sim = Simulator(url=ROUTER_URL + str(args.router_port), verbose=args.verbose)
    
    # 4. מפעילים את ה-Simulator ברקע כדי שלא יחסום את האתר
    simulator_thread = threading.Thread(target=sim.run, daemon=True)
    simulator_thread.start()
    print("[DEBUG] Simulator is now running in the background.")

    # 5. מפעילים את שרת ה-Flask (האתר)
    print("[DEBUG] Starting the English Auth Flask Server...")
    app.run(debug=False, port=args.port, use_reloader=False)
