import argparse
import json
import os
import sys
import time
import requests
import base64
import threading
import struct
import random, queue, threading
import glob
import hashlib

# Ensure lora_common is in path for pubkey
sys.path.append(os.path.join(os.path.dirname(__file__), 'lora_common'))
from pubkey import RSA1024

ROUTER_URL = "http://localhost:8080"
COORDINATOR_URL = "http://34.165.8.95:8080"  # Franji's Google cloud node 2025-04-16
# use http://34.165.8.95:8080/coordinator_stats to check stats

class UserState:
    def __init__(self, filename=None, nickname=None):
        self.lock = threading.RLock()
        self.key = None
        self.key_id = None
        self.messages = []
        self.last_seq = {}
        self.messages_to_send : queue = queue.Queue()
        
        if nickname:
            self.nickname = nickname
            self.fullname = nickname
            self.phone = ""
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
                self.phone = data.get('phone', '')
                pem = data.get('private_key')
                if pem:
                    self.key = RSA1024.from_private_text(pem)
                    self.key_id = int.from_bytes(self.key.key_id(), 'big')
                self.messages = data.get('messages', [])
                self.last_seq = data.get('last_seq', {})
        except Exception as e:
            print(f"Error loading state from {self.filename}: {e}")
            

    def generate(self):
        print(f"Generating new key pair for {self.nickname}...", flush=True)
        self.key = RSA1024.generate()
        self.key_id = int.from_bytes(self.key.key_id(), 'big')
        self.save()

    def save(self):
        with self.lock:
            data = {
                'nickname': self.nickname,
                'fullname': self.fullname,
                'phone': self.phone,
                'private_key': self.key.private_key_text() if self.key else None,
                'messages': self.messages,
                'last_seq': self.last_seq
            }
            try:
                with open(self.filename, 'w') as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                print(f"Error saving {self.filename}: {e}", flush=True)

    def add_message(self, msg):
        with self.lock:
            if msg not in self.messages:
                self.messages.append(msg)
                self.save()
                return True
        return False
    
def read_line(user : UserState):
    while True:
        text = input()
        print(text)
        splt_text = text.split('#')
        if len(splt_text) == 1: splt_text = (None, splt_text[0])
        user.messages_to_send.put((splt_text[0], splt_text[1]))
        time.sleep(2.5)

class Simulator:
    def __init__(self, url, coordinator_url, interval, verbose=False, plain_text=False):
        self.url = url
        self.coordinator_url = coordinator_url
        self.interval = interval
        self.verbose = verbose
        self.plain_text = plain_text
        self.local_users = []
        self.remote_users = {} # key_id -> dict
        self.running = True
        self.stats = {'sent': 0, 'received': 0, 'replied': 0, 'cleared': 0}
        self.message_counter = 0
        self.session_prefix = int(time.time() % 100000)
        self.router_connected = False
        self.coordinator_connected = False

    def load_users(self):
        files = glob.glob("user_*.json")
        for f in files:
            try:
                u = UserState(filename=f)
                if u.key_id:
                    self.local_users.append(u)
                    self.log(f"Loaded local user: {u.nickname} ({u.key_id})")
            except Exception as e:
                self.log(f"Failed to load user file {f}: {e}")

    def create_user(self, nickname):
        filename = f"user_{nickname}.json"
        if os.path.exists(filename):
           # print(f"Warning: User '{nickname}' already exists. Not recreating.", flush=True)
            return
        u = UserState(nickname=nickname)
        self.log(f"Created user: {u.nickname} ({u.key_id})")

    def log(self, msg):
        if self.verbose:
          #  print(f"[VERBOSE] {msg}", flush=True)
          pass

    def report_to_coordinator(self, event_type, sender, receiver, message_id, **kwargs):
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
                  #  print(f"Connected to coordinator: {self.coordinator_url}", flush=True)
                    self.coordinator_connected = True
            else:
                self.log(f"Coordinator report returned {r.status_code}: {r.text}")
        except Exception as e:
            self.log(f"Coordinator report failed: {e}")

    def sync_remote_users(self):
        try:
            r = requests.get(f"{self.url}/users", timeout=5)
            if r.status_code == 200:
                if not self.router_connected:
                    print(f"Connected to router: {self.url}", flush=True)
                    self.router_connected = True
                users = r.json().get('users', [])
                received_key_ids = {u['key_id'] for u in users}
                simulated_local_ids = {lu.key_id for lu in self.local_users}
                
                for lu in self.local_users:
                    if lu.key_id not in received_key_ids:
                     #   print(f"\n[WARNING] Local user {lu.nickname} ({lu.key_id}) is NOT reported by router /users API.", flush=True)
                        pass

                has_remote = any(uid not in simulated_local_ids for uid in received_key_ids)
                if users and not has_remote:
                    if self.verbose:
                   #     print("\n[INFO] Only local users found in /users. No remote routers/users discovered yet.", flush=True)
                        pass

                for u in users:
                    kid = u['key_id']
                    self.remote_users[kid] = u
                    if kid not in simulated_local_ids:
                        remote_file = f"remote_{kid}.json"
                        if not os.path.exists(remote_file):
                            try:
                                with open(remote_file, 'w') as f:
                                    json.dump(u, f, indent=2)
                                self.log(f"Created {remote_file}")
                            except Exception as e:
                                self.log(f"Failed to save {remote_file}: {e}")
        except Exception as e:
            self.log(f"Failed to sync users: {e}")
            print("bug 198")

    def decrypt_message(self, local_user, msg):
        if 'text' in msg: return msg['text']
        
        is_plain = 'plain2_b64' in msg
        if not is_plain and 'crypt2_b64' not in msg:
            return None
            
        sender_id = msg['sender']
        sender_info = self.remote_users.get(sender_id)
        if not sender_info and not is_plain:
            return None
            
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
            return decrypted_payload[3:3+text_len].decode('utf-8')
        except Exception as e:
            return None

    def send_text(self, sender_user, to_key_id, text, report=False, mid=None):
        print("Has tex")
        target_info = self.remote_users.get(to_key_id)
        print(target_info)
        if not target_info and not self.plain_text:
            print("here is the bug line 230")
            return False
            
        try:
            print("debug0")
            seq = (sender_user.last_seq.get(str(to_key_id), 0) + 1) % 256
            if seq == 0: seq = 1
            sender_user.last_seq[str(to_key_id)] = seq
            sender_user.save()
            print("debug1")
            text_bytes = text.encode('utf-8')
            payload = struct.pack("BBB", seq, 0, len(text_bytes)) + text_bytes
            
            data = {
                'sender': sender_user.key_id,
                'to': to_key_id,
                'utime': int(time.time())
            }
            print("debug2")

            if self.plain_text:
                plain2_b64 = base64.b64encode(payload).decode('ascii')
                data['plain2_b64'] = plain2_b64
                payload_hash = hashlib.sha256(payload).hexdigest()
            else:
                target_pub_key = RSA1024.from_public_bytes(base64.b64decode(target_info['key_b64']))
                encrypted = target_pub_key.encrypt(payload)
                signed_encrypted = sender_user.key.decrypt(encrypted)
                crypt2_b64 = base64.b64encode(signed_encrypted).decode('ascii')
                data['crypt2_b64'] = crypt2_b64
                payload_hash = hashlib.sha256(signed_encrypted).hexdigest()
            
            print(f"from {sender_user.key_id} to {to_key_id} sent {text}")
            r = requests.post(f"{self.url}/text", json=data, timeout=5)
            if r.status_code == 200:
                if report and mid:
                    self.stats['sent'] += 1
                    self.report_to_coordinator('SENT', sender_user.key_id, to_key_id, mid, payload_hash=payload_hash)
                return True
            return False
        except Exception as e:
            return False

    def poll_user(self, user):
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
                for m in resp.get('messages', []):
                    if m not in user.messages:
                        text = self.decrypt_message(user, m)
                        if text:
                            crypt2 = base64.b64decode(m.get('crypt2_b64', m.get('plain2_b64', '')))
                            payload_hash = hashlib.sha256(crypt2).hexdigest()
                            
                            user.add_message(m)
                            sender_id = m['sender']
                            self.log(f"[{user.nickname}] Received: {text} from {sender_id}")
                            
                            if text.startswith("LOAD:"):
                                mid = text[5:]
                                self.stats['received'] += 1
                                self.report_to_coordinator('RECEIVED', sender_id, user.key_id, mid, payload_hash=payload_hash)
                                self.send_text(user, sender_id, f"ACK:{mid}")
                                self.stats['replied'] += 1
                                self.report_to_coordinator('REPLIED', sender_id, user.key_id, mid, payload_hash=payload_hash)
                                #here we get new text, not ack
                                print(f"from {sender_id} got {mid}")
                            elif text.startswith("ACK:"):
                                mid = text[4:]
                                self.stats['cleared'] += 1
                                self.report_to_coordinator('CLEARED', user.key_id, sender_id, mid, payload_hash=payload_hash)
                        else:
                            self.log(f"[{user.nickname}] Delaying message from {m.get('sender')}: missing key or decryption failed.")
        except Exception as e:
            self.log(f"poll_user error for {user.nickname}: {e}")

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
        try:
            r = requests.get(f"{self.coordinator_url}/coordinator_stats", timeout=2)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None

    def run(self):
        last_sync = 0
        last_send = {}

        print(f"Starting simulator with {len(self.local_users)} local users...", flush=True)

        for u in self.local_users:
            print(u.key_id)
            threading.Thread(target=read_line, args=(u, ), daemon=True).start()
        
        while self.running:
            now = time.time()
            
            if now - last_sync > 10:
                self.sync_remote_users()
                last_sync = now

            for u in self.local_users:
                self.poll_user(u)

            u : UserState
            for u in self.local_users:
                if u.key_id is None: continue
                if u.key_id not in last_send: last_send[u.key_id] = 0
                if now - last_send[u.key_id] > self.interval and not u.messages_to_send.empty():
                    all_known = set(k for k in self.remote_users.keys() if k is not None)
                    candidates = [kid for kid in all_known if kid != u.key_id]
                    if candidates:
                        self.message_counter += 1
                        
                        target_id, mid = u.messages_to_send.get()
                        self.log(f"[{u.nickname}] Sending LOAD:{mid} to {target_id}")
                        self.send_text(u, target_id, f"LOAD:{mid}", report=True, mid=mid)
                    else:
                        if self.verbose and now - last_sync < 1.0:
                          #  print(f"[WARNING] No remote users available for {u.nickname} to send to.", flush=True)
                            pass
                    last_send[u.key_id] = now
            
            nicks = ",".join([u.nickname for u in self.local_users])
            stats_msg = f"Stats[{nicks}]: Sent:{self.stats['sent']} Received:{self.stats['received']} Acked:{self.stats['replied']} Cleared:{self.stats['cleared']}   "
            if not self.verbose:
                #print(f"\r{stats_msg}", end="", flush=True)
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
            
            time.sleep(1)

def main():
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

    parser = argparse.ArgumentParser(description="LoRa Load Test Simulator")
    parser.add_argument("--new-user", help="Create/Load a new user with this nickname")
    parser.add_argument("--url", default=ROUTER_URL, help="Router URL")
    parser.add_argument("--coordinator", default=COORDINATOR_URL, help="Coordinator URL")
    parser.add_argument("--interval", type=int, default=30, help="Send interval T_SEND (seconds)")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--plain", action="store_true", help="Send messages as plain text (no encryption)")
    args = parser.parse_args()

    sim = Simulator(args.url, args.coordinator, args.interval, args.verbose, args.plain)
    
    if args.new_user:
        sim.create_user(args.new_user)
        
    sim.load_users()

    if not sim.local_users:
        print("Error: No local users found. Please run with --new-user <nickname> to create one.")
        sys.exit(1)

    try:
        sim.run()
    except KeyboardInterrupt:
        print("\nStopping...")

if __name__ == "__main__":
    main()